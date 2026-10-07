"""
MCP Explorer Agent -- ReAct loop that calls workflow engine tools via MCP protocol.

The explorer receives tasks from the planner once the installer has finished
preparing the local venv, then iteratively calls tools (exposed by the MCP server) to complete each task:
submitting Python tasks, running shell commands, checking outputs, installing
missing packages, and recovering from errors.

The explorer doesn't know which workflow engine is behind the MCP server.
It calls the same tools regardless of backend (Parsl, PyCOMPSs, ADIOS).
"""

import os
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


# __ Skill file helpers ________________________________________________________

_SKILLS_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "skills")

# common.consts is dependency-free literals, so importing it here can't create the
# circular import that forced ENV_NOTES to be duplicated below.
from common.consts import USE_CASE_SKILL_REDIRECTS


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

# Skill paths actually read during this run, in first-load order. The post-run
# review needs to know which domain file was in play so it can propose (and
# optionally append) edits to the right file rather than guessing.
_loaded_skills: list[str] = []

# Mirrors agent_mcp.ENV_NOTES. Duplicated rather than imported to avoid a circular
# import (agent_mcp imports `explorer` from this module).
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


@tool
def write_workflow(python_code: str, filename: str = "workflow.py") -> str:
    """Write the single standalone Parsl workflow file that performs ALL the work.

    Parsl runs only what this file expresses. Write ONE file in which every unit
    of work is a decorated function:
      - `@bash_app`   -- ANY command-line/MPI invocation. The function body returns
                         the command string; Parsl executes it. You never run it.
      - `@python_app` -- pure Python work (analysis, plotting, writing files).

    The file must build its own Parsl Config, call `parsl.load(config)`, invoke the
    apps, resolve futures with `.result()`, and `parsl.clear()` at the end.

    The server rejects the file if it has no apps, never calls parsl.load, imports
    or calls subprocess/os.system/os.popen, or puts mpirun/srun outside a @bash_app.

    Args:
        python_code: Full contents of the Parsl driver file
        filename: Bare .py filename written under /app/work/run0 (default: workflow.py)
    """
    return _call_mcp_tool("write_workflow", {
        "python_code": python_code, "filename": filename,
    })


@tool
def run_workflow(filename: str = "workflow.py", timeout: int = 7200) -> str:
    """Execute the Parsl workflow file previously created with write_workflow.

    The server launches the file; the Parsl runtime inside it schedules every
    @bash_app and @python_app step. Call this after write_workflow succeeds.

    Args:
        filename: Workflow filename under /app/work/run0 (default: workflow.py)
        timeout: Max seconds to wait (default: 7200)
    """
    return _call_mcp_tool("run_workflow", {"filename": filename, "timeout": timeout})


@tool
def get_task_status(task_id: str) -> str:
    """Get the current status of a submitted task.

    Args:
        task_id: The task ID returned by run_workflow
    """
    return _call_mcp_tool("get_task_status", {"task_id": task_id})


@tool
def get_task_result(task_id: str) -> str:
    """Get the full output (stdout/stderr) of a completed task.

    Args:
        task_id: The task ID returned by run_workflow
    """
    return _call_mcp_tool("get_task_result", {"task_id": task_id})


@tool
def list_tasks() -> str:
    """List all submitted tasks and their current status."""
    return _call_mcp_tool("list_tasks", {})


@tool
def install_package(package: str) -> str:
    """Install a pip package into the local venv.

    Use when a required package is missing (ModuleNotFoundError).

    Args:
        package: Package name to install (e.g. "numpy", "ovito==3.10.0")
    """
    return _call_mcp_tool("install_package", {"package": package})


@tool
def check_package(package: str) -> str:
    """Check if a Python package is installed in the local venv.

    Args:
        package: Package name to check (e.g. "numpy", "lammps", "ovito")
    """
    return _call_mcp_tool("check_package", {"package": package})


@tool
def list_files(directory: str = "/app/work/run0") -> str:
    """List all files in a directory in the local environment.

    Use this to verify that expected output files were created after a task.

    Args:
        directory: Path to list (default: /app/work/run0)
    """
    return _call_mcp_tool("list_files", {"directory": directory})


