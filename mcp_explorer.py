"""
MCP Explorer Agent -- ReAct loop that calls workflow engine tools via MCP protocol.

The explorer receives tasks from the planner once the installer has finished
preparing the local venv, then iteratively calls tools (exposed by the MCP server) to complete each task:
submitting Python tasks, running shell commands, checking outputs, installing
missing packages, and recovering from errors.

The explorer calls the same tools whichever engine is behind the MCP server:
it writes the files the run needs (the workflow itself plus any configuration
that workflow reads), then asks the server to run the workflow. Which engine's
constructs the workflow file has to be written in is the only thing that
differs, and that comes from the engine reference in the system prompt.
"""

import os
import re
import sys
import json
import asyncio
import time
from typing import Optional
from contextlib import AsyncExitStack

from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from rich.console import Console
from trace_logger import tracer, extract_usage, message_to_dict
from rich.panel import Panel

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

console = Console()

# __ MCP Server Config _________________________________________________________

_SERVERS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "servers")

ENGINE_SERVERS = {
    "parsl": os.path.join(_SERVERS_DIR, "parsl_server.py"),
    "pycompss": os.path.join(_SERVERS_DIR, "pycompss_server.py"),
    "adios": os.path.join(_SERVERS_DIR, "adios_server.py"),
}


# __ LangChain Tool Wrappers __________________________________________________
# These tools are bound to the LLM. When called, they delegate to the MCP session
# stored in _mcp_session (set during the explorer's async run).

_mcp_session: Optional[ClientSession] = None


_event_loop: Optional[asyncio.AbstractEventLoop] = None


def _call_mcp_tool(tool_name: str, arguments: dict) -> str:
    """Synchronously call an MCP tool via the active session.
    
    Uses the event loop from the explorer's async context to avoid
    creating new threads/loops per call.
    """
    if _mcp_session is None:
        return json.dumps({"error": "MCP session not connected"})

    async def _call():
        result = await _mcp_session.call_tool(tool_name, arguments)
        if result.content:
            texts = [block.text for block in result.content if hasattr(block, "text")]
            return "\n".join(texts) if texts else "{}"
        return "{}"

    # Use the explorer's event loop directly
    if _event_loop and _event_loop.is_running():
        # Schedule the coroutine on the running loop and wait for result
        future = asyncio.run_coroutine_threadsafe(_call(), _event_loop)
        return future.result(timeout=600)
    else:
        return asyncio.run(_call())


# __ LLM configuration _________________________________________________________
# Load the project's .env here, in whatever process imports this module.
#
# academy_agents.py calls load_dotenv() too, but only in the launching process.
# Under --env local and --env hpc the Explorer lives in that process and
# inherits it; under --env multi-node it does not -- it is a Parsl worker on
# another node, whose environment is whatever worker_init built (modules, venv,
# setup_hpc.sh, PYTHONPATH) and nothing else. There MODEL_NAME and
# CODER_MODEL_NAME came back None and ChatOpenAI(model=None) failed pydantic
# validation before the first LLM call.
#
# The path is absolute and __file__-derived on purpose: worker_init does
# `cd <run_dir>`, so a bare load_dotenv() searching upward from the cwd is
# searching from the wrong place. load_dotenv does not override variables
# already set, so this is a no-op in the launching process.
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))


# __ Skill file helpers ________________________________________________________

_SKILLS_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "skills")

# common.consts is dependency-free literals, so importing it here cannot create a
# circular import. ENV_NOTES used to be duplicated in this file to dodge one that
# no longer applies -- src/utility.py already reads it from here, and a second
# copy drifting from the first is how condition A ends up describing a different
# machine to different agents.
from common.consts import USE_CASE_SKILL_REDIRECTS, ENV_NOTES

# Prompts and text constants. Kept in their own module: they are the part that
# gets edited most often, and this file is the loop that consumes them.
from systemprompts.explorer import (
    KNOWLEDGE_SKILLS,
    ENGINE_EXEC_PROMPTS,
    EXEC_PROMPT_HEAD,
    ENGINE_SKILLS,
    ENGINE_RELEVANT_TOOLS,
    SCRATCH_DIRS,
    EXPLORER_SYSTEM_PROMPT,
    SKILL_REVIEW_SYSTEM_PROMPT,
)


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


def _read_skill(rel_path: str, agent_name: str = "explorer", enabled: bool = True) -> str:
    """Read skills/<rel_path>.SKILL.md -- returns '' if disabled (condition A) or not found.

    Condition A never touches the filesystem under skills/ at all -- not even an
    os.path.isfile check -- so disabled short-circuits before any I/O happens.
    """
    if not enabled:
        tracer.log_skill_load(agent_name, rel_path, found=False, suppressed=True)
        return ""
    rel_path = _redirect_skill(rel_path)
    full = os.path.join(_SKILLS_ROOT, rel_path + ".SKILL.md")
    found = os.path.isfile(full)
    tracer.log_skill_load(agent_name, rel_path, found, suppressed=False)
    if found:
        if rel_path not in _loaded_skills:
            _loaded_skills.append(rel_path)
        with open(full) as f:
            return f.read()
    return ""


# set once per explorer run, lets the module-level load_skill tool see the
# current condition even though it has no access to AgentState when the LLM calls it
_current_condition: str = "B"


def set_condition(condition: str) -> None:
    """Set the run condition that the module-level load_skill tool reads.

    Callers in other modules cannot do this with a `global` statement:
    that binds a name in *their* module, leaving this one at its default
    and silently disabling condition A's skill suppression.
    """
    global _current_condition
    _current_condition = condition

# Skill paths actually read during this run, in first-load order. The post-run
# review needs to know which domain file was in play so it can propose (and
# optionally append) edits to the right file rather than guessing.
_loaded_skills: list[str] = []

@tool
def write_file(path: str, content: str) -> str:
    """Write a file the run needs: the workflow itself, or anything it reads.

    Use this for EVERY file you generate. The workflow .py goes through here,
    and so does every configuration file that workflow opens -- XML, JSON,
    YAML, .params, input decks. Write the configuration first, then the
    workflow that reads it, then call run_workflow.

    A bare filename lands in the work directory. A relative path is relative to
    it, so "config/params.json" works and the directory is created for you.
    Writing outside the work directory is refused.

    Python files are parsed before being written, so a syntax error is reported
    instead of leaving a broken file on disk. Anything the engine would rather
    you did differently comes back as `warnings` -- read them, they are about
    whether the file actually uses the engine.

    Args:
        path: file to write, e.g. "workflow.py" or "config/project.xml"
        content: full contents of the file
    """
    return _call_mcp_tool("write_file", {"path": path, "content": content})


