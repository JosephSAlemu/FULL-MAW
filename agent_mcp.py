"""
MCP Approach -- Multi-Agent Workflow (Tool-Calling Mode)

This is the MCP approach entry point. Instead of generating a complete workflow script
(codegen) and executing it (executor), this approach uses an explorer agent that
interactively calls tools to execute each workflow task step by step in a local venv.

Pipeline:
    orchestrator -> planner -> installer -> explorer -> end

Usage:
    python agent_mcp.py --paper 1 --combination b --goal "Reproduce this workflow..."
    python agent_mcp.py --paper YildizO_RAPIDS.pdf --combination a --goal "..."

--combination is required: a=PDF+Image+Desc, b=PDF+Desc, c=Image+Desc, d=Desc Only
(labels the planner inputs actually used this run; see run_archiver.py)
"""

from common.consts import LITERATURE_PATH, SKILLS_PATH, DATA_PATH, RUN_PATH
from systemprompts.orchestrator import ORCHESTRATOR_SYSTEM_PROMPT, ORCHESTRATOR_SYSTEM_PROMPT_NO_SKILLS
from systemprompts.planner import PLANNER_PROMPT, PLANNER_PROMPT_NO_SKILLS
from systemprompts.context import PROJECT_LAYOUT
from common.exceptions import exception_message
import os
import base64
import json
import mimetypes
import operator
import argparse
import re
import subprocess
import time
import warnings
from datetime import datetime
from typing import Annotated, Literal, Sequence
from typing_extensions import TypedDict
from pydantic import BaseModel
from pypdf import PdfReader
from pathlib import Path
warnings.filterwarnings("ignore", module="pypdf")
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from langchain_openai import ChatOpenAI
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langgraph.graph import StateGraph, END
from langgraph.graph.message import add_messages

from mcp_explorer import explorer
from trace_logger import tracer, extract_usage, message_to_dict
from run_archiver import archive_run

load_dotenv()

console = Console()

_run_log_path: str = ""  # jsonl log of routing decisions, orchestrator writes to this


# __ LLM ______________________________________________________________________
model = ChatOpenAI(model=os.getenv("MODEL_NAME"), streaming=True, stream_usage=True)


# __ Agent State _______________________________________________________________

class AgentState(TypedDict):
    messages:              Annotated[Sequence[BaseMessage], add_messages]
    goal:                  str
    pdf_path:              str
    literature_findings:   list[str]
    stack_decision:        list[str]
    tasks:                 list[str]
    exploration_log:       list[dict]       # explorer output (tool call records)
    selected_data_files:   list[str]        # filenames chosen from data/ at startup
    requirements_content:  str
    requirements_approved: bool
    image_path:            str              # path to user-uploaded image for planning (optional)
    current_step:          str
    orchestrator_feedback: str
    next:                  str
    planner_revisions:     int
    installer_revisions:   int
    explorer_revisions:    int
    engine:                str              # workflow engine: "parsl", "pycompss", etc.
    env:                   str              # execution environment: "local" or "hpc"
    condition:              str             # ablation condition: "A" (no-skills), "B" (full), "C" (single-agent)
    domain:                str              # paper domain label, e.g. "cosmology"
                                             # used to look up use_cases/<domain>/* directly instead of keyword matching


# __ Pydantic Schemas __________________________________________________________

class OrchestratorOutput(BaseModel):
    reasoning:           str
    next:                  Literal["planner", "installer", "explorer", "end"]
    feedback:              str
    requirements_approved: bool = False
    skill_requests:        list[str] = []

class PlannerOutput(BaseModel):
    literature_findings: list[str]
    stack_decision:      list[str]
    tasks:               list[str]
    skill_requests:      list[str] = []

class InstallerOutput(BaseModel):
    requirements_content: str


# __ Project layout (injected into context) ____________________________________


HOST_REPO_PATH = os.environ.get("HOST_REPO_PATH", os.path.dirname(os.path.abspath(__file__)))


# __ Structured output helper __________________________________________________