@tool
def read_file(path: str, max_lines: int = 100) -> str:
    """Read the contents of a file in the local environment.

    Use this to inspect output files, check results, or debug errors.

    Args:
        path: Absolute path of the file
        max_lines: Maximum number of lines to return (default: 100)
    """
    return _call_mcp_tool("read_file", {"path": path, "max_lines": max_lines})


@tool
def get_resources() -> str:
    """Detect available compute resources (nodes, ranks, launcher) for this run.

    Always call this FIRST, before writing any MPI command and before deciding
    whether you're on a single local machine or inside a multi-node HPC allocation.

    Returns JSON with in_pbs, nnodes, ntasks, cpus_per_task, nodelist, launcher.
    If in_pbs is false, call load_skill("local"). If in_pbs is true, call
    load_skill("hpc") to learn the launcher conventions and storage paths.
    """
    return _call_mcp_tool("get_resources", {})



_KNOWLEDGE_SKILLS = {
    "local": "knowledge/local",
    "hpc":   "knowledge/lcrc",
}

# Per-engine execution contract, appended to the system prompt. Every engine is
# generated-file only and binds the same two execution tools; what differs is the
# constructs the generated file must use, which is what this text supplies.
_EXEC_PROMPT_HEAD = """\
## How You Execute Work (generated file only)

You have exactly two execution tools: `write_workflow` and `run_workflow`.
These are the only execution tools. No tool takes a code string or a command
string and runs it for you.

1. `write_workflow(python_code=..., filename="workflow.py")` -- write ONE
   standalone file containing EVERY step of the workflow.
2. `run_workflow(filename="workflow.py")` -- the server executes that file.

A simulation plus its visualization is ONE file with multiple steps in it, not
two files and not two tool calls.

If `write_workflow` rejects your file, read the `errors` list, fix the file, and
call `write_workflow` again. Do not try to route around it with another tool.
"""

_ENGINE_EXEC_PROMPTS = {
    "parsl": _EXEC_PROMPT_HEAD + """
### What the file must contain (Parsl)

Every unit of work is a decorated function:

- `@bash_app` for ANY command-line work (simulation binaries, MPI runs, file
  conversion). The function body returns the command **string**; Parsl runs it.
  An 8-rank MPI run is a @bash_app returning `f"mpirun -n 8 {exe} {params}"`.
- `@python_app` for pure Python work (analysis, plotting, writing summaries).

The file must build its own Parsl `Config`, call `parsl.load(config)`, invoke the
apps, wire them together with futures and `File` objects, and call
`parsl.clear()` at the end.

Rejected: `import subprocess`, `subprocess.run/Popen`, `os.system`, `os.popen`,
and any `mpirun`/`srun`/`mpiexec` string outside a `@bash_app` body.
""",

    "pycompss": _EXEC_PROMPT_HEAD + """
### What the file must contain (PyCOMPSs)

Every unit of work is a decorated function:

- `@task` for pure Python work.
- `@binary(binary="...")` stacked ABOVE `@task` for a command-line program.
  The body is `pass`. Arguments come from the function signature plus `@task`
  parameter types (`FILE_IN`, `FILE_OUT`, `FILE_IN_STDIN`, `FILE_OUT_STDOUT`,
  `Prefix`), or from an `args` string with `{{name}}` placeholders.
- `@mpi(binary="...", runner="mpirun", processes=N)` stacked above `@task` for
  an MPI program on N ranks. Body is `pass`. This is how rank count is
  expressed -- you never type `mpirun` yourself.

Dependencies come from passing values between tasks and from FILE_IN/FILE_OUT
parameters; call `compss_wait_on()` only where the driver needs a real value.

The runtime lifecycle depends on how this server launches the file, which
`write_workflow` reports as `launch_mode`:
- `runcompss`: the launcher owns the runtime -- do NOT call compss_start/stop.
- `direct`: the file MUST call `compss_start()` first and `compss_stop()` last.

Rejected: `subprocess` anywhere, `os.system`/`os.popen`, `@binary`/`@mpi`
without `@task` beneath it, and `mpirun`/`srun` named outside an `@mpi`
decorator.
""",

    "adios": _EXEC_PROMPT_HEAD + """
### What the file must contain (ADIOS2)

ADIOS2 is an I/O library, not a scheduler -- it has no task decorator. The file
is a **staged pipeline**:

- Each stage is a top-level function; a `main()` calls them in order.
- Inter-stage numerical data MUST move through ADIOS2: the producing stage
  writes with `stream.write(...)` on an `adios2.Stream(path, "w")`, and the
  consuming stage reads it back with `stream.read(...)` from an
  `adios2.Stream(path, "r")`. Write it and actually read it back -- do not keep
  using the in-memory copy.
- Human-facing outputs (PNG, summary text) stay plain files; BP is for the
  numerical arrays flowing between stages.

Because ADIOS2 cannot launch programs, a CLI step for an external compiled
binary is a stage function that uses `subprocess` internally. That is allowed
ONLY inside a stage body. Never subprocess another Python stage -- Python
stages live in this same file and talk to each other through ADIOS2.

For true in-situ streaming (producer and consumer running CONCURRENTLY rather
than one after the other), declare a top-level `MPI_RANKS = <int>`. The server
then launches the whole file under one `mpirun -n <MPI_RANKS>`, and the stages
dispatch on MPI rank: split `MPI.COMM_WORLD` with `world.Split(color=role,
key=rank)`, pass the split `comm` to `adios2.Adios(comm)` and
`io.open(name, mode, comm)`, and use the SST engine. See the engine skill for
a full example. Without `MPI_RANKS` the pipeline runs sequentially, which is
fine when stages don't need to overlap.

Rejected: `subprocess`/`os.system`/`os.popen` at module top level, no stage
functions, no `main()` (serial mode), opening no ADIOS2 stream, never reading
back what was written, or declaring `MPI_RANKS` without splitting COMM_WORLD
and passing the split comm to ADIOS2.
""",
}