@tool
def run_workflow(filename: str = "workflow.py", args: Optional[list] = None,
                 ranks: int = 0, timeout: int = 7200) -> str:
    """Run the workflow file. This is the only tool that executes anything.

    Call it after write_file has put the workflow AND every file it reads in
    place. The server builds this engine's launch command and runs it once.

    Args:
        filename: workflow file under the work directory (default: workflow.py)
        args: arguments passed to the workflow file itself
        ranks: ranks to launch, for engines that launch ranks. 0 lets the
               engine decide -- check get_resources before asking for more than
               one, and never ask for ranks when in_pbs is false.
        timeout: seconds before the run is killed (default: 7200)
    """
    return _call_mcp_tool("run_workflow", {
        "filename": filename,
        "args": list(args or []),
        "ranks": ranks,
        "timeout": timeout,
    })


@tool
def write_xml(kind: str, content: str) -> str:
    """Write COMPSs's project.xml or resources.xml. PyCOMPSs runs only.

    Both are required before run_workflow: they describe the machine COMPSs
    will run on, and its runtime will not come up without them. Call
    get_resources first so the hosts and slots you declare match the allocation
    that actually exists.

    Args:
        kind: "project" or "resources"
        content: the full XML document
    """
    return _call_mcp_tool("write_xml", {"kind": kind, "content": content})


@tool
def run_mpmd(blocks: list, filename: str = "workflow.py", timeout: int = 7200) -> str:
    """Run the pipeline as ONE mpirun with a rank block per program. ADIOS runs only.

    Use this instead of run_workflow when a compiled MPI binary and the Python
    stages must be alive AT THE SAME TIME, so ADIOS2's SST engine can stream
    between them live. run_workflow with `ranks` launches N copies of one
    program; this launches several different programs as rank blocks of one
    job, which is the only way an external binary can be one of the roles.

    Each block is an object with `ranks` plus either `exe` (a program, with
    optional `args`) or `stage` (the name of a stage function in the workflow
    file, which is re-entered as `python workflow.py --stage <name>`). At least
    two blocks, or nothing streams to anything.

    The file is re-entered once per stage block, so it must read --stage and
    dispatch to the named function, and must select the SST engine.

    Args:
        blocks: rank blocks in launch order, e.g.
                [{"ranks": 8, "exe": "/path/sim", "args": ["in.params"]},
                 {"ranks": 4, "stage": "analyze"}]
        filename: workflow file the `stage` blocks re-enter (default: workflow.py)
        timeout: seconds before the run is killed (default: 7200)
    """
    return _call_mcp_tool("run_mpmd", {
        "blocks": blocks, "filename": filename, "timeout": timeout,
    })


@tool
def get_status(run_id: str = "") -> str:
    """Status of the workflow runs started by run_workflow.

    With no argument, every run and its outcome. With a run_id, that run in
    full, including the untruncated stdout and stderr -- which is where to look
    when run_workflow's own output was cut short.

    Args:
        run_id: the run_id returned by run_workflow (default: all runs)
    """
    return _call_mcp_tool("get_status", {"run_id": run_id})


@tool
def install_package(package: str) -> str:
    """Install a pip package into the interpreter that runs workflows.

    Pure-Python dependencies only. The engine itself, and anything with
    compiled MPI bindings, comes from the environment this run was launched in
    -- pip cannot fix those, so do not try more than once.

    Args:
        package: pip requirement, e.g. "numpy" or "matplotlib==3.9.0"
    """
    return _call_mcp_tool("install_package", {"package": package})


@tool
def check_package(package: str) -> str:
    """Check whether a Python package can be imported.

    Probed with the same interpreter that runs workflows, so the answer is
    about the environment the workflow will actually see.

    Args:
        package: import name, e.g. "numpy", "mpi4py", "adios2"
    """
    return _call_mcp_tool("check_package", {"package": package})


@tool
def list_files(directory: str = "", max_entries: int = 400) -> str:
    """List the files under a directory. Default: the work directory.

    Use it to confirm the outputs you expected actually exist.

    Args:
        directory: directory to list (default: the work directory)
        max_entries: stop after this many files (default: 400)
    """
    return _call_mcp_tool("list_files", {
        "directory": directory, "max_entries": max_entries,
    })


@tool
def read_file(path: str, max_lines: int = 200, start_line: int = 1) -> str:
    """Read a file back -- an output, a generated file, or a log.

    Args:
        path: path of the file
        max_lines: how many lines to return (default: 200)
        start_line: 1-based line to start at (default: 1)
    """
    return _call_mcp_tool("read_file", {
        "path": path, "max_lines": max_lines, "start_line": start_line,
    })


@tool
def get_resources() -> str:
    """Detect the compute resources available to this run.

    Always call this FIRST, before writing anything that asks for ranks and
    before deciding whether you are on one machine or inside an allocation.

    Returns JSON with in_pbs, nnodes, ntasks, ranks_per_node, nodelist, launcher,
    the work and data directories, and facts specific to this engine. If in_pbs
    is false, call load_skill("local"); if it is true, call load_skill("hpc").
    """
    return _call_mcp_tool("get_resources", {})


def _engine_exec_prompt(engine: str) -> str:
    """Execution-contract text for this engine's generated workflow file."""
    return ENGINE_EXEC_PROMPTS.get(engine, EXEC_PROMPT_HEAD)


def _walk_artifacts(root: str):
    """Yield (path, size_bytes) for real output files under `root`.

    Prunes scratch directories in place via os.walk's dirnames list, so their
    contents are never stat'd rather than merely filtered out afterwards.
    """
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SCRATCH_DIRS]
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            try:
                size = os.path.getsize(full)
            except OSError:
                size = -1
            yield full, size

_SKILL_NAME_RE = re.compile(r"^name:\s*(\S+)\s*$", re.MULTILINE)


def _dedupe_skills(contents: list[str]) -> list[str]:
    """Drop blank entries and any skill that is already in the list.

    The two files named for an engine in ENGINE_SKILLS are concatenated into
    the explorer's system prompt verbatim, and that prompt is resent on every
    iteration of the ReAct loop -- so anything in there twice is paid for up to
    max_iterations times.

    Two files can be the same skill: knowledge/PARSL.SKILL.md declares
    `name: systems/parsl` in its own front matter, because it was copied from
    systems/parsl.SKILL.md and never re-headed. Matching on that declared name
    is what identifies the duplicate -- not a guess from how similar the text
    looks -- and a byte-identical fallback catches a copy with no front matter
    at all. Genuinely distinct references (adios, pycompss) declare different
    names or none, and are both kept.
    """
    kept: list[str] = []
    seen_names: set[str] = set()
    seen_bodies: set[str] = set()
    for content in contents:
        if not content:
            continue
        body = content.strip()
        match = _SKILL_NAME_RE.search(content)
        name = match.group(1) if match else None
        if (name and name in seen_names) or body in seen_bodies:
            console.print(
                f"[dim yellow][explorer] skipped duplicate engine skill "
                f"({name or 'identical content'})[/dim yellow]"
            )
            continue
        if name:
            seen_names.add(name)
        seen_bodies.add(body)
        kept.append(content)
    return kept