def _invoke_structured(llm, schema, messages, agent_name, retries=5):
    """Call llm and parse the response as schema, tolerating preamble text before the JSON block."""
    import json as _json, re as _re
    last_err = None
    model_name = getattr(llm, "model_name", None) or os.getenv("MODEL_NAME", "")
    msg_dicts = [message_to_dict(m) for m in messages]
    for attempt in range(retries):
        _t0 = time.time()
        response = llm.invoke(messages)
        latency_s = round(time.time() - _t0, 2)
        text = response.content if hasattr(response, "content") else str(response)
        usage = extract_usage(response)
        tracer.log_llm_call(
            agent_name, model_name, msg_dicts, text,
            input_tokens=usage["input_tokens"], output_tokens=usage["output_tokens"],
            total_tokens=usage["total_tokens"], latency_s=latency_s, attempt=attempt + 1,
        )
        match = _re.search(r'\{.*\}', text, _re.DOTALL)
        if not match:
            last_err = ValueError(f"No JSON object found in model response:\n{text[:500]}")
            console.print(f"[yellow][_invoke_structured] attempt {attempt+1}: no JSON found, retrying...[/yellow]")
            continue
        try:
            return schema.model_validate(_json.loads(match.group(0)))
        except (_json.JSONDecodeError, Exception) as e:
            last_err = e
            console.print(f"[yellow][_invoke_structured] attempt {attempt+1}: parse error ({e}), retrying...[/yellow]")
    raise last_err


# __ Skill file helpers ________________________________________________________

_SKILLS_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "skills")

def _read_skill(rel_path: str, agent_name: str, enabled: bool = True) -> str:
    """Read skills/<rel_path>.SKILL.md -- returns '' if disabled (condition A) or not found.

    Condition A never touches the filesystem under skills/ at all -- not even an
    os.path.isfile check -- so disabled short-circuits before any I/O happens.
    """
    if not enabled:
        tracer.log_skill_load(agent_name, rel_path, found=False, suppressed=True)
        return ""
    full = os.path.join(_SKILLS_ROOT, rel_path + ".SKILL.md")
    found = os.path.isfile(full)
    tracer.log_skill_load(agent_name, rel_path, found, suppressed=False)
    if found:
        with open(full) as f:
            return f.read()
    return ""

_ENV_KNOWLEDGE = {
    "local": "knowledge/local",
    "hpc":   "knowledge/lcrc",
}

# __ Condition-A substitute for env knowledge ___________________________________
# B/C load the real knowledge/local|lcrc skill file, unchanged. A doesn't get that
# file, just these few bare facts about the machine, so it's not permanently stuck.
ENV_NOTES = {
    "local": (
        "Environment: a single local machine, no job scheduler. No PBS/LSF, no multi-node "
        "MPI. /app/ paths are resolved to the repo root at runtime."
    ),
    "hpc": (
        "Environment: an HPC cluster, presumably inside a job allocation. /app/ paths are "
        "resolved to the repo root at runtime; never hardcode cluster-specific absolute paths."
    ),
}

def _env_knowledge(env: str, agent_name: str, enabled: bool = True) -> str:
    """Return environment-knowledge content for the given env.

    enabled=True (condition B/C): reads the real knowledge/local|lcrc skill file, unchanged.
    enabled=False (condition A): the skill file is suppressed (logged as such); a short
    hardcoded ENV_NOTES substitute is returned instead of nothing.
    """
    skill_key = _ENV_KNOWLEDGE.get(env, "knowledge/local")
    if enabled:
        return _read_skill(skill_key, agent_name, enabled=True)
    # condition A: no filesystem touch, _read_skill(enabled=False) just logs it and bails
    _read_skill(skill_key, agent_name, enabled=False)
    return ENV_NOTES.get(env, ENV_NOTES["local"])

def _slugify(path: str) -> str:
    """Turn a file path into a stable run-metadata id, e.g. for paper_id."""
    base = os.path.splitext(os.path.basename(path))[0] if path else "no_paper"
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "_", base).strip("_")
    return slug or "no_paper"


def _list_skills(folder: str) -> list:
    """List available names in skills/<folder>/: subdirectory names AND .SKILL.md base names."""
    d = os.path.join(_SKILLS_ROOT, folder)
    if not os.path.isdir(d):
        return []
    results = []
    for name in os.listdir(d):
        if os.path.isdir(os.path.join(d, name)):
            results.append(name)
        elif name.endswith(".SKILL.md"):
            results.append(os.path.splitext(name)[0])
    return results


# __ Nodes _____________________________________________________________________

# __ Orchestrator _______________________________________________________________

