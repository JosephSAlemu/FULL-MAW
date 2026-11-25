import json
import shlex
import subprocess
import sys
from datetime import datetime

from academy.agent import Agent, action, loop
from academy.handle import Handle
from langchain_core.tools import Tool
from langchain_core.messages import HumanMessage, SystemMessage

from langchain.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.graph import StateGraph, END
from rich.console import Console
from rich.panel import Panel


import mcp_explorer
from mcp_explorer import _explorer_async
from tracing.trace_logger import tracer
import warnings

from pypdf import PdfReader

# pypdf is noisy about malformed cross-reference tables ("Ignoring wrong
# pointing object N") on papers it recovers from perfectly well. The filter
# used to live in utility.py, which uses no pypdf at all -- it worked only
# as an import side effect of this module importing that one. It belongs
# beside the PdfReader it is for.
warnings.filterwarnings("ignore", module="pypdf")
from states import PlannerOutput, OrchestratorOutput, AgentState, AgentSteps
from utility import (_list_skills, _env_knowledge, _invoke_structured,
                         _read_skill, _resolve_use_case_skill)
from common.consts import BUILDS_PATH
from systemprompts.orchestrator import ORCHESTRATOR_SYSTEM_PROMPT, ORCHESTRATOR_SYSTEM_PROMPT_NO_SKILLS
from systemprompts.planner import PLANNER_PROMPT, PLANNER_PROMPT_NO_SKILLS
from systemprompts.context import PROJECT_LAYOUT

import os
import base64
import mimetypes

console = Console()

_PDF_TEXT_CACHE: dict[tuple, str] = {}