def _classify_engine_usage(engine: str, tool_name: str, tool_args: dict,
                            tool_result_text: str) -> tuple[Optional[str], Optional[bool]]:
    """Derive (engine_backend, engine_verified) for a tool call, for tracing.

    engine_backend is the engine the server reported handling the call. Every
    server states it in the same `engine` field, so this needs no per-engine
    cases: the server is authoritative about which engine ran, and there is no
    longer any fallback path for it to disagree with.

    engine_verified answers a narrower question -- did the file look like it
    used the engine at all. `write_file` returns `warnings` when it does not
    (no Parsl app, no @task, no ADIOS2 stream), which is the one signal here
    that a run can be structurally wrong while every tool call succeeds.
    """
    if tool_name not in ENGINE_RELEVANT_TOOLS:
        return None, None
    try:
        parsed = json.loads(tool_result_text)
    except (json.JSONDecodeError, AttributeError, TypeError):
        return None, None
    if not isinstance(parsed, dict):
        return None, None

    backend = parsed.get("engine")
    if backend is None:
        return None, None

    if tool_name == "write_file" and parsed.get("status") == "written":
        # Only .py files are checked, so no warnings key means either a clean
        # workflow or a config file -- neither is evidence of a problem.
        if "warnings" in parsed:
            return backend, False
        return backend, None

    return backend, backend == engine


@tool
def load_skill(name: str) -> str:
    """Load runtime-environment knowledge into your context: "local" or "hpc".

    Call this only after get_resources tells you which environment you're actually
    in -- do not load both. "local" covers single-machine constraints (no MPI,
    no PBS). "hpc" covers PBS/mpirun conventions and LCRC storage paths.

    Args:
        name: "local" or "hpc"
    """
    if name not in KNOWLEDGE_SKILLS:
        return f"Unknown skill '{name}'. Use 'local' or 'hpc'."
    # B/C read the real skill file. A never touches it, gets the same lean
    # ENV_NOTES substitute the orchestrator/planner use in that condition
    enabled = _current_condition != "A"
    content = _read_skill(KNOWLEDGE_SKILLS[name], enabled=enabled)
    return content or ENV_NOTES.get(name, ENV_NOTES["local"])


# Everything every engine exposes. The servers all inherit these from one base
# class, so this list is the same whichever engine is behind the session.
_COMMON_TOOLS = [
    get_resources, load_skill,
    check_package, install_package,
    list_files, read_file,
    get_status,
    write_file, run_workflow,
]

# Tools that exist for one engine only, bound just for that engine so the model
# is never offered a tool the server does not implement. PyCOMPSs needs its XML
# topology written before the runtime can start; the other two need nothing.
_ENGINE_ONLY_TOOLS = {
    "pycompss": [write_xml],
    "adios":    [run_mpmd],
}

EXPLORER_TOOLS = _COMMON_TOOLS


def _tools_for_engine(engine: str) -> list:
    """Tools to bind for this run: the common set plus this engine's own."""
    return _COMMON_TOOLS + _ENGINE_ONLY_TOOLS.get(engine, [])


# __ Explorer System Prompt ____________________________________________________

# __ Post-Run Skill Review _____________________________________________________

def _ask(prompt: str) -> str:
    """Prompt the human on stdin. Blocking -- callers run it off the event loop."""
    return input(prompt).strip()


async def _ask_user(prompt: str) -> str:
    """Prompt the human without blocking the running event loop."""
    return await asyncio.to_thread(_ask, prompt)


async def _ask_yes_no(prompt: str) -> bool:
    """Ask a yes/no question, re-asking until the answer is unambiguous."""
    while True:
        answer = (await _ask_user(prompt)).lower()
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        console.print("[yellow]Please answer 'yes' or 'no'.[/yellow]")


def _work_dir() -> str:
    """The directory this run actually writes to.

    MAW_WORK_DIR is what mcp_explorer exports to the server, what the server
    confines every write to, and what make_worker_init exports on the
    Explorer's compute node -- so it is the only authority on where the outputs
    are. This used to be `<repo>/work`, derived from __file__, which was the
    parent of the real work directory and ignored the variable entirely: point
    MAW_WORK_DIR anywhere else, or rotate run0 aside with MAW_ROTATE_RUN0, and
    the artifact manifest and the run digest both walked a directory the run
    had never touched -- reporting no files produced for a run that produced
    plenty.
    """
    repo_root = os.environ.get(
        "MAW_REPO_ROOT", os.path.dirname(os.path.abspath(__file__))
    )
    return os.environ.get(
        "MAW_WORK_DIR", os.path.join(repo_root, "work", "run0")
    )


def _runs_dir() -> str:
    """Where run logs and the recommendations file live."""
    repo_root = os.environ.get(
        "MAW_REPO_ROOT", os.path.dirname(os.path.abspath(__file__))
    )
    return os.path.join(repo_root, "runs")


def _domain_skill(domain: str, agent: str = "explorer") -> str:
    """Skill path for a domain: the single domain file, else the legacy per-agent one.

    `domain` is a folder name under skills/use_cases/, validated against the
    folders on disk by academy_agents before the run starts.

    A domain is one file now -- use_cases/<name>/domain.SKILL.md, carrying the
    Tools / Input Parameters / Outputs / Pitfalls / Guidelines sections that the
    post-run review writes against. The older directories still hold a file per
    agent (explorer/planner/installer/orchestrator), so those are the fallback.

    Checking for the file rather than consulting a redirect table is what makes
    a new domain work by being dropped in: USE_CASE_SKILL_REDIRECTS had to name
    every consolidated domain explicitly, so a new one silently loaded nothing
    until someone remembered to add it.
    """
    domain_path = os.path.join(_SKILLS_ROOT, "use_cases", domain, "domain.SKILL.md")
    if os.path.isfile(domain_path):
        return f"use_cases/{domain}/domain"
    return f"use_cases/{domain}/{agent}"


def _domain_skill_candidates() -> list[str]:
    """Skill paths loaded this run, domain/use-case files first.

    The review targets a domain file by default; engine and environment skills are
    still offered because an issue can genuinely belong to one of those instead.
    """
    use_cases = [p for p in _loaded_skills if p.startswith("use_cases/")]
    others = [p for p in _loaded_skills if not p.startswith("use_cases/")]
    return use_cases + others


