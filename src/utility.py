"""
Shared helpers for the agents: skill loading, environment knowledge, and the
structured-output call with its retry policy.

IMPORTS ARE DELIBERATELY NARROW. This module used to import the prompts, the
pydantic schemas, langchain's chat model and graph types, pypdf, the run
archiver and mcp_explorer -- about thirty names, of which it used none. Because
agents.py imports this module, every one of those was pulled in before any
agent could start, which dragged the whole MCP stack into the import graph of
anything that touched a skill file. Add an import here only when something in
this file actually uses it.
"""

import asyncio
import os
import random
import time

from rich.console import Console

from common.consts import (
    SKILLS_PATH, ENV_KNOWLEDGE, ENV_NOTES, USE_CASE_SKILL_REDIRECTS,
)
from trace_logger import tracer, extract_usage, message_to_dict

console = Console()


def _redirect_skill(rel_path: str) -> str:
    """Rewrite use_cases/<key>/<agent> requests to a single consolidated skill file.

    Used for side-by-side comparisons where one domain (e.g. cosmology) should
    load one shared skill file for every agent instead of its per-agent files.
    """
    parts = rel_path.split("/")
    if len(parts) >= 2 and parts[0] == "use_cases":
        target = USE_CASE_SKILL_REDIRECTS.get(parts[1])
        if target:
            return target
    return rel_path


async def _read_skill(rel_path: str, agent_name: str, enabled: bool = True) -> str:
    """Read skills/<rel_path>.SKILL.md -- returns '' if disabled (condition A) or not found.

    Condition A never touches the filesystem under skills/ at all -- not even an
    os.path.isfile check -- so disabled short-circuits before any I/O happens.
    """
    if not enabled:
        tracer.log_skill_load(agent_name, rel_path, found=False, suppressed=True)
        return ""
    requested = rel_path
    rel_path = _redirect_skill(rel_path)
    if rel_path != requested:
        # USE_CASE_SKILL_REDIRECTS can point a request at a completely
        # different file. Only the post-redirect path reaches the tracer, so
        # a substitution used to be invisible: the trace said one skill was
        # loaded while the prompt contained another. Say it out loud.
        console.print(f"[yellow][{agent_name}] skill redirect: "
                      f"{requested} -> {rel_path}[/yellow]")
    full = os.path.join(SKILLS_PATH, rel_path + ".SKILL.md")

    # A use case may be laid out either way, and BOTH requests must work on
    # BOTH layouts:
    #   old: use_cases/<name>/<agent>.SKILL.md, one file per agent
    #   new: use_cases/<name>/domain.SKILL.md,  one file for all agents
    # So a per-agent request falls back to domain, and a domain request falls
    # back to this agent's file. Only the direction agent->domain existed
    # before, which left "use_cases/cosmology/domain" silently returning "".
    if not os.path.isfile(full):
        parts = rel_path.split("/")
        if len(parts) == 3 and parts[0] == "use_cases":
            alt = (f"{parts[0]}/{parts[1]}/domain" if parts[2] != "domain"
                   else f"{parts[0]}/{parts[1]}/{agent_name}")
            if os.path.isfile(os.path.join(SKILLS_PATH, alt + ".SKILL.md")):
                rel_path = alt
                full = os.path.join(SKILLS_PATH, rel_path + ".SKILL.md")

    found = os.path.isfile(full)
    tracer.log_skill_load(agent_name, rel_path, found, suppressed=False)
    if found:
        with open(full) as f:
            return f.read()
    return ""

async def _list_skills(folder: str) -> list:
    """List available names in skills/<folder>/: subdirectory names AND .SKILL.md base names."""
    d = os.path.join(SKILLS_PATH, folder)
    if not os.path.isdir(d):
        return []
    results = []
    for name in os.listdir(d):
        if os.path.isdir(os.path.join(d, name)):
            results.append(name)
        elif name.endswith(".SKILL.md"):
            results.append(os.path.splitext(name)[0])
    return results