def _engine_exec_prompt(engine: str) -> str:
    """Execution-contract text for this engine's generated workflow file."""
    return _ENGINE_EXEC_PROMPTS.get(engine, _EXEC_PROMPT_HEAD)

# engine-specific skills, force-loaded into the system prompt in _explorer_async
# instead of leaving it to load_skill (systems/<engine>, knowledge/<ENGINE>)
_ENGINE_SKILLS = {
    "parsl":    ("systems/parsl",    "knowledge/PARSL"),
    "pycompss": ("systems/pycompss", "knowledge/PyCOMPSs"),
    "adios":    ("systems/adios",    "knowledge/ADIOS"),
}

_ENGINE_RELEVANT_TOOLS = (
    "write_workflow", "run_workflow",
)


def _classify_engine_usage(engine: str, tool_name: str, tool_args: dict,
                            tool_result_text: str) -> tuple[Optional[str], Optional[bool]]:
    """Derive (engine_backend, engine_verified) for a tool call, for tracing.

    engine_backend is the raw "engine" field self-reported by the MCP server (e.g.
    "parsl-fallback", "adios2-unused"), when the tool's JSON result has one.

    All three engines use the same shape: the server is authoritative on whether
    the engine was genuinely used, not just available. parsl/pycompss_server.py
    report whether their runtime actually dispatched the workflow (vs. fallback);
    adios_server.py's _adios_engine_state scans the generated file for a real
    adios2 API call and reports "adios2-unused" when the package was available
    but never actually called.

    "-fallback" maps to None for adios (package unavailable isn't the explorer's
    fault), but to False for parsl/pycompss, whose fallback always means a real
    runtime-dispatch failure worth flagging. "adios2-n/a" also maps to None.
    """
    if tool_name not in _ENGINE_RELEVANT_TOOLS:
        return None, None
    try:
        backend = json.loads(tool_result_text).get("engine")
    except (json.JSONDecodeError, AttributeError, TypeError):
        backend = None
    if backend is None:
        return None, None
    if engine in ("parsl", "pycompss"):
        return backend, not backend.endswith("-fallback")
    if engine == "adios":
        if backend in ("adios2-fallback", "adios2-n/a"):
            return backend, None
        return backend, backend != "adios2-unused"
    return backend, None