def _build_run_digest(state: dict, exploration_log: list, final_summary: str,
                      max_chars: int = 40000) -> str:
    """Flatten what happened this run into a readable narrative for the review.

    Includes the planned tasks, every tool call with its result, and the explorer's
    own closing summary. LLM prompt bodies are deliberately excluded: they are huge
    and the decisions they produced are already visible in the tool calls.
    """
    lines = [
        f"Goal: {state.get('goal', '')}",
        f"Engine: {state.get('engine', '')} | Env: {state.get('env', '')} | "
        # The folder under skills/use_cases that this run loaded, which is also
        # what the review is asked to target: "use_cases/<domain>/domain".
        f"Domain: {state.get('domain', '') or '(none)'}",
        f"Input data files: {', '.join(state.get('selected_data_files', [])) or '(none)'}",
        "",
    ]

    if state.get("literature_findings"):
        lines.append("Literature findings:")
        lines += [f"  - {f}" for f in state["literature_findings"]]
        lines.append("")

    if state.get("tasks"):
        lines.append("Planned tasks:")
        lines += [f"  {i+1}. {t}" for i, t in enumerate(state["tasks"])]
        lines.append("")

    if _loaded_skills:
        lines.append("Skill files loaded during the run: " + ", ".join(_loaded_skills))
        lines.append("")

    lines.append("Tool calls, in order:")
    for e in exploration_log:
        status = "OK" if e.get("succeeded") else "FAILED"
        args = json.dumps(e.get("args", {}), default=str)[:400]
        lines.append(f"  [iter {e.get('iteration')}] {e.get('tool')} [{status}] args={args}")
        lines.append(f"      result: {str(e.get('result', ''))[:800]}")
        if e.get("engine_verified") is False:
            lines.append(f"      engine NOT verified (backend={e.get('engine_backend')})")

    if final_summary:
        lines += ["", "Explorer's own closing summary:", final_summary]

    # Record what actually landed on disk -- "the file is missing" is usually the
    # single most diagnostic fact available, and the log alone does not show it.
    work_dir = _work_dir()
    if os.path.isdir(work_dir):
        produced = [f"  {full} ({size} bytes)" for full, size in _walk_artifacts(work_dir)]
        lines += ["", f"Files present under {work_dir} after the run ({len(produced)}):"]
        lines += produced[:200]
    else:
        lines += ["", f"The work directory does not exist: {work_dir}"]

    digest = "\n".join(lines)
    if len(digest) > max_chars:
        head, tail = digest[: max_chars // 2], digest[-max_chars // 2 :]
        digest = f"{head}\n\n... [log truncated] ...\n\n{tail}"
    return digest


def _append_to_skill(rel_path: str, section: str, addition: str, is_new_section: bool) -> str:
    """Append `addition` under `section` in skills/<rel_path>.SKILL.md.

    Inserts at the end of the named section (just before the next heading of the same
    or higher level) so the text lands inside the section it was written for, rather
    than at the bottom of the file. Creates the section at the end when it is new.
    Returns a status string describing what happened.
    """
    # `rel_path` is model output, and this is the only function in the project
    # that writes into skills/. Confine it the same way the servers confine
    # write_file: a "../.." in that field would otherwise land outside the
    # skills tree. The user does see the path before approving, so this is a
    # backstop rather than the only check -- but it is a cheap one.
    full = os.path.normpath(
        os.path.join(_SKILLS_ROOT, _redirect_skill(rel_path) + ".SKILL.md")
    )
    root = os.path.normpath(_SKILLS_ROOT)
    if not full.startswith(root + os.sep):
        return f"SKIPPED (path escapes the skills directory: {rel_path!r})"

    if not os.path.isfile(full):
        return f"SKIPPED (no such skill file: {full})"

    with open(full) as f:
        lines = f.read().split("\n")

    target = section.lstrip("#").strip().lower()

    heading_idx = None
    heading_level = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("#"):
            level = len(stripped) - len(stripped.lstrip("#"))
            if stripped.lstrip("#").strip().lower() == target:
                heading_idx = i
                heading_level = level
                break

    if heading_idx is None:
        if not is_new_section:
            # The model named a section that isn't there. Adding it silently would
            # contradict its own is_new_section=False, so make the new heading visible.
            note = f"(section '{section}' was not found; added as a new section)"
        else:
            note = ""
        while lines and not lines[-1].strip():
            lines.pop()
        # Match the separator style the file already uses between top-level sections.
        block = ["", "---", "", f"# {section.lstrip('#').strip()}", "", addition.rstrip()]
        if not any(ln.strip() == "---" for ln in lines[3:]):
            block = ["", f"# {section.lstrip('#').strip()}", "", addition.rstrip()]
        lines += block
        with open(full, "w") as f:
            f.write("\n".join(lines) + "\n")
        return f"APPENDED as new section '{section}' {note}".strip()

    # Find where this section ends: the next heading at the same or higher level.
    end = len(lines)
    for j in range(heading_idx + 1, len(lines)):
        stripped = lines[j].strip()
        if stripped.startswith("#"):
            level = len(stripped) - len(stripped.lstrip("#"))
            if level <= heading_level:
                end = j
                break

    # Back off past trailing blanks and any '---' rule that separates sections, so
    # the addition stays inside this section instead of landing under the divider.
    insert_at = end
    while insert_at > heading_idx + 1 and (
        not lines[insert_at - 1].strip() or lines[insert_at - 1].strip() == "---"
    ):
        insert_at -= 1

    lines[insert_at:insert_at] = [addition.rstrip()]
    with open(full, "w") as f:
        f.write("\n".join(lines) + "\n")
    return f"APPENDED under existing section '{section}'"


async def post_run_review(state: dict) -> dict:
    """Run the explorer's end-of-run verification exactly once, after the graph finishes.

    Called by the entrypoint rather than from inside the explorer node: the
    orchestrator can route back to the explorer several times, and the user should
    be asked about the run as a whole, once, not once per pass.

    Never raises -- a failed review must not take down an otherwise finished run.
    """
    llm = ChatOpenAI(
        model=os.getenv("CODER_MODEL_NAME", os.getenv("MODEL_NAME")),
        streaming=True,
        stream_usage=True,
    )
    try:
        return await _post_run_skill_review(
            state,
            state.get("exploration_log", []),
            state.get("explorer_summary", ""),
            llm,
        )
    except Exception as e:
        console.print(f"[red][explorer] post-run review failed: {e}[/red]")
        return {}


async def _post_run_skill_review(state: dict, exploration_log: list,
                                 final_summary: str, llm) -> dict:
    """Ask the user whether the run worked; on 'no', propose and optionally apply skill edits.

    Returns the state fields describing the outcome. Never raises -- a failure to
    review must not take down an otherwise finished run.
    """
    console.print(Panel(
        "The workflow has finished. Confirm the result before the run is closed out.",
        title="[bold cyan]Run Verification[/bold cyan]",
        border_style="cyan",
    ))

    succeeded = await _ask_yes_no("Has the workflow executed properly? (yes/no): ")
    if succeeded:
        console.print("[green][explorer] Confirmed -- closing out the run.[/green]")
        tracer.log_user_verification(True, "")
        return {"run_succeeded": True, "reported_issue": "", "skill_recommendations": []}

    issue = ""
    while not issue:
        issue = await _ask_user("What issue occurred with the workflow? ")
        if not issue:
            console.print("[yellow]Please describe the issue so it can be diagnosed.[/yellow]")
    tracer.log_user_verification(False, issue)

    console.print("\n[dim cyan][explorer] reviewing the run against the reported issue...[/dim cyan]")

    candidates = _domain_skill_candidates()
    # Condition A withholds skill content by design; don't surface it here either.
    # The review still runs, just without knowing what the files already say.
    enabled = state.get("condition", "B") != "A"

    current = ""
    if enabled and candidates:
        blocks = []
        for rel in candidates:
            path = os.path.join(_SKILLS_ROOT, _redirect_skill(rel) + ".SKILL.md")
            if os.path.isfile(path):
                with open(path) as f:
                    blocks.append(f"--- skills/{_redirect_skill(rel)}.SKILL.md ---\n{f.read()}")
        if blocks:
            current = ("\n\n=== Current contents of the skill files used this run ===\n"
                       "Do not duplicate guidance already present; sharpen it instead.\n\n"
                       + "\n\n".join(blocks))

    targets = ("\n\nSkill files loaded this run (target one of these):\n"
               + "\n".join(f"  - {_redirect_skill(c)}" for c in candidates)) if candidates else ""

    human = (
        f"The user reports the workflow did not execute properly.\n\n"
        f"=== Issue reported by the user ===\n{issue}\n\n"
        f"=== Full record of the run ===\n"
        f"{_build_run_digest(state, exploration_log, final_summary)}"
        f"{current}{targets}"
    )

    try:
        result = await asyncio.to_thread(
            llm.invoke,
            [SystemMessage(content=SKILL_REVIEW_SYSTEM_PROMPT),
             HumanMessage(content=human)],
        )
    except Exception as e:
        console.print(f"[red][explorer] skill review call failed: {e}[/red]")
        return {"run_succeeded": False, "reported_issue": issue, "skill_recommendations": []}

    text = result.content if hasattr(result, "content") else str(result)
    usage = extract_usage(result) or {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    tracer.log_llm_call("explorer", getattr(llm, "model_name", ""),
                        [{"role": "system", "content": "<skill review>"},
                         {"role": "human", "content": human[:4000]}],
                        text, input_tokens=usage["input_tokens"],
                        output_tokens=usage["output_tokens"],
                        total_tokens=usage["total_tokens"])

    import re as _re
    match = _re.search(r"\{.*\}", text, _re.DOTALL)
    if not match:
        console.print("[yellow][explorer] skill review returned no parseable JSON.[/yellow]")
        return {"run_succeeded": False, "reported_issue": issue, "skill_recommendations": []}
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError as e:
        console.print(f"[yellow][explorer] could not parse skill review JSON: {e}[/yellow]")
        return {"run_succeeded": False, "reported_issue": issue, "skill_recommendations": []}

    root_cause = parsed.get("root_cause", "")
    recs = [r for r in parsed.get("recommendations", []) if isinstance(r, dict)]

    body = f"[bold]Root cause[/bold]\n{root_cause}"
    for i, rec in enumerate(recs, 1):
        tag = " [magenta](new section)[/magenta]" if rec.get("is_new_section") else ""
        body += (f"\n\n[bold]{i}. skills/{rec.get('skill_file')}.SKILL.md"
                 f" -> {rec.get('section')}{tag}[/bold]\n"
                 f"[green]{rec.get('addition', '')}[/green]\n"
                 f"[dim]Why: {rec.get('rationale', '')}[/dim]")
    if not recs:
        body += "\n\n[yellow]No skill-file change recommended for this issue.[/yellow]"
    console.print(Panel(body, title="[bold magenta]Suggested Skill Edits[/bold magenta]",
                        border_style="magenta"))

    applied = False
    if recs:
        apply = await _ask_yes_no(
            "\nApply these edits to the skill file(s) automatically? (yes/no): ")
        if apply:
            for rec in recs:
                status = _append_to_skill(
                    rec.get("skill_file", ""), rec.get("section", ""),
                    rec.get("addition", ""), bool(rec.get("is_new_section")),
                )
                rec["applied"] = status
                console.print(f"[green][explorer] {rec.get('skill_file')}: {status}[/green]")
            applied = True
        else:
            console.print("[dim][explorer] Edits not applied -- they remain in the trace "
                          "and the recommendations file.[/dim]")

    _write_recommendations_file(state, root_cause, recs, issue, applied)

    tracer.log_skill_recommendation(root_cause, recs)
    return {
        "run_succeeded": False,
        "reported_issue": issue,
        "skill_recommendations": recs,
        "skill_edits_applied": applied,
    }


def _write_recommendations_file(state: dict, root_cause: str, recs: list,
                                issue: str, applied: bool) -> None:
    """Save the review next to the run log so it survives the terminal session."""
    run_log = state.get("run_log") or ""
    if not run_log:
        runs_dir = _runs_dir()
        run_id = getattr(tracer.run_metadata, "run_id", "") if tracer.run_metadata else ""
        if not run_id:
            return
        run_log = os.path.join(runs_dir, run_id + ".jsonl")

    path = os.path.splitext(run_log)[0] + "_skill_recommendations.md"
    lines = [
        "# Skill Recommendations",
        "",
        f"Generated: {time.strftime('%Y-%m-%dT%H:%M:%S')}",
        f"Applied automatically: {'yes' if applied else 'no'}",
        "",
        "## Issue reported by the user",
        "",
        issue or "(none given)",
        "",
        "## Root cause",
        "",
        root_cause or "(none given)",
        "",
        "## Proposed additions",
        "",
    ]
    if not recs:
        lines.append("No skill-file change recommended for this issue.")
    for i, rec in enumerate(recs, 1):
        tag = " (NEW SECTION)" if rec.get("is_new_section") else ""
        lines += [
            f"### {i}. `skills/{rec.get('skill_file')}.SKILL.md` -> `{rec.get('section')}`{tag}",
            "",
            "Add:",
            "",
            "```markdown",
            rec.get("addition", ""),
            "```",
            "",
            f"Why: {rec.get('rationale', '')}",
        ]
        if rec.get("applied"):
            lines.append(f"Status: {rec['applied']}")
        lines.append("")

    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write("\n".join(lines) + "\n")
        console.print(f"[dim]Skill recommendations written: {path}[/dim]")
    except OSError as e:
        console.print(f"[yellow][explorer] could not write recommendations file: {e}[/yellow]")


# __ Explorer Node _____________________________________________________________

def explorer(state: dict) -> dict:
    """
    Explorer node -- connects to MCP server and runs a ReAct tool-calling loop
    to execute workflow tasks step by step.
    """
    global _mcp_session, _event_loop, _current_condition

    _current_condition = state.get("condition", "B")
    console.print("\n[dim cyan][explorer] starting interactive workflow execution...[/dim cyan]")
    tracer.log_agent_start("explorer", {"engine": state.get("engine", "parsl")})
    tracer.log_agent_input("explorer", {
        "tasks_count": len(state.get("tasks", [])),
        "findings_count": len(state.get("literature_findings", [])),
        "engine": state.get("engine", "parsl"),
    })

    engine = state.get("engine", "parsl")
    console.print(f"[dim cyan][explorer] connecting to {engine} MCP server...[/dim cyan]")

    # Create a dedicated event loop for the MCP session
    loop = asyncio.new_event_loop()
    _event_loop = loop

    try:
        result = loop.run_until_complete(_explorer_async(state, engine))
        return result
    finally:
        _event_loop = None
        loop.close()


async def _explorer_async(state: dict, engine: str) -> dict:
    """Async implementation of the explorer -- keeps MCP session alive throughout."""
    global _mcp_session

    server_path = ENGINE_SERVERS.get(engine)
    if not server_path or not os.path.isfile(server_path):
        raise FileNotFoundError(f"Server not found for engine '{engine}': {server_path}")

    # The server reads where everything lives from the environment and resolves
    # nothing itself, so whatever is exported here is what it will use. The
    # setup script run before launch is expected to have set these; the
    # fallbacks only cover MAW_REPO_ROOT, which is derivable from this file's
    # own location and is what the other two default from.
    #
    # The old HOST_REPO_PATH that used to be set here was read by nothing --
    # the servers looked for REPO_ROOT -- so the repo root never actually
    # crossed this boundary and each server silently fell back to its own
    # __file__. That worked only because the servers live inside the repo.
    repo_root = os.environ.get(
        "MAW_REPO_ROOT", os.path.dirname(os.path.abspath(__file__))
    )
    server_params = StdioServerParameters(
        command=sys.executable,
        args=[server_path],
        env={
            **os.environ,
            "MAW_REPO_ROOT": repo_root,
            "MAW_WORK_DIR": os.environ.get(
                "MAW_WORK_DIR", os.path.join(repo_root, "work", "run0")
            ),
            "MAW_DATA_DIR": os.environ.get(
                "MAW_DATA_DIR", os.path.join(repo_root, "data")
            ),
        },
    )

    async with AsyncExitStack() as stack:
        # Connect to MCP server
        stdio_transport = await stack.enter_async_context(stdio_client(server_params))
        read_stream, write_stream = stdio_transport
        session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
        await session.initialize()

        # Set global session so tools can use it
        _mcp_session = session

        # List available tools
        tools_result = await session.list_tools()
        available = [t.name for t in tools_result.tools]
        console.print(f"[dim cyan][explorer] MCP server tools: {available}[/dim cyan]")

        # Build context from state
        tasks = state.get("tasks", [])
        findings = state.get("literature_findings", [])
        stack_decision = state.get("stack_decision", [])

        data_files = state.get("selected_data_files", [])

        context_parts = []
        if data_files:
            context_parts.append("Available input data files (in /app/data/):\n" +
                                 "\n".join(f"  - {f}" for f in data_files))
        if findings:
            context_parts.append("Literature findings:\n" +
                                 "\n".join(f"  - {f}" for f in findings))
        if stack_decision:
            context_parts.append(f"Software stack in venv: {', '.join(stack_decision)}")
        if tasks:
            context_parts.append("Tasks to execute:\n" +
                                 "\n".join(f"  {i+1}. {t}" for i, t in enumerate(tasks)))
        if state.get("mpi_slots"):
            # The allocation is exactly this shape, so get_resources agrees
            # with it: nnodes, ntasks and ranks_per_node are the same numbers.
            # Stated here anyway because the rank LAYOUT matters and not just
            # the total -- 1x8 and 2x4 are both 8 ranks and need different
            # `--map-by ppr:N:node` values.
            context_parts.append(
                f"MPI capacity: {state['mpi_nodes']} nodes x "
                f"{state['mpi_ranks_per_node']} ranks per node = "
                f"{state['mpi_slots']} ranks total. get_resources reports the "
                f"same shape. Launch with "
                f"`-np {state['mpi_slots']} --map-by "
                f"ppr:{state['mpi_ranks_per_node']}:node` -- the ranks-per-node "
                f"in that layout must equal the allocation's mpiprocs, or the "
                f"launch fails with 'not enough slots available'."
            )

        # condition A never touches skills/ at all, not even to list use_cases/,
        # so the whole auto-detection block below just gets skipped
        _enabled = state.get("condition", "B") != "A"
        _base_skill = _read_skill("agents/explorer", enabled=_enabled)

        # The domain is chosen on the command line (--domain) and validated
        # against the folders under skills/use_cases/ before the run starts, so
        # here it is just read. Nothing is matched, guessed or scanned for.
        #
        # What used to be here: the --domain label substring-matched against
        # every folder name in both directions, and failing that, each
        # candidate skill's first 500 characters sniffed for a package name
        # from the installer's stack decision. Either could pick the wrong
        # folder, and when they picked none the run carried on with no domain
        # knowledge and reported nothing -- which is the same observable
        # behaviour as a domain skill that simply has little to say.
        _uc_skill = ""
        _domain = (state.get("domain") or "").strip()
        if _enabled and _domain:
            _uc_path = _domain_skill(_domain)
            _uc_skill = _read_skill(_uc_path)
            if _uc_skill:
                console.print(f"[dim cyan][explorer] domain skill: {_uc_path}[/dim cyan]")
            else:
                # Say so. The run can continue without domain knowledge, but
                # silently is how this went unnoticed before.
                console.print(
                    f"[bold yellow][explorer] domain '{_domain}' has no readable "
                    f"skill file (looked for {_uc_path}.SKILL.md) -- continuing "
                    f"without domain knowledge[/bold yellow]"
                )
        elif _enabled:
            console.print("[dim yellow][explorer] no --domain given -- running "
                          "without a domain skill[/dim yellow]")

        # force-load the engine skills instead of leaving it to the model to call
        # load_skill("adios"), that's exactly how you get imports with no real API use.
        # systems/<engine> loads before knowledge/<ENGINE> so the operational rules
        # land before the long general reference
        _sys_skill_path, _know_skill_path = ENGINE_SKILLS.get(engine, (None, None))
        _engine_skill = ""
        if _sys_skill_path:
            _engine_skill = "\n\n".join(_dedupe_skills([
                _read_skill(_sys_skill_path, enabled=_enabled),
                _read_skill(_know_skill_path, enabled=_enabled),
            ]))

        system_prompt = EXPLORER_SYSTEM_PROMPT
        if _base_skill:
            system_prompt = _base_skill + "\n\n---\n\n" + system_prompt

        # the execution contract differs per engine, so it goes in before the
        # reference material -- the model needs to know which tools exist first
        system_prompt += "\n\n" + _engine_exec_prompt(engine)

        if _engine_skill:
            system_prompt += (
                f"\n\n--- {engine.upper()} Engine Reference (REQUIRED -- read before "
                f"calling any execution tool) ---\n\n"
                f"This run's workflow engine is {engine}. Whether you actually exercise "
                f"its real API is recorded in the trace, not just whether you import it.\n\n"
                f"{_engine_skill}"
            )
        if _uc_skill:
            system_prompt += "\n\n--- Use Case Context ---\n\n" + _uc_skill

        feedback = state.get("orchestrator_feedback", "")
        if feedback:
            context_parts.append(f"\nOrchestrator feedback -- address these issues:\n{feedback}")

        human_message = "\n\n".join(context_parts)

        console.print(Panel(
            f"[bold]Tasks:[/bold] {len(tasks)}\n"
            f"[bold]Findings:[/bold] {len(findings)}\n"
            f"[bold]Engine:[/bold] {engine}",
            title="[bold cyan]Explorer Starting[/bold cyan]",
            border_style="cyan",
        ))

        # Initialize LLM with tool binding
        llm = ChatOpenAI(
            model=os.getenv("CODER_MODEL_NAME", os.getenv("MODEL_NAME")),
            streaming=True,
            stream_usage=True,
        )
        llm_with_tools = llm.bind_tools(_tools_for_engine(engine))

        messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=human_message),
        ]

        # ReAct loop
        # LLM calls are sync (blocking), tool calls go through MCP async session.
        # We run LLM in a thread to avoid blocking the event loop.
        max_iterations = 100
        exploration_log = []
        iteration = 0
        final_message = ""

        _MAX_TOOL_RESULT_CHARS = 8_000  # cap per tool result added to messages
        _CONTEXT_WINDOW        = 20     # message slots kept beyond system + human

        for iteration in range(max_iterations):
            console.print(f"\n[dim yellow][explorer] iteration {iteration + 1}/{max_iterations}[/dim yellow]")

            # Trim context window: keep system + human + last _CONTEXT_WINDOW messages.
            # Scan forward past any leading ToolMessages to avoid orphaned tool results.
            if len(messages) > 2 + _CONTEXT_WINDOW:
                tail = messages[-_CONTEXT_WINDOW:]
                start = next((i for i, m in enumerate(tail) if not isinstance(m, ToolMessage)), 0)
                messages = messages[:2] + tail[start:]
                console.print(f"[dim yellow][explorer] context trimmed to {len(messages)} messages[/dim yellow]")

            # Run LLM call in a thread (it's sync/blocking)
            _t0 = time.time()
            response = await asyncio.to_thread(llm_with_tools.invoke, messages)
            _latency_s = round(time.time() - _t0, 2)
            # `or {...}`: extract_usage returns None when the response carries
            # no usage info, and subscripting it below killed the whole
            # exploration on an LLM call that had actually succeeded. The
            # log_token_usage block further down already guarded for this.
            _usage = extract_usage(response) or {
                "input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
            }
            _model_name = getattr(llm, "model_name", None) or os.getenv(
                "CODER_MODEL_NAME", os.getenv("MODEL_NAME", ""))
            tracer.log_llm_call(
                "explorer", _model_name,
                [message_to_dict(m) for m in messages],
                response.content if hasattr(response, "content") else str(response),
                tool_calls=response.tool_calls or [],
                input_tokens=_usage["input_tokens"], output_tokens=_usage["output_tokens"],
                total_tokens=_usage["total_tokens"], latency_s=_latency_s, attempt=iteration + 1,
            )
            messages.append(response)

            usage = extract_usage(response)
            if usage:
                tracer.log_token_usage("explorer", usage["input_tokens"],
                                       usage["output_tokens"], usage["total_tokens"],
                                       model=getattr(llm, "model_name", ""))

            if not response.tool_calls:
                # This closing message is the explorer's own account of what it did
                # and what failed. The post-run review reads it back as evidence.
                final_message = response.content if response.content else ""
                console.print(Panel(
                    final_message[:3000] if final_message else "(no content)",
                    title="[bold green]Explorer Complete[/bold green]",
                    border_style="green",
                ))
                break

            mcp_broken = False
            for tool_call in response.tool_calls:
                tool_name = tool_call["name"]
                tool_args = tool_call["args"]
                tool_id = tool_call["id"]

                console.print(f"[dim cyan][explorer] calling tool: {tool_name}({json.dumps(tool_args, indent=2)[:200]})[/dim cyan]")

                if tool_name in ("run_workflow", "run_mpmd"):
                    _ranks = tool_args.get("ranks", 0)
                    console.print(Panel(
                        f"file={tool_args.get('filename', 'workflow.py')}"
                        + (f"  ranks={_ranks}" if _ranks else ""),
                        title=f"[bold yellow]Running {engine} workflow[/bold yellow]",
                        border_style="yellow",
                    ))

                # load_skill is client side, reads skill files directly via _read_skill.
                # no server implements it as a tool so it can't go through session.call_tool
                # like everything else below
                if tool_name == "load_skill":
                    tool_result = load_skill.invoke(tool_args)
                    console.print(f"[green][explorer] {tool_name} -> loaded locally[/green]")
                    exploration_log.append({
                        "iteration": iteration + 1,
                        "tool": tool_name,
                        "args": tool_args,
                        "result": tool_result[:2000],
                        "succeeded": True,
                    })
                    tracer.log_tool_call("explorer", tool_name, tool_args, tool_result,
                                         True, iteration=iteration + 1)
                    messages.append(ToolMessage(
                        content=tool_result[:_MAX_TOOL_RESULT_CHARS],
                        tool_call_id=tool_id,
                    ))
                    continue

                # Call MCP tool with timeout protection
                try:
                    mcp_result = await asyncio.wait_for(
                        session.call_tool(tool_name, tool_args),
                        timeout=1800,  # 30 min max per tool call
                    )
                    if mcp_result.content:
                        texts = [block.text for block in mcp_result.content if hasattr(block, "text")]
                        tool_result = "\n".join(texts) if texts else "{}"
                    else:
                        tool_result = "{}"
                except asyncio.TimeoutError:
                    tool_result = json.dumps({
                        "error": f"Tool call timed out after 1800s",
                        "status": "timeout",
                    })
                    console.print(f"[bold red][explorer] {tool_name} timed out -- skipping[/bold red]")
                except (BrokenPipeError, ConnectionError, EOFError) as e:
                    tool_result = json.dumps({
                        "error": f"MCP connection lost: {e}",
                        "status": "connection_lost",
                    })
                    console.print(f"[bold red][explorer] MCP connection lost -- ending exploration[/bold red]")
                    mcp_broken = True
                except Exception as e:
                    tool_result = json.dumps({"error": str(e)})

                # Determine success/failure from the result.
                #
                # Every server now returns the same shapes, because they all
                # inherit these tools from one base class -- so this no longer
                # has to guess at per-engine response formats.
                tool_succeeded = False
                display_status = "?"
                try:
                    parsed = json.loads(tool_result)
                    status_val = parsed.get("status")
                    exit_code = parsed.get("exit_code")
                    has_error = "error" in parsed

                    if status_val == "failed":
                        tool_succeeded = False
                        display_status = f"failed (exit {exit_code})"
                    elif status_val == "rejected":
                        # write_file refused to commit the file -- a syntax
                        # error, or a path outside the work directory. Scored
                        # as a failure so the orchestrator can tell a malformed
                        # file apart from a broken tool.
                        tool_succeeded = False
                        display_status = "rejected"
                    elif has_error:
                        tool_succeeded = False
                        display_status = f"error: {parsed['error'][:80]}"
                    elif status_val in ("completed", "success", "written", "cleaned up"):
                        # "written" is what write_file and write_xml return on
                        # success, "completed" what a run_workflow that exited 0
                        # returns. Both are successes; a file written with
                        # warnings is still written, and the warnings are
                        # surfaced separately below.
                        tool_succeeded = True
                        display_status = status_val
                        if parsed.get("warnings"):
                            display_status += f" ({len(parsed['warnings'])} warning(s))"
                    elif exit_code == 0:
                        tool_succeeded = True
                        display_status = "exit_code: 0"
                    elif parsed.get("installed") is True:
                        tool_succeeded = True
                        display_status = f"installed v{parsed.get('version', '?')}"
                    elif parsed.get("installed") is False:
                        tool_succeeded = True  # query succeeded, package just isn't there
                        display_status = "not installed"
                    elif "files" in parsed:
                        tool_succeeded = True
                        display_status = f"{parsed.get('count', '?')} files"
                    elif "content" in parsed:
                        tool_succeeded = True
                        display_status = f"{parsed.get('total_lines', '?')} lines"
                    elif "runs" in parsed:
                        # get_status with no run_id. An empty list is a valid
                        # answer -- nothing has been run yet.
                        tool_succeeded = True
                        display_status = f"{parsed.get('total', 0)} run(s)"
                    elif "nnodes" in parsed:
                        # get_resources has no status field; nnodes being
                        # present is what says it worked.
                        tool_succeeded = True
                        launcher = parsed.get("launcher") or "none"
                        display_status = (f"nodes={parsed['nnodes']} "
                                          f"ranks={parsed.get('ntasks', 1)} "
                                          f"launcher={launcher}")
                    else:
                        # unknown shape, call it a failure so nothing gets silently swallowed
                        tool_succeeded = False
                        display_status = "unknown response"
                except (json.JSONDecodeError, AttributeError):
                    if tool_result.lower().startswith(("unknown tool", "error")):
                        # Plain-text error responses (e.g. a hallucinated tool name) must
                        # not be marked successful just because they aren't JSON.
                        tool_succeeded = False
                        display_status = tool_result[:80]
                    else:
                        tool_succeeded = True  # raw text response, not an error
                        display_status = "done"

                engine_backend, engine_verified = _classify_engine_usage(
                    engine, tool_name, tool_args, tool_result)

                log_entry = {
                    "iteration": iteration + 1,
                    "tool": tool_name,
                    "args": tool_args,
                    "result": tool_result[:2000],
                    "succeeded": tool_succeeded,
                    "engine_backend": engine_backend,
                    "engine_verified": engine_verified,
                }
                exploration_log.append(log_entry)

                color = "green" if tool_succeeded else "red"
                console.print(f"[{color}][explorer] {tool_name} -> {display_status}[/{color}]")
                try:
                    _launch_cmd = json.loads(tool_result).get("launch_command")
                    if _launch_cmd:
                        console.print(f"[dim {color}][explorer]   $ {_launch_cmd}[/dim {color}]")
                except (json.JSONDecodeError, AttributeError):
                    pass
                if tool_name in ("write_file", "write_xml"):
                    # Print the server's warnings in full. They say whether the
                    # file looks like it uses the engine at all -- no Parsl app,
                    # no @task, no ADIOS2 stream -- so a structurally-wrong
                    # workflow is visible here rather than after a run that
                    # exits 0 having done nothing with the engine.
                    try:
                        r = json.loads(tool_result)
                        for _warning in r.get("warnings", []):
                            console.print(f"[yellow][explorer]   warning: {_warning}[/yellow]")
                        for _problem in r.get("errors", []):
                            console.print(f"[red][explorer]   {_problem}[/red]")
                    except (json.JSONDecodeError, AttributeError):
                        pass
                if engine_verified is False:
                    console.print(
                        f"[bold red][explorer] WARNING: the file written by "
                        f"'{tool_name}' does not look like it uses the real "
                        f"{engine} API (engine_backend={engine_backend})[/bold red]"
                    )

                # log the full result to trace, not truncated, need it for debugging failed runs
                tracer.log_tool_call("explorer", tool_name, tool_args,
                                     tool_result, tool_succeeded, iteration=iteration + 1,
                                     engine_backend=engine_backend, engine_verified=engine_verified)

                messages.append(ToolMessage(
                    content=tool_result[:_MAX_TOOL_RESULT_CHARS],
                    tool_call_id=tool_id,
                ))

                # If MCP connection is broken, stop the tool loop
                if mcp_broken:
                    break

            # If MCP connection is broken, stop the iteration loop
            if mcp_broken:
                console.print("[bold yellow][explorer] MCP connection lost -- ending with partial results[/bold yellow]")
                break

        else:
            console.print("[bold red][explorer] hit max iterations limit[/bold red]")

        # Cleanup MCP server
        console.print("[dim cyan][explorer] cleaning up MCP server...[/dim cyan]")
        try:
            await session.call_tool("cleanup", {})
        except Exception:
            pass

        _mcp_session = None

        # Build summary
        total_calls = len(exploration_log)
        successes = sum(1 for e in exploration_log if e.get("succeeded", False))
        failures = total_calls - successes

        summary = (
            f"Explorer completed: {total_calls} tool calls, "
            f"{successes} succeeded, {failures} failed, "
            f"{iteration + 1} iterations"
        )
        console.print(f"[dim cyan][explorer] {summary}[/dim cyan]")

        # record what actually got produced, so scoring can read straight from the
        # trace instead of re-deriving it from tool-call text
        work_dir = _work_dir()
        artifacts = []
        if os.path.isdir(work_dir):
            artifacts = [{"path": full, "size_bytes": size}
                         for full, size in _walk_artifacts(work_dir)]
        tracer.log_artifact_manifest(artifacts)

        tracer.log_agent_output("explorer", {
            "total_tool_calls": total_calls,
            "successes": successes,
            "failures": failures,
            "iterations": iteration + 1,
        })

        tracer.log_agent_end("explorer")

        # The orchestrator may route back here for another pass, so the post-run
        # review does NOT happen inline -- it would prompt the user once per pass.
        # It runs once after the graph finishes, via post_run_review(). Carry the
        # closing summary forward so that review can read it back as evidence.
        return {
            "exploration_log": exploration_log,
            "current_step": "explorer_complete",
            "explorer_summary": final_message,
        }