async def _resolve_use_case_skill(domain: str, agent_name: str,
                                  enabled: bool = True) -> str:
    """Load a use case's skill for one agent, whichever layout its folder uses.

    This is the ONLY way a use-case skill should be loaded. It takes the domain
    from the run (--domain, validated against the folder list before the run
    starts) rather than from a model's skill_requests, because the model was
    choosing a folder from the whole index and could choose a different one on
    each call -- a run then loaded a domain nobody selected and nothing said so.

    Layouts, in precedence order:
      use_cases/<domain>/<agent>.SKILL.md   one file per agent (older)
      use_cases/<domain>/domain.SKILL.md    one file for all agents (newer)

    Per-agent wins when both exist, so a folder holding both is unambiguous.
    A folder that exists but holds neither is a packaging mistake and is
    reported, rather than returning "" the way a missing folder does -- those
    two cases need different fixes and used to be indistinguishable.
    """
    if not domain:
        return ""

    folder = os.path.join(SKILLS_PATH, "use_cases", domain)
    if not os.path.isdir(folder):
        console.print(f"[yellow][{agent_name}] no skills/use_cases/{domain}/ "
                      f"folder -- running without a domain skill[/yellow]")
        return ""

    for rel in (f"use_cases/{domain}/{agent_name}", f"use_cases/{domain}/domain"):
        if os.path.isfile(os.path.join(SKILLS_PATH, rel + ".SKILL.md")):
            return await _read_skill(rel, agent_name, enabled=enabled)

    console.print(
        f"[yellow][{agent_name}] skills/use_cases/{domain}/ has neither "
        f"{agent_name}.SKILL.md nor domain.SKILL.md[/yellow]"
    )
    return ""


async def _env_knowledge(env: str, agent_name: str, enabled: bool = True) -> str:
    """Return environment-knowledge content for the given env.

    enabled=True (condition B/C): reads the real knowledge/local|lcrc skill file, unchanged.
    enabled=False (condition A): the skill file is suppressed (logged as such); a short
    hardcoded ENV_NOTES substitute is returned instead of nothing.
    """
    skill_key = ENV_KNOWLEDGE.get(env, "knowledge/local")
    if enabled:
        return await _read_skill(skill_key, agent_name, enabled=True)
    # condition A: no filesystem touch, _read_skill(enabled=False) just logs it and bails
    await _read_skill(skill_key, agent_name, enabled=False)
    return ENV_NOTES.get(env, ENV_NOTES["local"])


# __ Transport-error retry _____________________________________________________
#
# A dropped HTTP response is not a bad answer: the model never finished sending
# one. The parse loop below retries a response it could not parse, but an
# exception out of llm.invoke escaped it entirely and killed the run -- a run
# that had often already done minutes of work.
#
# Matching is on the exception CLASS NAME rather than isinstance, for two
# reasons. This venv has two httpx packages (the normal one and the `httpx2`
# Academy re-raises through), and a RemoteProtocolError from one is not an
# instance of the other. And langchain/openai wrap SDK errors in their own
# types, so the real cause is often one or two links down the chain.
_TRANSPORT_ERROR_NAMES = {
    "RemoteProtocolError",   # peer closed the connection mid-body
    "ReadTimeout",
    "ReadError",
    "ConnectError",
    "ConnectTimeout",
    "APIConnectionError",    # openai SDK's wrapper around the above
    "APITimeoutError",
    "IncompleteRead",
}


def _is_transport_error(exc: BaseException) -> bool:
    """True for a dropped or timed-out HTTP response, at any nesting depth."""
    seen = set()
    cur = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if type(cur).__name__ in _TRANSPORT_ERROR_NAMES:
            return True
        cur = cur.__cause__ or cur.__context__
    return False


