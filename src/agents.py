import asyncio
import json
import subprocess
import sys
from datetime import datetime

from academy.agent import Agent, action, loop
from academy.handle import Handle
from academy.exchange import LocalExchangeFactory
from academy.manager import Manager
from langchain_core.tools import Tool
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

from langchain.tools import tool
from langchain_openai import ChatOpenAI
from langchain.agents import create_agent
from langgraph.graph import StateGraph, END
from rich.console import Console
from rich.panel import Panel


import mcp_explorer
from mcp_explorer import _explorer_async
from trace_logger import tracer, extract_usage, message_to_dict
from pypdf import PdfReader
from src.states import PlannerOutput, InstallerOutput, OrchestratorOutput, AgentState, AgentSteps
from src.utility import _list_skills, _env_knowledge, _invoke_structured, _read_skill
from common.consts import BUILDS_PATH
from systemprompts.orchestrator import ORCHESTRATOR_SYSTEM_PROMPT, ORCHESTRATOR_SYSTEM_PROMPT_NO_SKILLS
from systemprompts.planner import PLANNER_PROMPT, PLANNER_PROMPT_NO_SKILLS
from systemprompts.context import PROJECT_LAYOUT

import os
import base64
import mimetypes

console = Console()

_PDF_TEXT_CACHE: dict[tuple, str] = {}


def _extract_pdf_text(pdf_path: str) -> str:
    """Extract and cache a paper's text, keyed on (path, mtime, size).

    The orchestrator can route back to the planner, and each of those passes
    re-parsed the whole PDF with pypdf to produce byte-identical text. Keying
    on mtime and size rather than path alone means editing or swapping the
    paper mid-session still re-parses.
    """
    try:
        stat = os.stat(pdf_path)
        key = (os.path.abspath(pdf_path), stat.st_mtime_ns, stat.st_size)
    except OSError:
        key = None

    if key is not None and key in _PDF_TEXT_CACHE:
        console.print("[dim cyan][planner] reusing cached PDF text[/dim cyan]")
        return _PDF_TEXT_CACHE[key]

    reader = PdfReader(pdf_path)
    # Extract once per page, then filter. The original
    #   "\n".join(p.extract_text() for p in reader.pages if p.extract_text())
    # ran extract_text TWICE on every page -- once for the `if`, once for the
    # value -- and extract_text is the expensive part, not the iteration. The
    # output is identical either way; the filter only drops empty pages, which
    # the comprehension below does from the already-extracted text.
    pages = [page.extract_text() for page in reader.pages]
    text = "\n".join(t for t in pages if t)
    console.print(f"[dim cyan][planner] loaded {len(reader.pages)} pages[/dim cyan]")
    if key is not None:
        _PDF_TEXT_CACHE[key] = text
    return text


# __ Remote-agent output ______________________________________________________
# Under --env multi-node an agent runs in another process on another node, so
# it has neither this terminal nor this process's tracer. Each such agent is
# launched with a log_path and a trace_path on the shared filesystem: stdout
# goes to the log (academy_agents.follow() prints it here as it grows) and the
# tracer events to the JSONL trace.
#
# Redirection is per *process*, not per action, because the agents sharing a
# node share one Parsl worker process and one event loop. Three agents each
# wrapping their own action in redirect_stdout would nest and interleave
# redirects on the same file; doing it once at startup cannot.

_redirected_to: str = ""


def _redirect_process_stdout(log_path: str) -> None:
    """Point this process's stdout at log_path. Idempotent, and a no-op for "".

    Line-buffered on purpose: follow() reads the file while it is still open,
    so a block-buffered stream would show nothing until the process exited.
    """
    global _redirected_to
    if not log_path or _redirected_to == log_path:
        return
    os.makedirs(os.path.dirname(os.path.abspath(log_path)), exist_ok=True)
    sys.stdout = open(log_path, "a", buffering=1)
    _redirected_to = log_path


def _dump_trace(trace_path: str) -> None:
    """Write this process's tracer events to trace_path as JSONL.

    Rewritten in full rather than appended to: the orchestrator can route back
    to an agent, and the tracer still holds every event from earlier passes.
    Best effort -- losing the side-car trace must not fail a finished run.
    """
    if not trace_path:
        return
    try:
        os.makedirs(os.path.dirname(os.path.abspath(trace_path)), exist_ok=True)
        with open(trace_path, "w") as f:
            for event in tracer.get_trace():
                f.write(json.dumps(event, default=str) + "\n")
    except OSError as e:
        console.print(f"[yellow][agents] could not write trace file: {e}[/yellow]")