def orchestrator(state: AgentState) -> dict:
    console.print("\n[dim cyan][orchestrator] reviewing state...[/dim cyan]")
    tracer.log_agent_start("orchestrator")         ########### insert TRACER ##########
    tracer.log_agent_input("orchestrator", {       ########### insert TRACER ##########
        "current_step": state.get("current_step", ""),
        "goal": state.get("goal", ""),
        "has_findings": bool(state.get("literature_findings")),
        "has_tasks": bool(state.get("tasks")),
        "has_exploration_log": bool(state.get("exploration_log")),
    })

    revisions = {
        "planner":   state.get("planner_revisions",   0),
        "installer": state.get("installer_revisions", 0),
        "explorer":  state.get("explorer_revisions",  0),
    }
    if any(revisions.values()):
        rev_str = " | ".join(f"{k}: {v}" for k, v in revisions.items() if v > 0)
        console.print(f"[dim yellow][orchestrator] revision counts -- {rev_str}[/dim yellow]")

    parts = [f"Goal: {state['goal']}", f"Current step: {state['current_step']}"]
    if any(revisions.values()):
        parts.append("Revision counts so far: " +
                     ", ".join(f"{k}={v}" for k, v in revisions.items()))
    if state.get("literature_findings"):
        parts.append(f"Literature findings ({len(state['literature_findings'])} items):\n" +
                     "\n".join(f"  - {f}" for f in state["literature_findings"]))
    if state.get("stack_decision"):
        parts.append(f"Stack: {state['stack_decision']}")
    if state.get("tasks"):
        parts.append(f"Tasks ({len(state['tasks'])}):\n" +
                     "\n".join(f"  {i+1}. {t}" for i, t in enumerate(state["tasks"])))
    if state.get("requirements_content"):
        parts.append(f"requirements.txt:\n{state['requirements_content']}")
    if state.get("exploration_log"):
        # Summarize exploration log for orchestrator
        log = state["exploration_log"]
        total = len(log)
        successes = sum(1 for e in log if e.get("succeeded", False))
        failures = total - successes
        parts.append(f"Exploration: {total} tool calls, {successes} succeeded, {failures} failed")
        # Include last few entries for detail
        for entry in log[-5:]:
            status_str = "OK" if entry.get("succeeded", False) else "FAILED"
            parts.append(f"  [{entry['tool']}] {status_str} - {entry.get('result', '')[:200]}")
        # engine_verified comes from mcp_explorer.py's _classify_engine_usage. False means
        # a submit_task/submit_shell_task/submit_mpi_task call didn't actually hit the real engine
        unverified = [e for e in log if e.get("engine_verified") is False]
        if unverified:
            parts.append(
                f"ENGINE USAGE WARNING: {len(unverified)} of {total} tool call(s) did not "
                f"demonstrably exercise the real {state.get('engine')} API/runtime "
                f"(fallback path or no real API call detected): " +
                "; ".join(
                    f"{e['tool']}({e.get('args', {}).get('name', '?')}, "
                    f"engine_backend={e.get('engine_backend')})"
                    for e in unverified[:5]
                )
            )

    # system prompt = base skill + env knowledge + skill index + core prompt.
    # condition A: _base comes back empty, so fall back to ORCHESTRATOR_SYSTEM_PROMPT_NO_SKILLS
    _enabled = state.get("condition", "B") != "A"
    _base = _read_skill("agents/orchestrator", "orchestrator", enabled=_enabled)
    _env_kn = _env_knowledge(state.get("env", "local"), "orchestrator", enabled=_enabled)
    _env_section = f"\n\n=== Environment Knowledge ===\n{_env_kn}" if _env_kn else ""

    if _base:
        _uc = _list_skills("use_cases")
        _sys = _list_skills("systems")
        _index = (f"\n\nAvailable skill contexts (set in skill_requests to load):"
                  f"\n  use_cases: {_uc}  -- request as \"use_cases/<name>/orchestrator\""
                  f"\n  systems:   {_sys}  -- request as \"systems/<name>\"") if (_uc or _sys) else ""
        _sys_prompt = _base + _env_section + _index + "\n\n---\n\n" + ORCHESTRATOR_SYSTEM_PROMPT + "\n\n" + PROJECT_LAYOUT
    else:
        _sys_prompt = ORCHESTRATOR_SYSTEM_PROMPT_NO_SKILLS + _env_section + "\n\n" + PROJECT_LAYOUT
    _human = "\n\n".join(parts)

    result: OrchestratorOutput = _invoke_structured(model, OrchestratorOutput, [
        SystemMessage(content=_sys_prompt),
        HumanMessage(content=_human),
    ], "orchestrator")

    # Two-pass: if sub-skills requested, load them and re-invoke once
    if result.skill_requests:
        _sub = "\n\n".join(filter(None, (_read_skill(r, "orchestrator", enabled=_enabled) for r in result.skill_requests)))
        if _sub:
            _enriched = _sys_prompt + f"\n\n=== Loaded Skills ===\n{_sub}\n\n(Final pass -- do not set skill_requests.)"
            result = _invoke_structured(model, OrchestratorOutput, [
                SystemMessage(content=_enriched),
                HumanMessage(content=_human),
            ], "orchestrator")

    # Hard overrides: lock routing at deterministic transition points
    if state.get("current_step") == "installer_requirements_pending_approval":
        result.next = "installer"
    elif state.get("current_step") == "installer_complete":
        result.next = "explorer"
    elif state.get("current_step") == "explorer_complete":
        # After explorer completes, check if key outputs exist before deciding to re-run.
        # If the explorer produced results (even partial), prefer ending over re-running.
        log = state.get("exploration_log", [])
        successes = sum(1 for e in log if e.get("succeeded", False))
        if successes > 0 and result.next == "explorer":
            # explorer already ran and got results, need a good reason to run it again
            explorer_revs = state.get("explorer_revisions", 0)
            if explorer_revs >= 1:
                console.print("[dim yellow][orchestrator] explorer already revised once with results -- ending[/dim yellow]")
                result.next = "end"

    panel_body = f"[bold]Routing to:[/bold] [green]{result.next}[/green]\n\n[bold]Reasoning:[/bold]\n{result.reasoning}"
    if result.feedback:
        panel_body += f"\n\n[bold]Feedback to {result.next}:[/bold]\n[yellow]{result.feedback}[/yellow]"
    console.print(Panel(panel_body, title="[bold cyan]Orchestrator Decision[/bold cyan]", border_style="cyan"))

    # Only wriiten in orchestrator, planner/installer don't use it 
    if _run_log_path:
        with open(_run_log_path, "a") as _lf:
            _lf.write(json.dumps({
                "ts":         datetime.now().isoformat(),
                "from_step":  state.get("current_step"),
                "routing_to": result.next,
                "revisions":  revisions,
                "feedback":   result.feedback,
                "reasoning":  result.reasoning[:500],
            }) + "\n")

    already_ran = {
        "planner":   bool(state.get("literature_findings")),
        "installer": bool(state.get("requirements_content")),
        "explorer":  bool(state.get("exploration_log")),
    }
    revision_update = {}
    if result.next in already_ran and already_ran[result.next]:
        key = f"{result.next}_revisions"
        revision_update[key] = state.get(key, 0) + 1
        console.print(f"[bold yellow][orchestrator] revision #{revision_update[key]} for {result.next}[/bold yellow]")
        tracer.log_replan(result.next, revision_update[key])

    state_update = {
        "next":                  result.next,
        "orchestrator_feedback": result.feedback,
        "requirements_approved": result.requirements_approved,
        "current_step":          f"orchestrator_routed_to_{result.next}",
        **revision_update,
    }

    tracer.log_routing("orchestrator", result.next, result.reasoning, result.feedback,                   ############# insert TRACER #############
                        payload_size=len(json.dumps(state_update, default=str)))
    tracer.log_agent_output("orchestrator", {"next": result.next, "feedback": result.feedback})          ############# insert TRACER #############
    tracer.log_agent_end("orchestrator")                                                                 ############# TRACER ENDS   #############

    return state_update