@tool
def load_skill(name: str) -> str:
    """Load runtime-environment knowledge into your context: "local" or "hpc".

    Call this only after get_resources tells you which environment you're actually
    in -- do not load both. "local" covers single-machine constraints (no MPI,
    no PBS). "hpc" covers PBS/mpirun conventions and LCRC storage paths.

    Args:
        name: "local" or "hpc"
    """
    if name not in _KNOWLEDGE_SKILLS:
        return f"Unknown skill '{name}'. Use 'local' or 'hpc'."
    # B/C read the real skill file. A never touches it, gets the same lean
    # ENV_NOTES substitute the orchestrator/planner use in that condition
    enabled = _current_condition != "A"
    content = _read_skill(_KNOWLEDGE_SKILLS[name], enabled=enabled)
    return content or ENV_NOTES.get(name, ENV_NOTES["local"])


# Inspection/environment tools. These never execute workflow work, they only
# observe it or prepare the venv.
_COMMON_TOOLS = [
    get_task_status, get_task_result, list_tasks,
    install_package, check_package,
    list_files, read_file,
    get_resources, load_skill,
]

# Execution tools. Every engine is generated-file only: the agent writes ONE
# workflow file expressed in that engine's own constructs, then runs it. No
# engine accepts a code or command string to execute, which is what used to let
# work bypass the engine entirely.
_EXEC_TOOLS = [write_workflow, run_workflow]

EXPLORER_TOOLS = _COMMON_TOOLS + _EXEC_TOOLS


def _tools_for_engine(engine: str) -> list:
    return EXPLORER_TOOLS


# __ Explorer System Prompt ____________________________________________________

EXPLORER_SYSTEM_PROMPT = """\
You are the Explorer agent in a scientific workflow reproduction system. Your job is to
execute a computational workflow step by step using tools provided by a workflow engine
MCP server.

You receive:
- A list of tasks from the planner (what needs to be done)
- Literature findings (scientific context from the paper)
- Information about what software is installed in the venv

Your goal: complete every task successfully by calling tools, observing results, and
adapting your approach when things fail.

## Available Tools

The exact execution tools you have depend on this run's engine -- use the ones
actually bound for you, listed in the engine reference section below. These are
always available:

| Tool | When to use |
|---|---|
| get_resources | **Call first.** Detects nodes/ranks/launcher (in_pbs true/false) |
| load_skill | Load "local" or "hpc" environment knowledge -- pick ONE based on get_resources |
| get_task_status | Check if a previously submitted task is done |
| get_task_result | Get full stdout/stderr from a completed task |
| list_tasks | See all tasks and their statuses |
| install_package | Install a missing pip package |
| check_package | Verify a package is installed |
| list_files | Check what files exist in a directory |
| read_file | Inspect file contents |

## Workflow

1. Call get_resources first. Then call load_skill("hpc") if in_pbs is true, or
   load_skill("local") if it is false -- load only the one that matches.
2. Review the tasks list and plan your execution order
3. Before running anything, check prerequisites (files exist, packages installed)
4. Express and run the work using this engine's execution tools (see below)
5. After execution, verify output (list_files, read_file)
6. If something fails, diagnose and fix (install package, change code, retry)

## Rules

- Always verify output after execution
- Max 3 retries per task before giving up -- then MOVE ON to the next task
- Do NOT spend more than 3 attempts debugging any single issue (e.g. Qt rendering, display errors)
- If a visualization/rendering task fails due to display/Qt/GUI issues, SKIP it and move to the next task
- When all executable tasks are done, STOP and provide your final summary -- do not keep retrying failed tasks
- Report what you accomplished and what failed in your final message

## You Never Execute Anything Yourself

Whatever the engine, you do not run code or commands -- the workflow engine does.
You never open a subprocess, never invoke a CLI directly, never write or run a
bash/PBS script, and never call `mpirun`/`srun` yourself. Every command-line
invocation must be expressed to the engine as a command *string* that the engine
executes on your behalf.

## Scientific Integrity Rules (CRITICAL)

- Do NOT generate synthetic, fake, or hardcoded data to simulate results from the paper
- Do NOT fabricate timing data, performance benchmarks, or scaling measurements
- Do NOT reproduce scaling plots, strong/weak scaling curves, or performance comparisons
  that require HPC infrastructure (MPI, multi-node, PBS) unavailable in the current environment
- ALL visualizations MUST use data produced by your own simulation runs in this session,
  not values copied from the paper or invented to look plausible
- If a task requires infrastructure you do not have (MPI, multi-node cluster, specific
  HPC hardware), SKIP it and explain why in your summary
- If the paper shows benchmark results on 40-1280 processes but you are running on a
  single machine, do NOT simulate those benchmarks -- skip them
- You CAN measure and plot actual single-machine timing of your own pipeline stages
  (e.g. how long particle generation, analysis, and visualization took on this run)

## Data Layout (MANDATORY -- do not deviate)

NOTE: /app/ paths are automatically resolved to the local repo directory at runtime.
You do not need an actual /app/ folder. Always use /app/ paths in your code --
the MCP server translates them to the correct local paths.

- Input data: /app/data/ (source files)
- Working directory: /app/work/run0/ (ALL output goes here)
- ALL output files, subdirectories, scripts, and results MUST be placed under /app/work/run0/
- Use consistent subdirectory names:
  - /app/work/run0/frames/       for simulation trajectory/frame output
  - /app/work/run0/renders/      for visualization images and GIFs
  - /app/work/run0/results.csv   for tabular analysis results
  - /app/work/run0/workflow.py   for generated workflow scripts
  - /app/work/run0/run_workflow.sh for launcher scripts
- Do NOT create output directories with arbitrary names (no "output/", "decaf_workflow_output/", 
  "smoketest/", etc.)
- Do NOT write files to /tmp/ -- always use /app/work/run0/
"""