class RemoteLoggingAgent(Agent):
    """Agent that sends its output to files when it runs off the launching node.

    log_path and trace_path are "" under --env local and --env hpc, where the
    agent is in the launching process and prints to its terminal directly, and
    both methods below become no-ops.
    """

    log_path: str = ""
    trace_path: str = ""

    async def agent_on_startup(self) -> None:
        _redirect_process_stdout(self.log_path)

    def _flush_trace(self) -> None:
        _dump_trace(self.trace_path)


class Planner(RemoteLoggingAgent):
    def __init__(self, model=None, log_path: str = "", trace_path: str = ""):
        self.model = model
        self.log_path = log_path
        self.trace_path = trace_path

    @action
    async def set_model_config(self, model: str, base_url: str, api_key: str) -> None:
        # Strings, not a built ChatOpenAI: over the HTTP exchange the object
        # has to serialize, and the httpx client inside it will not pickle.
        self.model = ChatOpenAI(
            model=model, api_key=api_key, base_url=base_url,
            streaming=True, stream_usage=True,
        )

    @action
    async def planner(self, state: AgentState) -> dict:
        try:
            console.print("\n[dim cyan][planner] reading PDF...[/dim cyan]")
            tracer.log_agent_start("planner")
            tracer.log_agent_input("planner", {"pdf_path": state["pdf_path"], "goal": state["goal"]})

            pdf_text = _extract_pdf_text(state["pdf_path"])

            feedback = state.get("orchestrator_feedback", "")
            feedback_section = (f"\n\nOrchestrator feedback -- address these issues before returning:\n{feedback}"
                                if feedback else "")

            # system prompt = base skill + env knowledge + skill index + core prompt.
            # condition A: _base comes back empty, so fall back to PLANNER_PROMPT_NO_SKILLS
            _enabled = state.get("condition", "B") != "A"
            _base = await _read_skill("agents/planner", "planner", enabled=_enabled)
            _env_kn = await _env_knowledge(state.get("env", "local"), "planner", enabled=_enabled)
            _env_section = f"\n\n=== Environment Knowledge ===\n{_env_kn}" if _env_kn else ""

            if _base:
                _uc = await _list_skills("use_cases")
                _sys = await _list_skills("systems")
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

            result: PlannerOutput = await _invoke_structured(self.model, PlannerOutput, [
                SystemMessage(content=_sys_prompt),
                _human_msg,
            ], "planner")

            # Two-pass: if sub-skills requested, load them and re-invoke once
            if result.skill_requests:
                _sub = "\n\n".join(filter(None, [await _read_skill(r, "planner", enabled=_enabled) for r in result.skill_requests]))
                if _sub:
                    _enriched = _sys_prompt + f"\n\n=== Loaded Skills ===\n{_sub}\n\n(Final pass -- do not set skill_requests.)"
                    result = await _invoke_structured(self.model, PlannerOutput, [
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
                "current_step":        AgentSteps.PLANNER_COMPLETE,
            }
        except Exception as e:
            console.print(f"[red][planner] ERROR: {e}[/red]")
            raise


class Installer(RemoteLoggingAgent):
    def __init__(self, model=None, log_path: str = "", trace_path: str = ""):
        self.model = model
        self.log_path = log_path
        self.trace_path = trace_path

    @action
    async def set_model_config(self, model: str, base_url: str, api_key: str) -> None:
        self.model = ChatOpenAI(
            model=model, api_key=api_key, base_url=base_url,
            streaming=True, stream_usage=True,
        )

    @action
    async def installer(self, state: AgentState) -> dict:
        try:
            tracer.log_agent_start("installer")
            # BUILDS_PATH, not __file__-relative: this module moved into src/,
            # so "the directory holding this file" is now src/ and the
            # requirements would land in src/builds/. Everything else that
            # refers to this file -- the installer skills, PROJECT_LAYOUT, the
            # README, and the explorer resolving /app/builds/ -- means the
            # repo-root builds/, so writing anywhere else leaves the explorer
            # reinstalling packages the installer already handled.
            os.makedirs(BUILDS_PATH, exist_ok=True)
            requirements_path = os.path.join(BUILDS_PATH, "requirements.txt")

            if state.get("requirements_approved"):
                # __ Phase 2: approved, pip install into the venv __
                with open(requirements_path) as f:
                    packages = [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]

                if not packages:
                    console.print("[dim cyan][installer] no packages to install[/dim cyan]")
                    tracer.log_agent_output("installer", {"status": "no packages"})
                    tracer.log_agent_end("installer")
                    return {"current_step": AgentSteps.INSTALLER_COMPLETE}

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
                return {"current_step": AgentSteps.INSTALLER_COMPLETE}

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
                    "current_step":         AgentSteps.INSTALLER_REQUIREMENTS_PENDING_APPROVAL,
                }

        except Exception as e:
            console.print(f"[red][installer] ERROR: {e}[/red]")
            raise