# __ Planner ____________________________________________________________________
def planner(state: AgentState) -> dict:
    try:
        console.print("\n[dim cyan][planner] reading PDF...[/dim cyan]")
        tracer.log_agent_start("planner")
        tracer.log_agent_input("planner", {"pdf_path": state["pdf_path"], "goal": state["goal"]})

        reader = PdfReader(state["pdf_path"])
        pdf_text = "\n".join(page.extract_text() for page in reader.pages if page.extract_text())
        console.print(f"[dim cyan][planner] loaded {len(reader.pages)} pages[/dim cyan]")

        feedback = state.get("orchestrator_feedback", "")
        feedback_section = (f"\n\nOrchestrator feedback -- address these issues before returning:\n{feedback}"
                            if feedback else "")

        # system prompt = base skill + env knowledge + skill index + core prompt.
        # condition A: _base comes back empty, so fall back to PLANNER_PROMPT_NO_SKILLS
        _enabled = state.get("condition", "B") != "A"
        _base = _read_skill("agents/planner", "planner", enabled=_enabled)
        _env_kn = _env_knowledge(state.get("env", "local"), "planner", enabled=_enabled)
        _env_section = f"\n\n=== Environment Knowledge ===\n{_env_kn}" if _env_kn else ""

        if _base:
            _uc = _list_skills("use_cases")
            _sys = _list_skills("systems")
            _index = (f"\n\nAvailable skill contexts (set in skill_requests to load):"
                      f"\n  use_cases: {_uc}  -- request as \"use_cases/<name>/planner\""
                      f"\n  systems:   {_sys}  -- request as \"systems/<name>\"") if (_uc or _sys) else ""
            _sys_prompt = _base + _env_section + _index + "\n\n---\n\n" + PLANNER_PROMPT + "\n\n" + PROJECT_LAYOUT
        else:
            _sys_prompt = PLANNER_PROMPT_NO_SKILLS + _env_section + "\n\n" + PROJECT_LAYOUT
        _data_files = state.get("selected_data_files", [])
        _data_section = (f"\n\nAvailable input data files (in /app/data/):\n" +
                         "\n".join(f"  - {f}" for f in _data_files)) if _data_files else ""
        _human = f"Goal: {state['goal']}{_data_section}\n\nPaper:\n{pdf_text}{feedback_section}"

        # multimodal message if an image got passed in
        if state.get("image_path"):
            console.print("\n[dim cyan][planner] loading image...[/dim cyan]")
            with open(state["image_path"], "rb") as _f:
                _img_bytes = _f.read()
            _b64 = base64.b64encode(_img_bytes).decode()
            _mime, _ = mimetypes.guess_type(state["image_path"])
            _mime = _mime or "image/png"
            _human_msg = HumanMessage(content=[
                {"type": "image_url", "image_url": {"url": f"data:{_mime};base64,{_b64}"}},
                {"type": "text", "text": _human},
            ])
            console.print(f"[dim cyan][planner] image loaded ({_mime}, {len(_img_bytes)//1024}KB)[/dim cyan]")
        else:
            _human_msg = HumanMessage(content=_human)

        result: PlannerOutput = _invoke_structured(model, PlannerOutput, [
            SystemMessage(content=_sys_prompt),
            _human_msg,
        ], "planner")

        # Two-pass: if sub-skills requested, load them and re-invoke once
        if result.skill_requests:
            _sub = "\n\n".join(filter(None, (_read_skill(r, "planner", enabled=_enabled) for r in result.skill_requests)))
            if _sub:
                _enriched = _sys_prompt + f"\n\n=== Loaded Skills ===\n{_sub}\n\n(Final pass -- do not set skill_requests.)"
                result = _invoke_structured(model, PlannerOutput, [
                    SystemMessage(content=_enriched),
                    _human_msg,
                ], "planner")

        # force adios2 into the stack for this engine, don't leave it up to the model.
        # without it every task just falls back to numpy I/O and adios2 never actually runs
        if state.get("engine") == "adios" and "adios2" not in result.stack_decision:
            result.stack_decision.append("adios2")

        console.print(f"[dim cyan][planner] produced {len(result.tasks)} tasks[/dim cyan]")

        findings = "\n".join(f"  [cyan]*[/cyan] {f}" for f in result.literature_findings)
        stack    = "\n".join(f"  [cyan]*[/cyan] {s}" for s in result.stack_decision)
        tasks    = "\n".join(f"  [bold]{i+1}.[/bold] {t}" for i, t in enumerate(result.tasks))
        console.print(Panel(
            f"[bold]Literature Findings[/bold]\n{findings}\n\n"
            f"[bold]Stack[/bold]\n{stack}\n\n"
            f"[bold]Tasks[/bold]\n{tasks}",
            title="[bold green]Planner Output[/bold green]",
            border_style="green",
        ))

        tracer.log_agent_output("planner", {
            "findings_count": len(result.literature_findings),
            "stack": result.stack_decision,
            "tasks_count": len(result.tasks),
            "tasks_preview": result.tasks[:3],
        })
        tracer.log_agent_end("planner")

        return {
            "literature_findings": result.literature_findings,
            "stack_decision":      result.stack_decision,
            "tasks":               result.tasks,
            "current_step":        "planner_complete",
        }
    except Exception as e:
        console.print(f"[red][planner] ERROR: {e}[/red]")
        raise