# __ Post-Run Skill Review _____________________________________________________

SKILL_REVIEW_SYSTEM_PROMPT = """\
You are the Explorer agent, reviewing the run you just finished. The user has told you
the workflow did NOT execute properly. Your job is to work out the root cause from what
you actually observed, and write what should be added to the domain skill file so the
same problem does not happen on the next run.

You are NOT fixing anything now and NOT re-running anything. You are writing durable
guidance for future runs.

---

## What a skill file is

A skill file is domain knowledge written for a scientist. It is not source code, and it
is not a description of how this system is built internally. It tells a future run what
tool to use, where the files are, what comes out, what goes wrong, and what the rules of
the domain are.

The domain skill file has these sections. Each has a strict purpose -- put your addition
in the section it belongs to and nowhere else:

| Section | What belongs in it | What does NOT belong in it |
|---|---|---|
| `Tools` | The name of the tool that gets run, where it lives on disk, whether it is already installed. | Pitfalls, output names, domain rules. |
| `Input Parameters` | Which files hold the settings, where they live, which values are fixed and must not be invented, which input paths to use. | Descriptions of what the run produces, or of what went wrong. |
| `Outputs` | Names, paths, formats, and plain-language descriptions of the files the run produces. | Anything about workflow type, orchestration, which stage failed, or what the tool is. |
| `Pitfalls` | Things that tend to go wrong, in plain English, paired with what to do instead. | Source code, stack traces, function names, internal module names. |
| `Guidelines` | Rules of the domain: what must never be rebuilt or reimplemented, what is closed-source or pre-built, what must not be edited, what must not be re-run, required conventions. | Step-by-step procedure, or anything belonging to another section. |

---

## Hard constraints on what you may write

1. **No code.** No source code, snippets, or pseudocode. No function names, class names,
   module or package import names, API signatures, command lines, flags, environment
   variable names, or configuration keys.
2. **No internal system details.** Nothing about agents, routing, orchestration, tool
   calls, MCP, servers, state fields, or retries. The skill file describes the
   scientific workflow, not the machinery running it.
3. **Allowed technical content is exactly three things:**
   - **Paths** -- where tools live, where inputs live, where outputs are written.
   - **Inputs** -- which input files exist, what kind of file each is, what each holds.
   - **Outputs** -- which output files are produced, what format each is, what each contains.
4. **Errors are described in English, not pasted.** Say what the failure looks like to a
   person ("the output folder is empty after the run", "the counts come out roughly ten
   times too low"), never the literal error text, exit code, exception type, or traceback.
5. **Prefer an existing section.** Only propose a new section when the guidance genuinely
   fits none of the five. A new section is subject to every constraint above: it may not
   be about code, software engineering, or computer science beyond paths, inputs, and
   outputs.

---

## How to choose what to recommend

- Anchor on the root cause, not the symptom. If the visible failure was a missing output
  file but the real cause was a stage that ran in the wrong place, write about the cause.
- Write guidance that generalizes to a different paper in the same domain.
- Do not restate guidance the skill file already contains. If it already warns about this
  and the run ignored it, say so in your rationale and sharpen the existing wording.
- Keep each addition short and concrete: one or two sentences for a guideline, one table
  row for a pitfall, one bullet for an output.
- Match the formatting the target section already uses. If the section is a pitfalls
  table, your addition must be a table row. If it is a bullet list, a bullet.
- You ran this workflow yourself. Prefer what you actually observed over speculation.

---

Return ONLY a valid JSON object with exactly these keys:
- root_cause:      str -- plain-language analysis of what actually went wrong
- recommendations: list of objects, each with exactly these keys:
    - skill_file:     str -- skill path to edit, e.g. "use_cases/<name>/domain"
    - section:        str -- the exact section heading the addition goes under, without the leading '#'
    - is_new_section: bool -- true only if this section does not already exist in that file
    - addition:       str -- the exact markdown text to add, formatted to match the section
    - rationale:      str -- why this addition prevents a repeat of the issue
"""


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
        f"Domain: {state.get('domain', '') or '(unset)'}",
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
    work_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "work")
    if os.path.isdir(work_dir):
        produced = []
        for dirpath, _, filenames in os.walk(work_dir):
            for fn in filenames:
                full = os.path.join(dirpath, fn)
                try:
                    size = os.path.getsize(full)
                except OSError:
                    size = -1
                produced.append(f"  {full} ({size} bytes)")
        lines += ["", f"Files present under work/ after the run ({len(produced)}):"]
        lines += produced[:200]

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
    full = os.path.join(_SKILLS_ROOT, _redirect_skill(rel_path) + ".SKILL.md")
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
        runs_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "runs")
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

    server_params = StdioServerParameters(
        command=sys.executable,
        args=[server_path],
        env={
            **os.environ,
            "HOST_REPO_PATH": os.environ.get(
                "HOST_REPO_PATH",
                os.path.dirname(os.path.abspath(__file__)),
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

        # condition A never touches skills/ at all, not even to list use_cases/,
        # so the whole auto-detection block below just gets skipped
        _enabled = state.get("condition", "B") != "A"
        _base_skill = _read_skill("agents/explorer", enabled=_enabled)
        _uc_skill = ""
        if _enabled:
            _uc_dir = os.path.join(_SKILLS_ROOT, "use_cases")
            _domain_lower = (state.get("domain") or "").strip().lower()

            # prefer the explicit --domain flag over guessing. substring match both
            # ways since domain labels don't always match the folder name exactly
            # (--domain MOLECULAR vs the use_cases/molecular_nucleation/ folder)
            if _domain_lower and os.path.isdir(_uc_dir):
                for uc_name in os.listdir(_uc_dir):
                    uc_lower = uc_name.lower()
                    if _domain_lower in uc_lower or uc_lower in _domain_lower:
                        content = _read_skill(f"use_cases/{uc_name}/explorer")
                        if content:
                            _uc_skill = content
                            console.print(f"[dim cyan][explorer] loaded use case skill via --domain: {uc_name}[/dim cyan]")
                        break

            # fallback if --domain wasn't passed or didn't match: scan every
            # use_cases/*/explorer.SKILL.md and match on stack_decision package names.
            # shaky heuristic, package names rarely show up in a skill's own description
            if not _uc_skill:
                _stack_lower = [p.lower() for p in stack_decision]
                if os.path.isdir(_uc_dir):
                    for uc_name in os.listdir(_uc_dir):
                        content = _read_skill(f"use_cases/{uc_name}/explorer")
                        if not content:
                            continue
                        desc_lower = content[:500].lower()
                        if any(pkg in desc_lower for pkg in _stack_lower):
                            _uc_skill = content
                            console.print(f"[dim cyan][explorer] loaded use case skill via stack match: {uc_name}[/dim cyan]")
                            break

        # force-load the engine skills instead of leaving it to the model to call
        # load_skill("adios"), that's exactly how you get imports with no real API use.
        # systems/<engine> loads before knowledge/<ENGINE> so the operational rules
        # land before the long general reference
        _sys_skill_path, _know_skill_path = _ENGINE_SKILLS.get(engine, (None, None))
        _engine_skill = ""
        if _sys_skill_path:
            _engine_skill = "\n\n".join(filter(None, [
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
            _usage = extract_usage(response)
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

                if tool_name == "run_workflow":
                    console.print(Panel(
                        f"file={tool_args.get('filename', 'workflow.py')}",
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

                # Determine success/failure from the result
                tool_succeeded = False
                display_status = "?"
                try:
                    parsed = json.loads(tool_result)
                    # counts as success: status completed/success, exit_code 0, installed True,
                    # or a files/content key present. no error key either way
                    status_val = parsed.get("status")
                    exit_code = parsed.get("exit_code")
                    has_error = "error" in parsed

                    if status_val == "failed":
                        tool_succeeded = False
                        display_status = f"failed (exit {exit_code})"
                    elif has_error:
                        tool_succeeded = False
                        display_status = f"error: {parsed['error'][:80]}"
                    elif status_val in ("completed", "success"):
                        tool_succeeded = True
                        display_status = status_val
                    elif exit_code == 0:
                        tool_succeeded = True
                        display_status = "exit_code: 0"
                    elif parsed.get("installed") is True:
                        tool_succeeded = True
                        display_status = "installed"
                    elif parsed.get("installed") is False:
                        tool_succeeded = True  # query succeeded, package just isn't there
                        display_status = "not installed"
                    elif "files" in parsed:
                        tool_succeeded = True
                        display_status = f"{parsed.get('count', '?')} files"
                    elif "content" in parsed:
                        tool_succeeded = True
                        display_status = f"{parsed.get('total_lines', '?')} lines"
                    elif "version" in parsed:
                        tool_succeeded = True
                        display_status = f"v{parsed['version']}"
                    elif "tasks" in parsed:
                        tool_succeeded = True
                        display_status = f"{parsed.get('total', '?')} tasks"
                    elif "nnodes" in parsed:
                        # get_resources has no status field, nnodes present just means it worked
                        tool_succeeded = True
                        launcher = parsed.get("launcher") or "none"
                        display_status = f"nodes={parsed['nnodes']} ranks={parsed.get('ntasks',1)} launcher={launcher}"
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
                if tool_name == "write_workflow":
                    # surface the apps/tasks/stages the server actually found, so a
                    # structurally-wrong file is visible before it ever runs
                    try:
                        r = json.loads(tool_result)
                        _shape = {k: v for k, v in r.items() if k in (
                            "bash_apps", "python_apps", "tasks", "binary_tasks",
                            "mpi_tasks", "stages", "adios_writes", "adios_reads",
                            "launch_mode")}
                        if _shape:
                            console.print(f"[dim {color}][explorer]   {json.dumps(_shape)}[/dim {color}]")
                    except (json.JSONDecodeError, AttributeError):
                        pass
                if engine_verified is False:
                    console.print(
                        f"[bold red][explorer] WARNING: did not exercise the real "
                        f"{engine} API in '{tool_args.get('name', tool_name)}' "
                        f"(engine_backend={engine_backend})[/bold red]"
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
        work_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "work")
        artifacts = []
        if os.path.isdir(work_dir):
            for dirpath, _, filenames in os.walk(work_dir):
                for fn in filenames:
                    full = os.path.join(dirpath, fn)
                    try:
                        size_bytes = os.path.getsize(full)
                    except OSError:
                        size_bytes = -1
                    artifacts.append({"path": full, "size_bytes": size_bytes})
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