async def _invoke_with_transport_retry(llm, messages, agent_name,
                                       attempts: int = 4, base_delay: float = 2.0):
    """llm.invoke off the event loop, retried on transport failures only.

    Anything that is NOT a transport error -- a bad key, a context overflow, a
    refusal -- is raised straight away, because re-asking cannot fix it.
    Backoff is exponential with jitter so two agents failing at once do not
    retry in lockstep.
    """
    for attempt in range(1, attempts + 1):
        try:
            return await asyncio.to_thread(llm.invoke, messages)
        except BaseException as e:
            if not _is_transport_error(e) or attempt == attempts:
                raise
            delay = base_delay * (2 ** (attempt - 1)) * (0.5 + random.random())
            console.print(
                f"[yellow][{agent_name}] transport error on attempt "
                f"{attempt}/{attempts} ({type(e).__name__}: {e}); "
                f"retrying in {delay:.1f}s[/yellow]"
            )
            tracer.log_llm_call(
                agent_name, getattr(llm, "model_name", "") or "", [],
                f"TRANSPORT ERROR ({type(e).__name__}): {e}",
                input_tokens=0, output_tokens=0, total_tokens=0,
                latency_s=0.0, attempt=attempt,
            )
            await asyncio.sleep(delay)


async def _invoke_structured(llm, schema, messages, agent_name, retries=5):
    """Call llm and parse the response as schema, tolerating preamble text before the JSON block.

    Three failure modes, and only ONE is worth retrying:

      transport      handled inside _invoke_with_transport_retry, on its own
                     budget -- a dropped response has nothing to do with a
                     response that would not parse.
      truncation     the model stopped at its output limit mid-object. Raised
                     immediately: the same prompt at the same cap truncates at
                     the same place, so a retry is a full regeneration that
                     fails identically.
      malformation   valid JSON wrapped in prose, or a transient slip. THIS is
                     what the loop below exists for.
    """
    import json as _json, re as _re
    last_err = None
    model_name = getattr(llm, "model_name", None) or os.getenv("MODEL_NAME", "")
    msg_dicts = [message_to_dict(m) for m in messages]
    for attempt in range(retries):
        _t0 = time.time()
        response = await _invoke_with_transport_retry(llm, messages, agent_name)
        latency_s = round(time.time() - _t0, 2)
        text = response.content if hasattr(response, "content") else str(response)
        usage = extract_usage(response) or {
            "input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
        }
        tracer.log_llm_call(
            agent_name, model_name, msg_dicts, text,
            input_tokens=usage["input_tokens"], output_tokens=usage["output_tokens"],
            total_tokens=usage["total_tokens"], latency_s=latency_s, attempt=attempt + 1,
        )

        # Why the model stopped, where the gateway says so. With streaming=True
        # langchain assembles this from the final chunk, and some
        # OpenAI-compatible gateways omit it entirely -- the brace check below
        # covers that case without it.
        finish = (getattr(response, "response_metadata", None) or {}).get("finish_reason")

        if finish == "length":
            raise ValueError(
                f"{agent_name}: the model stopped at its output limit mid-response "
                f"({len(text)} chars, finish_reason=length), so the JSON is "
                f"incomplete and no retry will parse it. Raise or unset the output "
                f"cap for this agent, or shorten the schema it is asked to fill.\n"
                f"Last 200 chars:\n...{text[-200:]}"
            )

        match = _re.search(r'\{.*\}', text, _re.DOTALL)
        if not match:
            # An opening brace with no closing one is a TRUNCATION, not a model
            # that answered in prose. The regex needs both braces, so a cut-off
            # object matches nothing and reads exactly like no JSON at all --
            # which is why a response beginning {"literature_findings": [... was
            # reported as "no JSON found" on five consecutive attempts while the
            # actual cause was the output cap.
            if "{" in text:
                raise ValueError(
                    f"{agent_name}: the response opens a JSON object and never closes "
                    f"it -- truncated at {len(text)} chars. Raise or unset the output "
                    f"cap for this agent.\nLast 200 chars:\n...{text[-200:]}"
                )
            last_err = ValueError(f"No JSON object found in model response:\n{text[:500]}")
            console.print(f"[yellow][_invoke_structured] attempt {attempt+1}: "
                          f"no JSON found (finish_reason={finish}), retrying...[/yellow]")
            continue
        try:
            return schema.model_validate(_json.loads(match.group(0)))
        except Exception as e:
            # JSONDecodeError is a ValueError, so the old
            # `except (_json.JSONDecodeError, Exception)` caught nothing the
            # bare Exception did not.
            last_err = e
            console.print(f"[yellow][_invoke_structured] attempt {attempt+1}: "
                          f"parse error ({e}), retrying...[/yellow]")
    raise last_err