# __ Installer __________________________________________________________________

def installer(state: AgentState) -> dict:
    import sys
    try:
        tracer.log_agent_start("installer")
        build_dir         = os.path.join(os.path.dirname(os.path.abspath(__file__)), "builds")
        os.makedirs(build_dir, exist_ok=True)
        requirements_path = os.path.join(build_dir, "requirements.txt")

        if state.get("requirements_approved"):
            # __ Phase 2: approved, pip install into the venv __
            with open(requirements_path) as f:
                packages = [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]

            if not packages:
                console.print("[dim cyan][installer] no packages to install[/dim cyan]")
                tracer.log_agent_output("installer", {"status": "no packages"})
                tracer.log_agent_end("installer")
                return {"current_step": "installer_complete"}

            console.print(f"[dim cyan][installer] pip installing {len(packages)} packages...[/dim cyan]")
            proc = subprocess.run(
                [sys.executable, "-m", "pip", "install"] + packages,
                capture_output=True, text=True, timeout=600,
            )
            if proc.returncode != 0:
                console.print(f"[yellow][installer] pip stderr:\n{proc.stderr[-2000:]}[/yellow]")
            else:
                console.print("[dim cyan][installer] packages ready[/dim cyan]")

            tracer.log_agent_output("installer", {"status": "packages installed", "count": len(packages)})
            tracer.log_agent_end("installer")
            return {"current_step": "installer_complete"}

        else:
            # __ Phase 1: read or generate requirements.txt, send to orchestrator for approval __
            feedback = state.get("orchestrator_feedback", "")
            stack = state.get("stack_decision", [])

            existing_content = None
            if os.path.isfile(requirements_path):
                with open(requirements_path) as f:
                    existing_content = f.read()
                existing_packages = {
                    ln.strip() for ln in existing_content.splitlines()
                    if ln.strip() and not ln.startswith("#")
                }
                stale = bool(stack) and existing_packages != set(stack)
            else:
                stale = False

            # Regenerate from stack_decision if the orchestrator rejected the previous
            # requirements, OR if the file on disk doesn't match this run's stack_decision
            # (e.g. left over from a prior run with a different engine).
            if (feedback and stack) or stale:
                reason = (
                    "orchestrator rejected previous requirements"
                    if feedback else
                    "on-disk requirements.txt is stale (doesn't match current stack_decision)"
                )
                console.print(f"[dim cyan][installer] {reason} -- regenerating from stack_decision...[/dim cyan]")
                content = "\n".join(pkg for pkg in stack if pkg) + "\n"
                with open(requirements_path, "w") as f:
                    f.write(content)
                console.print(f"[dim cyan][installer] regenerated requirements.txt with {len(stack)} packages[/dim cyan]")
            elif existing_content is not None:
                console.print("[dim cyan][installer] reading existing requirements.txt (matches stack_decision)...[/dim cyan]")
                content = existing_content
            else:
                console.print("[dim cyan][installer] generating requirements.txt from stack_decision...[/dim cyan]")
                content = "\n".join(pkg for pkg in stack if pkg) + "\n"
                with open(requirements_path, "w") as f:
                    f.write(content)
                console.print(f"[dim cyan][installer] generated requirements.txt with {len(stack)} packages[/dim cyan]")

            console.print(Panel(
                content,
                title="[bold yellow]requirements.txt -- Pending Orchestrator Approval[/bold yellow]",
                border_style="yellow",
            ))
            console.print("[dim yellow][installer] waiting for orchestrator approval...[/dim yellow]")

            tracer.log_agent_output("installer", {"status": "pending approval", "packages": content.strip()})
            tracer.log_agent_end("installer")
            return {
                "requirements_content": content,
                "current_step":         "installer_requirements_pending_approval",
            }

    except Exception as e:
        console.print(f"[red][installer] ERROR: {e}[/red]")
        raise