PDF_MAX_CHARS = int(os.getenv("MAW_PDF_MAX_CHARS", "120000"))


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
    pages = [page.extract_text() for page in reader.pages]
    text = "\n".join(t for t in pages if t)
    console.print(f"[dim cyan][planner] loaded {len(reader.pages)} pages[/dim cyan]")
    if PDF_MAX_CHARS and len(text) > PDF_MAX_CHARS:
        console.print(
            f"[dim yellow][planner] truncating paper text "
            f"{len(text)} -> {PDF_MAX_CHARS} chars "
            f"(MAW_PDF_MAX_CHARS)[/dim yellow]"
        )
        text = text[:PDF_MAX_CHARS] + "\n\n[... paper truncated ...]"
    if key is not None:
        _PDF_TEXT_CACHE[key] = text
    return text


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
            max_retries=0,              # retries live in _invoke_structured
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

            # The use case comes from --domain, resolved and validated before the
            # run started -- it is NOT offered in the skill index for the model
            # to pick. Only system (engine) skills are requestable.
            _domain = state.get("domain", "")
            _domain_skill = await _resolve_use_case_skill(_domain, "planner", enabled=_enabled)
            _domain_section = (f"\n\n=== Domain Skill ({_domain}) ===\n{_domain_skill}"
                               if _domain_skill else "")

            # The engine is fixed by --engine, exactly like the domain. Offering
            # the whole systems/ list let the model pick one, and it could pick
            # a different one than the run is using -- a --engine parsl run was
            # reviewed against PyCOMPSs because the orchestrator chose
            # systems/pycompss off this index. Only the run's own engine is
            # offered now.
            _engine = state.get("engine", "parsl")
            if _base:
                _sys = await _list_skills("systems")
                _index = ((f"\n\nWorkflow engine for this run: {_engine} (fixed by --engine)."
                           f"\n  Request \"systems/{_engine}\" in skill_requests for its"
                           f" details. No other engine skill is available to you.")
                          if _engine in _sys else "")
                _sys_prompt = (_base + _env_section + _domain_section + _index
                               + "\n\n---\n\n" + PLANNER_PROMPT + "\n\n" + PROJECT_LAYOUT)
            else:
                _sys_prompt = (PLANNER_PROMPT_NO_SKILLS + _env_section + _domain_section
                               + "\n\n" + PROJECT_LAYOUT)
            _data_files = state.get("selected_data_files", [])
            _data_section = (f"\n\nAvailable input data files (in /app/inputs/data/):\n" +
                             "\n".join(f"  - {f}" for f in _data_files)) if _data_files else ""
            # State the engine in the message too, not just the skill index: the
            # tasks this returns have to be expressible in it, and the
            # orchestrator reviews them against the same engine.
            _engine_line = f"\n\nWorkflow engine: {_engine} (fixed for this run)"
            _human = (f"Goal: {state['goal']}{_engine_line}{_data_section}"
                      f"\n\nPaper:\n{pdf_text}{feedback_section}")

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

            # Two-pass: if sub-skills requested, load them and re-invoke once.
            #
            # The second pass does NOT resend the paper. It used to reuse
            # _human_msg, which carries the whole PDF (and the image, if there
            # is one), so the longest call in the run happened twice -- the
            # second time with the skill text on top of it. The paper is
            # already distilled into the first pass's findings, so the revision
            # only needs those, the goal, and the skills now in the system
            # prompt. Halves the planner's wall clock and removes one of the
            # two chances to lose the response.
            # Drop any use_cases/* request: the domain skill is already in the
            # prompt and is not the model's to choose. Without this filter a
            # model that ignores the instruction can still pull a second,
            # different domain in on the revision pass.
            _reqs = [r for r in result.skill_requests if not r.startswith("use_cases/")]
            if len(_reqs) != len(result.skill_requests):
                console.print("[yellow][planner] ignoring use_cases/* skill request; "
                              f"domain is fixed to {_domain or '<none>'}[/yellow]")
            # Same guard for engines. A systems/ request for anything other than
            # this run's engine would load a second, contradictory engine skill.
            _wrong = [r for r in _reqs
                      if r.startswith("systems/") and r != f"systems/{_engine}"]
            if _wrong:
                console.print(f"[yellow][planner] ignoring {_wrong}; engine is fixed "
                              f"to {_engine}[/yellow]")
                _reqs = [r for r in _reqs if r not in _wrong]
            if _reqs:
                _sub = "\n\n".join(filter(None, [await _read_skill(r, "planner", enabled=_enabled) for r in _reqs]))
                if _sub:
                    _enriched = _sys_prompt + f"\n\n=== Loaded Skills ===\n{_sub}\n\n(Final pass -- do not set skill_requests.)"
                    _revise_msg = HumanMessage(content=(
                        f"Goal: {state['goal']}{_data_section}\n\n"
                        f"Your first-pass output:\n{result.model_dump_json(indent=2)}\n\n"
                        f"Revise it against the skills now loaded in your system prompt. "
                        f"Keep whatever is already correct and change only what those "
                        f"skills require. Return the same schema."
                        f"{feedback_section}"
                    ))
                    result = await _invoke_structured(self.model, PlannerOutput, [
                        SystemMessage(content=_enriched),
                        _revise_msg,
                    ], "planner")

            # force adios2 into the stack for this engine, don't leave it up to the model.
            # without it every task just falls back to numpy I/O and adios2 never actually runs
            if state.get("engine") == "adios" and "adios2" not in result.stack_decision:
                result.stack_decision.append("adios2")

            console.print(f"[dim cyan][planner] produced {len(result.tasks)} tasks[/dim cyan]")

            findings = "\n".join(f"  [cyan]*[/cyan] {f}" for f in result.literature_findings)
            stack    = "\n".join(f"  [cyan]*[/cyan] {s}" for s in result.stack_decision)
            mods     = ("\n".join(f"  [cyan]*[/cyan] {m}" for m in result.modules)
                        or "  [dim](none)[/dim]")
            tasks    = "\n".join(f"  [bold]{i+1}.[/bold] {t}" for i, t in enumerate(result.tasks))
            console.print(Panel(
                f"[bold]Literature Findings[/bold]\n{findings}\n\n"
                f"[bold]Stack[/bold]\n{stack}\n\n"
                f"[bold]Modules[/bold]\n{mods}\n\n"
                f"[bold]Tasks[/bold]\n{tasks}",
                title="[bold green]Planner Output[/bold green]",
                border_style="green",
            ))

            tracer.log_agent_output("planner", {
                "findings_count": len(result.literature_findings),
                "stack": result.stack_decision,
                "modules": result.modules,
                "tasks_count": len(result.tasks),
                "tasks_preview": result.tasks[:3],
            })
            tracer.log_agent_end("planner")

            return {
                "literature_findings": result.literature_findings,
                "stack_decision":      result.stack_decision,
                # Names only, de-duplicated, `module load` prefix stripped in
                # case the model echoed the domain skill's phrasing verbatim.
                "modules":             [m.strip().removeprefix("module load").strip()
                                        for m in dict.fromkeys(result.modules) if m.strip()],
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
            max_retries=0,
        )

    def _report_mpi_bindings(self, packages: list) -> str:
        """Say which MPI an installed binding linked, or "" if none was installed.

        Probed in a child process: a binding whose libmpi cannot be resolved
        raises on import, and doing that here would take the agent down with it.
        """
        names = {p.split("==")[0].split(">")[0].split("<")[0].split("[")[0]
                 .strip().lower() for p in packages}
        if "mpi4py" not in names:
            return ""

        probe = (
            "import mpi4py, mpi4py.MPI as M; "
            "print(mpi4py.get_config().get('mpi_dir','?'), M.Get_library_version()"
            ".splitlines()[0] if M.Get_library_version() else '')"
        )
        proc = subprocess.run([sys.executable, "-c", probe],
                              capture_output=True, text=True, timeout=120)
        if proc.returncode != 0:
            msg = f"mpi4py installed but will not import: {proc.stderr.strip()[-300:]}"
            console.print(f"[yellow][installer] {msg}[/yellow]")
            return msg
        msg = proc.stdout.strip()
        console.print(f"[dim cyan][installer] mpi4py links: {msg}[/dim cyan]")
        return msg

    @action
    async def installer(self, state: AgentState) -> dict:
        """Two-phase: write requirements.txt, wait for approval, then install.

            pass 1  write the file, return INSTALLER_REQUIREMENTS_PENDING_APPROVAL
            pass 2  (requirements_approved) pip install it

        so the routing path is

            planner -> installer -> orchestrator -> installer -> orchestrator -> explorer

        WHAT THE GATE CAN AND CANNOT DO. This agent has no language model: it
        writes `stack_decision` to disk and nothing else. So orchestrator
        feedback CANNOT change what a second pass produces -- rejecting and
        re-routing here regenerates a byte-identical file.

        What the gate does buy, and the reason it is worth a round trip: the
        orchestrator gets to see the package list BEFORE anything is installed,
        and the fix for a list it dislikes is to route to `planner`, who owns
        stack_decision. Rejecting into the installer is the dead end; rejecting
        into the planner is the point.

        Pass 1 therefore says so explicitly in its output, so an orchestrator
        that rejects knows where to send the work. `installer_revisions` still
        counts re-entries, and the orchestrator's revision cap stops a loop if
        it ignores that.
        """
        try:
            tracer.log_agent_start("installer")
            # BUILDS_PATH, not __file__-relative: this module lives in src/, so
            # "the directory holding this file" would put requirements.txt in
            # src/builds/. Everything else -- the installer skills,
            # PROJECT_LAYOUT, the README, the explorer resolving /app/work/builds/ --
            # means work/builds/.
            os.makedirs(BUILDS_PATH, exist_ok=True)
            requirements_path = os.path.join(BUILDS_PATH, "requirements.txt")

            # requirements_override wins over stack_decision. It is the
            # orchestrator's corrected list, and the orchestrator is the only
            # agent that reviews the file -- see the gate in Orchestrator.
            override = [pkg for pkg in state.get("requirements_override", []) if pkg]
            stack = override or [pkg for pkg in state.get("stack_decision", []) if pkg]

            # ---- PASS 1: write, then stop for approval --------------------
            if not state.get("requirements_approved"):
                # Always regenerate. A requirements.txt on disk is from a
                # previous run -- possibly a different engine or domain -- and
                # reusing it is how a stale package reaches the venv. This also
                # means a re-entry produces the same bytes; see the docstring.
                content = "\n".join(stack) + ("\n" if stack else "")
                with open(requirements_path, "w") as f:
                    f.write(content)

                console.print(Panel(
                    content or "(empty)",
                    title="[bold yellow]requirements.txt -- Pending Orchestrator "
                          "Approval[/bold yellow]",
                    border_style="yellow",
                ))

                _feedback = state.get("orchestrator_feedback", "")

                console.print("[dim yellow][installer] waiting for orchestrator "
                              "approval...[/dim yellow]")
                tracer.log_agent_output("installer", {
                    "status": "pending approval",
                    "packages": content.strip(),
                    "count": len(stack),
                    "regenerated_identical": bool(_feedback),
                })
                tracer.log_agent_end("installer")
                return {
                    "requirements_content": content,
                    "current_step": AgentSteps.INSTALLER_REQUIREMENTS_PENDING_APPROVAL,
                }

            # ---- PASS 2: approved, install --------------------------------
            # An override means the orchestrator approved a DIFFERENT list from
            # the one pass 1 wrote, so the file on disk is stale: rewrite it
            # before reading it back, or pip installs the rejected set.
            if override:
                content = "\n".join(override) + "\n"
                with open(requirements_path, "w") as f:
                    f.write(content)
                console.print(Panel(
                    content,
                    title="[bold cyan]requirements.txt -- corrected by the "
                          "orchestrator[/bold cyan]",
                    border_style="cyan",
                ))

            # Read back rather than rebuilding from stack_decision: the
            # orchestrator approved the FILE, and rebuilding here would install
            # something it never saw if stack_decision changed in between.
            if os.path.isfile(requirements_path):
                with open(requirements_path) as f:
                    content = f.read()
                stack = [ln.strip() for ln in content.splitlines()
                         if ln.strip() and not ln.startswith("#")]
            else:
                content = "\n".join(stack) + ("\n" if stack else "")

            if not stack:
                console.print("[dim cyan][installer] no packages to install[/dim cyan]")
                tracer.log_agent_output("installer", {"status": "no packages"})
                tracer.log_agent_end("installer")
                return {
                    "requirements_content": content,
                    "current_step": AgentSteps.INSTALLER_COMPLETE,
                }

            # WHERE THIS RUNS, AND WHY IT MATTERS.
            #
            # This process is the launching one -- the Installer runs in the
            # event loop, not on a worker -- so pip installs into the venv using
            # whatever environment the agent was started in. For a pure-Python
            # package that is fine. For one that COMPILES against MPI (mpi4py,
            # or any package with MPI-linked extensions) it is a trap: the
            # compiler pip finds is whatever `mpicc` resolves to here, while the
            # MPI the tasks actually use is loaded inside each task's own
            # command string, on a node this process never touched. Those two
            # are not the same environment, and the mismatch is silent -- pip
            # succeeds, and the import fails much later with a missing libmpi or
            # a dead collective.
            #
            # state["modules"] closes the gap. The planner reads the domain
            # skill, which names the module in prose, and returns it as a field;
            # pip then runs under it in a login shell (which is also what
            # defines `module` in the first place). Empty when the domain names
            # no module, in which case pip inherits this process's environment.
            modules = [m.strip() for m in state.get("modules", []) if m.strip()]
            pip_argv = [sys.executable, "-m", "pip", "install"] + stack

            if modules:
                loads = " ".join(f"module load {shlex.quote(m)} &&" for m in modules)
                console.print(f"[dim cyan][installer] building under modules: "
                              f"{', '.join(modules)}[/dim cyan]")
                command = [
                    "bash", "-lc",
                    # purge first: whatever the launching shell happens to have
                    # loaded is not what the domain asked for, and a stale MPI
                    # here is compiled straight into the bindings.
                    "module purge 2>/dev/null || true && "
                    + loads + " "
                    + " ".join(shlex.quote(a) for a in pip_argv),
                ]
            else:
                command = pip_argv

            console.print(f"[dim cyan][installer] pip installing {len(stack)} packages...[/dim cyan]")
            proc = subprocess.run(command, capture_output=True, text=True, timeout=600)
            ok = proc.returncode == 0
            if ok:
                console.print("[dim cyan][installer] packages ready[/dim cyan]")
            else:
                console.print(f"[yellow][installer] pip stderr:\n{proc.stderr[-2000:]}[/yellow]")

            # Report what any MPI bindings actually linked, rather than leaving
            # it to be discovered at the first collective. A report, not a gate.
            mpi_report = self._report_mpi_bindings(stack)

            tracer.log_agent_output("installer", {
                "status": "packages installed" if ok else "pip failed",
                "count": len(stack),
                "exit_code": proc.returncode,
                "modules": modules or None,
                **({"mpi_bindings": mpi_report} if mpi_report else {}),
                **({"pip_error": proc.stderr[-2000:]} if not ok else {}),
            })
            tracer.log_agent_end("installer")

            # A pip failure is reported, not raised. The orchestrator decides
            # whether a missing package is fatal for this run -- the explorer
            # can often proceed, and check_package tells it what is there.
            return {
                "requirements_content": content,
                "current_step": AgentSteps.INSTALLER_COMPLETE,
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
            max_retries=0,
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
            max_retries=0,
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

        # The engine was reaching this prompt in exactly one place -- the
        # ENGINE USAGE WARNING below, which only fires once an exploration_log
        # exists. On the call right after the planner there was none, so the
        # orchestrator reviewed the plan with no idea which engine it was for
        # and took its cue from the systems/ skill index instead.
        _engine = state.get("engine", "parsl")
        parts = [
            f"Goal: {state['goal']}",
            f"Current step: {state['current_step']}",
            f"Workflow engine for this run: {_engine} (fixed by --engine -- review "
            f"every task against THIS engine and no other)",
        ]
        if any(revisions.values()):
            parts.append("Revision counts so far: " +
                         ", ".join(f"{k}={v}" for k, v in revisions.items()))
        if state.get("literature_findings"):
            parts.append(f"Literature findings ({len(state['literature_findings'])} items):\n" +
                         "\n".join(f"  - {f}" for f in state["literature_findings"]))
        if state.get("stack_decision"):
            parts.append(f"Stack: {state['stack_decision']}")
        if state.get("modules"):
            parts.append(
                f"Environment modules: {state['modules']} -- the installer builds "
                f"compiled packages under these, and each task loads them in its "
                f"own command string"
            )
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

        # Same as the planner: the use case is fixed by --domain, not chosen by
        # the model. This also removes the second orchestrator call that used to
        # happen at START purely to load a use-case skill it had just asked for.
        _domain = state.get("domain", "")
        _domain_skill = await _resolve_use_case_skill(_domain, "orchestrator", enabled=_enabled)
        _domain_section = (f"\n\n=== Domain Skill ({_domain}) ===\n{_domain_skill}"
                           if _domain_skill else "")

        if _base:
            _sys = await _list_skills("systems")
            _index = ((f"\n\nWorkflow engine for this run: {_engine} (fixed by --engine)."
                       f"\n  Request \"systems/{_engine}\" in skill_requests for its"
                       f" details. No other engine skill is available to you.")
                      if _engine in _sys else "")
            _sys_prompt = (_base + _env_section + _domain_section + _index
                           + "\n\n---\n\n" + ORCHESTRATOR_SYSTEM_PROMPT + "\n\n" + PROJECT_LAYOUT)
        else:
            _sys_prompt = (ORCHESTRATOR_SYSTEM_PROMPT_NO_SKILLS + _env_section
                           + _domain_section + "\n\n" + PROJECT_LAYOUT)
        _human = "\n\n".join(parts)

        result: OrchestratorOutput = await _invoke_structured(self.model, OrchestratorOutput, [
            SystemMessage(content=_sys_prompt),
            HumanMessage(content=_human),
        ], "orchestrator")

        # Two-pass: if sub-skills requested, load them and re-invoke once
        _reqs = [r for r in result.skill_requests if not r.startswith("use_cases/")]
        if len(_reqs) != len(result.skill_requests):
            console.print("[yellow][orchestrator] ignoring use_cases/* skill request; "
                          f"domain is fixed to {_domain or '<none>'}[/yellow]")
        # Same guard for engines -- see the note at `parts` above.
        _wrong = [r for r in _reqs
                  if r.startswith("systems/") and r != f"systems/{_engine}"]
        if _wrong:
            console.print(f"[yellow][orchestrator] ignoring {_wrong}; engine is fixed "
                          f"to {_engine}[/yellow]")
            _reqs = [r for r in _reqs if r not in _wrong]
        if _reqs:
            _sub = "\n\n".join(filter(None, [await _read_skill(r, "orchestrator", enabled=_enabled) for r in _reqs]))
            if _sub:
                _enriched = _sys_prompt + f"\n\n=== Loaded Skills ===\n{_sub}\n\n(Final pass -- do not set skill_requests.)"
                result = await _invoke_structured(self.model, OrchestratorOutput, [
                    SystemMessage(content=_enriched),
                    HumanMessage(content=_human),
                ], "orchestrator")

        # ---- ROUTING IS DETERMINISTIC. THE MODEL DOES NOT CHOOSE. -----
        #
        # The pipeline is fixed:
        #
        #   orchestrator -> planner -> orchestrator -> installer
        #     -> orchestrator -> installer (pass 2) -> explorer
        #     -> orchestrator -> end
        #
        # The orchestrator is a MESSENGER, with exactly one exception: the
        # installer's package list, which it corrects ITSELF via
        # requirements_override. It does not direct the planner, it does not
        # direct the explorer, and it never sends an agent back.
        #
        # WHATEVER THE PLANNER PRODUCES STANDS, EVEN IF IT IS WRONG. The
        # planner owns the plan; the explorer reports what happened. Routing
        # back to either costs a full LLM session (plus a PDF re-read for the
        # planner) and, observed repeatedly, returns the same output.
        #
        # `result.next` is therefore IGNORED. Earlier versions honoured it
        # and policed it afterwards -- first with a revision cap, then with a
        # vacuous-reason guard. Both were treating the symptom: if the route
        # is a property of the step, the model should not be asked for it.
        _ROUTE = {
            AgentSteps.START: "planner",
            AgentSteps.PLANNER_COMPLETE: "installer",
            AgentSteps.INSTALLER_REQUIREMENTS_PENDING_APPROVAL: "installer",
            AgentSteps.INSTALLER_COMPLETE: "explorer",
            AgentSteps.EXPLORER_COMPLETE: "end",
        }
        _step = state.get("current_step")
        _asked = result.next
        result.next = _ROUTE.get(_step, "end")
        if _asked != result.next:
            console.print(
                f"[dim yellow][orchestrator] asked for {_asked}; the route from "
                f"{_step} is {result.next}[/dim yellow]"
            )

        # THE TWO EXCEPTIONS. The orchestrator never sends an agent back, so the
        # only way it can act on an objection is to fix the thing itself and let
        # the fixed version travel forward on the normal route.
        if _step == AgentSteps.INSTALLER_REQUIREMENTS_PENDING_APPROVAL:
            # The installer has no language model, so a different list is the
            # only thing that changes the outcome.
            if result.requirements_override:
                console.print(
                    "[yellow][orchestrator] correcting requirements.txt to: "
                    f"{', '.join(result.requirements_override)}[/yellow]"
                )
            result.requirements_approved = True

        tasks_update: dict = {}
        if _step == AgentSteps.PLANNER_COMPLETE and result.tasks_override:
            # The corrected plan goes forward to the installer on the fixed
            # route; the planner is not re-entered.
            tasks_update["tasks"] = result.tasks_override
            console.print(
                f"[yellow][orchestrator] replacing the planner's "
                f"{len(state.get('tasks', []))} task(s) with "
                f"{len(result.tasks_override)} corrected task(s)[/yellow]"
            )

        # FEEDBACK GOES NOWHERE. The installer is corrected with a list, not
        # with prose, and no other agent is ever revised -- so there is no
        # recipient for a feedback string. Blanked rather than merely ignored,
        # so it cannot reach a prompt by some later edit. `reasoning` is kept:
        # it goes to the run log and the console, for the operator.
        result.feedback = ""

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

        # NO REVISION COUNTERS. No agent is re-entered, so nothing can be a
        # revision. The counters stay in AgentState for trace compatibility
        # and are never incremented.
        revision_update = {}

        state_update = {
            "next":                  result.next,
            "orchestrator_feedback": result.feedback,
            # Live again: the installer's pass 2 is gated on this.
            "requirements_approved": result.requirements_approved,
            "requirements_override": result.requirements_override,
            "current_step":          f"orchestrator_routed_to_{result.next}",
            **tasks_update,
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