class Explorer(RemoteLoggingAgent):
    def __init__(self, model=None, log_path: str = "", trace_path: str = ""):
        """
        log_path / trace_path are set only under --env multi-node, where this
        agent runs in another process on another node. Manager.launch passes
        them straight to this constructor, so they have to be accepted here or
        the launch fails with a TypeError before the agent ever starts.

        Under local and hpc both stay "": the Explorer is in the launching
        process, printing to its terminal and sharing its tracer, so it needs
        neither file.
        """
        self.model = model
        self.log_path = log_path
        self.trace_path = trace_path

    @action
    async def set_model_config(self, model: str, base_url: str, api_key: str) -> None:
        self.model = ChatOpenAI(
            model=model, api_key=api_key, base_url=base_url,
            streaming=True, stream_usage=True,
        )

    @staticmethod
    def explorer_tool(handle: Handle[Agent]) -> Tool:
        """
        Wraps an academy handle for explorer in a langchain tool.

        All academy agents that use langchain must use this tool.
        """
        @tool
        async def explorer(state: AgentState):
            return await handle.explorer(state)

        return explorer

    @action
    async def explorer(self, state: AgentState) -> dict:
        """
        Explorer node -- connects to MCP server and runs a ReAct tool-calling loop
        to execute workflow tasks step by step.
        """
        # `global _current_condition` here would bind a name in THIS module,
        # leaving mcp_explorer's own flag at its default and silently
        # disabling condition A's skill suppression for the explorer.
        mcp_explorer.set_condition(state.get("condition", "B"))

        # stdout was already pointed at log_path by agent_on_startup, once for
        # this process -- see RemoteLoggingAgent. Redirecting again per action
        # would open the same file a second time and interleave with the first.
        # The trace is flushed on the way out, including on failure, so a pass
        # that raises still leaves its events on the shared filesystem.
        try:
            return await self._explore(state)
        finally:
            self._flush_trace()

    async def _explore(self, state: AgentState) -> dict:
        """The explorer run itself, with stdout already pointed wherever it goes."""
        console.print("\n[dim cyan][explorer] starting interactive workflow execution...[/dim cyan]")
        tracer.log_agent_start("explorer", {"engine": state.get("engine", "parsl")})
        tracer.log_agent_input("explorer", {
            "tasks_count": len(state.get("tasks", [])),
            "findings_count": len(state.get("literature_findings", [])),
            "engine": state.get("engine", "parsl"),
        })

        engine = state.get("engine", "parsl")
        console.print(f"[dim cyan][explorer] connecting to {engine} MCP server...[/dim cyan]")

        # Already running inside an event loop (this is an async action) -- just await
        # the coroutine directly instead of spinning up a nested loop.
        return await _explorer_async(state, engine)