# __ Graph _____________________________________________________________________

def route_orchestrator(state: AgentState) -> str:
    return state["next"]


graph = StateGraph(AgentState)

graph.add_node("orchestrator", orchestrator)
graph.add_node("planner",      planner)
graph.add_node("installer",    installer)
graph.add_node("explorer",     explorer)

graph.set_entry_point("orchestrator")

graph.add_conditional_edges("orchestrator", route_orchestrator, {
    "planner":   "planner",
    "installer": "installer",
    "explorer":  "explorer",
    "end":       END,
})

graph.add_edge("planner",   "orchestrator")
graph.add_edge("installer", "orchestrator")
graph.add_edge("explorer",  "orchestrator")

app = graph.compile()


# __ Run _______________________________________________________________________

def user_input():
    parser = argparse.ArgumentParser(description="MAW -- Multi-Agent Workflow (MCP Approach)")
    parser.add_argument("--paper", type=int, help="Path to the PDF paper or paper index (1-based)")
    parser.add_argument("--image", type=str, help="Path to image file (diagram/figure) to use for planning")
    parser.add_argument("--goal", type=str, help="Goal for the workflow")
    parser.add_argument("--engine", type=str, default="parsl",
                        choices=["parsl", "pycompss", "adios"],
                        help="Workflow engine to use (default: parsl)")
    parser.add_argument("--env", type=str, default="local",
                        choices=["local", "hpc"],
                        help="Execution environment: local (default) or hpc (LCRC/PBS)")
    parser.add_argument("--condition", type=str, default="B",
                        choices=["A", "B", "C"],
                        help="Ablation condition: A=no-skills, B=full (default), C=single-agent")
    parser.add_argument("--trial", type=int, default=1,
                        help="Trial number for repeated (paper, condition) runs (default: 1)")
    parser.add_argument("--domain", type=str, default="",
                        help="Paper domain label, e.g. molecular_nucleation (optional)")
    parser.add_argument("--combination", type=str, required=True,
                        choices=["a", "b", "c", "d"],
                        help="Planner input combination: a=PDF+Image+Desc, "
                             "b=PDF+Desc, c=Image+Desc, d=Desc Only")
    

    return parser.parse_args()