class Orchestrator(Agent):
    def __init__(
        self,
        model: str,
        base_url: str,
        api_key: str,
        agents: dict[str, Handle[Agent]],
        run_log: str,
        agent_state: AgentState
    ):
        self.model = model
        self.checking_model = ""
        self.base_url = base_url
        self.api_key = api_key
        self.agents = agents
        self.run_log = run_log
        self.agent_state = agent_state

    async def agent_on_startup(self) -> None:
        # `self.agents` holds Handles, not the actual remote instances -- setting an
        # attribute on a handle does NOT set it on the remote agent. Push the model
        # through an explicit action instead.
        #
        # Send the three strings rather than a built ChatOpenAI: the agents are on
        # other nodes, so the argument has to serialize across the exchange, and the
        # httpx client inside a ChatOpenAI will not pickle. Each agent builds its own.
        #
        # Only the Planner. It is the one agent that reads self.model; the
        # Installer never makes an LLM call, and the Explorer builds its own client
        # from CODER_MODEL_NAME inside _explorer_async. Pushing to the Explorer was
        # not just wasted -- it was on the critical path: the Explorer lives in its
        # own PBS allocation, so awaiting its reply here blocked this startup hook,
        # and therefore the whole graph, until that job was scheduled and its node
        # had imported everything from GPFS. Without the call, the Planner and
        # Installer run while the Explorer's job queues, and the queue wait is
        # hidden behind work the run had to do anyway.
        await self.agents["planner"].set_model_config(
            model=self.model, base_url=self.base_url, api_key=self.api_key,
        )
        # The orchestrator runs its own `orchestrator` action locally, so it needs a
        # built LLM instance on itself too -- __init__ only stored the model name.
        self.model = ChatOpenAI(
            model=self.model,
            api_key=self.api_key,
            base_url=self.base_url,
            streaming=True,
            stream_usage=True,
        )
        

    @action
    async def orchestrator(self, state: AgentState) -> dict:
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
            # a write_workflow/run_workflow call did not actually hit the real engine
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
        _base = await _read_skill("agents/orchestrator", "orchestrator", enabled=_enabled)
        _env_kn = await _env_knowledge(state.get("env", "local"), "orchestrator", enabled=_enabled)
        _env_section = f"\n\n=== Environment Knowledge ===\n{_env_kn}" if _env_kn else ""

        if _base:
            _uc = await _list_skills("use_cases")
            _sys = await _list_skills("systems")
            _index = (f"\n\nAvailable skill contexts (set in skill_requests to load):"
                      f"\n  use_cases: {_uc}  -- request as \"use_cases/<name>/orchestrator\""
                      f"\n  systems:   {_sys}  -- request as \"systems/<name>\"") if (_uc or _sys) else ""
            _sys_prompt = _base + _env_section + _index + "\n\n---\n\n" + ORCHESTRATOR_SYSTEM_PROMPT + "\n\n" + PROJECT_LAYOUT
        else:
            _sys_prompt = ORCHESTRATOR_SYSTEM_PROMPT_NO_SKILLS + _env_section + "\n\n" + PROJECT_LAYOUT
        _human = "\n\n".join(parts)

        result: OrchestratorOutput = await _invoke_structured(self.model, OrchestratorOutput, [
            SystemMessage(content=_sys_prompt),
            HumanMessage(content=_human),
        ], "orchestrator")

        # Two-pass: if sub-skills requested, load them and re-invoke once
        if result.skill_requests:
            _sub = "\n\n".join(filter(None, [await _read_skill(r, "orchestrator", enabled=_enabled) for r in result.skill_requests]))
            if _sub:
                _enriched = _sys_prompt + f"\n\n=== Loaded Skills ===\n{_sub}\n\n(Final pass -- do not set skill_requests.)"
                result = await _invoke_structured(self.model, OrchestratorOutput, [
                    SystemMessage(content=_enriched),
                    HumanMessage(content=_human),
                ], "orchestrator")

        # Hard overrides: lock routing at deterministic transition points
        if state.get("current_step") == AgentSteps.INSTALLER_REQUIREMENTS_PENDING_APPROVAL:
            result.next = "installer"
        elif state.get("current_step") == AgentSteps.INSTALLER_COMPLETE:
            result.next = "explorer"
        elif state.get("current_step") == AgentSteps.EXPLORER_COMPLETE:
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
        if self.run_log:
            with open(self.run_log, "a") as _lf:
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

    @action
    async def agentic_workflow(self) -> dict:
        async def route_orchestrator(state: AgentState) -> str:
            return state["next"]

        # Adapter nodes: `self.agents[...]` holds academy Handles to remote-ish
        # Planner/Installer/Explorer agents. LangGraph nodes must be plain
        # `fn(state) -> dict` callables, so wrap each handle call rather than
        # passing the handle attribute (or an unbound class method) directly.
        async def planner_node(state: AgentState) -> dict:
            return await self.agents["planner"].planner(state)

        async def installer_node(state: AgentState) -> dict:
            return await self.agents["installer"].installer(state)

        async def explorer_node(state: AgentState) -> dict:
            return await self.agents["explorer"].explorer(state)

        graph = StateGraph(AgentState)

        graph.add_node("orchestrator", self.orchestrator)
        graph.add_node("planner",      planner_node)
        graph.add_node("installer",    installer_node)
        graph.add_node("explorer",     explorer_node)

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
        # Return the final state: the entrypoint runs the explorer's post-run
        # verification against it once the graph has finished.
        return await app.ainvoke(self.agent_state)