if __name__ == "__main__":

    # look at user_input to see valid arguments
    args = user_input()

    console.print(Panel(f"[bold blue]MAW -- Multi-Agent Workflow (MCP Approach)[/bold blue]\n[dim]Engine: {args.engine} | Env: {args.env}[/dim]", border_style="blue"))

    pdfs = [os.path.join(LITERATURE_PATH, pdf) for pdf in os.listdir(LITERATURE_PATH) if pdf.lower().endswith(".pdf")]

    if not pdfs:
        exception_message("No PDFs found in the Literature/ folder. Add a paper and try again.")

    console.print("\n[bold]Available papers:[/bold]")
    for i, name in enumerate(pdfs, 1):
        console.print(f"  {i}. {os.path.basename(name)}")

    # Handle paper selection
    paper = None
    if args.paper and 0 <= args.paper < len(pdfs):
        paper = args.paper-1

    while paper is None:
        choice = input("\nSelect a paper by number: ").strip()
        
        if 0 <= choice < len(pdfs):
            paper = choice-1
        else:
            console.print("[red]Invalid selection. Choose a proper paper[/red]")
    pdf_path = pdfs[paper]
    console.print(f"[dim]Selected: {os.path.basename(pdf_path)}[/dim]")

    # Handle image selection
    _IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
    images_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "images")
    os.makedirs(images_dir, exist_ok=True)

    if args.image:
        image_path = args.image if os.path.isabs(args.image) else os.path.join(images_dir, args.image)
        if not os.path.isfile(image_path):
            console.print(f"[red]Image not found: {args.image}[/red]")
            raise SystemExit(1)
        console.print(f"[dim]Image: {os.path.basename(image_path)}[/dim]")
    else:
        images = sorted(
            f for f in os.listdir(images_dir)
            if os.path.splitext(f)[1].lower() in _IMAGE_EXTS
        )
        if not images:
            image_path = ""
        else:
            console.print("\n[bold]Available images:[/bold]")
            for i, name in enumerate(images, 1):
                console.print(f"  {i}. {name}")
            console.print(f"  0. Skip -- no image")
            choice = input("\nSelect an image by number (or 0 to skip): ").strip()
            if choice == "0" or not choice:
                image_path = ""
                console.print("[dim]No image selected.[/dim]")
            else:
                try:
                    image_path = os.path.join(images_dir, images[int(choice) - 1])
                    console.print(f"[dim]Selected: {os.path.basename(image_path)}[/dim]")
                except (ValueError, IndexError):
                    console.print("[red]Invalid selection -- no image will be used.[/red]")
                    image_path = ""

    # Handle data file selection
    data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
    all_data_files = sorted(f for f in os.listdir(data_dir) if os.path.isfile(os.path.join(data_dir, f))) if os.path.isdir(data_dir) else []

    if not all_data_files:
        console.print("[yellow]No files found in data/ -- agents will have no input data.[/yellow]")
        selected_data_files = []
    else:
        console.print("\n[bold]Available data files:[/bold]")
        for i, name in enumerate(all_data_files, 1):
            console.print(f"  {i}. {name}")
        raw = input("\nSelect files by number (comma-separated, or Enter for all): ").strip()
        if not raw:
            selected_data_files = all_data_files
            console.print("[dim]Using all data files.[/dim]")
        else:
            selected_data_files = []
            for token in raw.split(","):
                token = token.strip()
                try:
                    selected_data_files.append(all_data_files[int(token) - 1])
                except (ValueError, IndexError):
                    console.print(f"[yellow]Skipping invalid selection: {token!r}[/yellow]")
            if not selected_data_files:
                console.print("[yellow]No valid files selected -- using all.[/yellow]")
                selected_data_files = all_data_files
            else:
                console.print(f"[dim]Selected: {', '.join(selected_data_files)}[/dim]")

    # Handle goal
    if args.goal:
        goal = args.goal
    else:
        goal = input("\nDescribe your goal for this workflow: ").strip()

    if not goal:
        console.print("[red]Goal cannot be empty.[/red]")
        raise SystemExit(1)

    os.makedirs(RUN_PATH, exist_ok=True)
    _run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    _run_log_path = os.path.join(RUN_PATH, _run_id + ".jsonl")
    console.print(f"[dim]Run log: {_run_log_path}[/dim]")

    initial_state = {
        "messages":              [],
        "goal":                  goal,
        "pdf_path":              pdf_path,
        "literature_findings":   [],
        "stack_decision":        [],
        "tasks":                 [],
        "exploration_log":       [],
        "selected_data_files":   selected_data_files,
        "requirements_content":  "",
        "requirements_approved": False,
        "image_path":            image_path,
        "current_step":          "start",
        "orchestrator_feedback": "",
        "next":                  "",
        "planner_revisions":     0,
        "installer_revisions":   0,
        "explorer_revisions":    0,
        "engine":                args.engine,
        "env":                   args.env,
        "condition":             args.condition,
        "domain":                args.domain,
    }

    trace_path = os.path.join(RUN_PATH, _run_id + "_trace.json")

    tracer.reset()   # brefor you run it, reset the tracer to clear any previous runs from the dashboard
    tracer.start_run(
        run_id=_run_id,
        condition=args.condition,
        combination=args.combination,
        trial=args.trial,
        paper_id=_slugify(pdf_path) if pdf_path else "no_paper",
        paper_path=pdf_path,
        domain=args.domain,
        framework=args.engine,
        env=args.env,
        goal=goal,
        model=os.getenv("MODEL_NAME", "claudeopus48"),
    )

    try:
        app.invoke(initial_state) # write the node functions to log to the tracer, and then invoke the graph with the initial state
        tracer.finalize_run("completed")
    except Exception as e:
        import traceback as _traceback
        tracer.log_run_error(type(e).__name__, str(e), _traceback.format_exc())
        tracer.finalize_run("failed")
        tracer.save(trace_path)
        console.print(f"[red]Run failed: {e}[/red]")
        console.print(f"[dim]Trace saved (partial): {trace_path}[/dim]")
        archive_path = archive_run(tracer.run_metadata, trace_path)
        if archive_path:
            console.print(f"[dim]Run archived (failed): {archive_path}[/dim]")
        raise

    # Save trace
    tracer.save(trace_path)  #save the trace to a file for later analysis
    console.print(f"[dim]Trace saved: {trace_path}[/dim]")

    archive_path = archive_run(tracer.run_metadata, trace_path)
    if archive_path:
        console.print(f"[dim]Run archived: {archive_path}[/dim]")

    # Print interaction summary
    summary = tracer.get_summary()
    console.print(f"\n[bold]Trace Summary:[/bold]")
    console.print(f"  Agents: {', '.join(summary['agents_involved'])}")
    console.print(f"  Routing path: {' -> '.join(summary['routing_path'])}")
    console.print(f"  Tool calls: {summary['tool_calls']} ({summary['tool_successes']} succeeded)")
    console.print(f"  Total time: {summary['total_duration_s']}s")

    # token usage summary, full numbers are in runs/xxxxxxxx_trace.json
    # Print token usage
    total_in = summary.get('total_input_tokens', 0)
    total_out = summary.get('total_output_tokens', 0)
    if total_in or total_out:
        console.print(f"\n[bold]Token Usage:[/bold]")
        console.print(f"  Input tokens:  {total_in:,}")
        console.print(f"  Output tokens: {total_out:,}")
        console.print(f"  Total tokens:  {total_in + total_out:,}")
        tokens_by_agent = summary.get('tokens_by_agent', {})
        if tokens_by_agent:
            console.print(f"  By agent:")
            for agent, count in sorted(tokens_by_agent.items()):
                console.print(f"    {agent}: {count:,}")

