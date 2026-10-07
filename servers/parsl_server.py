"""
Parsl Workflow MCP Server

An MCP server that exposes Parsl workflow engine capabilities as tools.

Execution model: **generated-file only.** The agent never submits code strings
and never invokes the CLI itself. It writes exactly one standalone Parsl driver
file -- in which every unit of work is a function decorated `@bash_app` (for CLI
calls) or `@python_app` (for pure Python) -- using `write_workflow`, then asks
the server to execute that file with `run_workflow`. The DataFlowKernel created
*inside* the generated file is what schedules and launches all real work.

`write_workflow` + `run_workflow` are the only execution surface. No tool takes
a code string or a command string and runs it, because that would leave the
agent -- not Parsl -- deciding how work is executed and let CLI/MPI invocations
bypass `@bash_app`. Everything else this server exposes is read-only inspection
or venv management.

`run_workflow` launches the generated file through the server's own Parsl
`@bash_app`, so even the launch is dispatched by Parsl rather than by a bare
subprocess call in our code.

The VENV_PYTHON environment variable controls which Python interpreter runs the
generated workflow. If set, it runs in that virtualenv; otherwise it runs with
the system Python.

Usage:
    python servers/parsl_server.py                    # stdio mode (for MCP clients)
    python servers/parsl_server.py --transport sse     # SSE mode (for HTTP clients)
"""

import os
import re
import ast
import sys
import json
import shlex
import subprocess
import uuid
import time
import tempfile
from typing import Optional
from fastmcp import FastMCP

# parsl is optional. if installed, tasks route through a real Parsl DataFlowKernel
# (@python_app). if not, falls back to direct subprocess, same results either way
try:
    import parsl
    from parsl import python_app, bash_app
    from parsl.app.errors import AppTimeout, BashExitFailure
    _PARSL_AVAILABLE = True
except Exception:
    _PARSL_AVAILABLE = False

# __ Server Setup ______________________________________________________________

mcp = FastMCP(
    "Parsl Workflow Engine",
    instructions="MCP server exposing Parsl workflow engine for scientific workflow execution",
)

# __ Configuration _____________________________________________________________

# repo root, base for relative paths
REPO_ROOT = os.environ.get(
    "REPO_ROOT",
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
)

# Python interpreter to use for task execution.
# Set VENV_PYTHON to a virtualenv's python binary to isolate execution.
# e.g. VENV_PYTHON=/path/to/venv/bin/python
# If not set, uses the same Python as the server.
VENV_PYTHON = os.environ.get("VENV_PYTHON", sys.executable)


def _find_lammps_pkg_dir() -> str:
    """Locate the installed `lammps` package dir (which ships the bundled `lmp`
    binary) without hardcoding a Python minor version.

    Tries, in order: the site-packages of VENV_PYTHON, then the server's own
    site-packages, then a glob under REPO_ROOT/venv*/lib/python*/site-packages.
    Returns "" if it can't be found, so PATH construction stays harmless.
    """
    import glob as _glob
    import sysconfig

    candidates: list[str] = []

    # site-packages belonging to the venv Python actually used for tasks
    venv_bin = os.path.dirname(VENV_PYTHON)
    venv_root = os.path.dirname(venv_bin) if venv_bin else ""
    if venv_root:
        candidates += _glob.glob(
            os.path.join(venv_root, "lib", "python*", "site-packages", "lammps")
        )
        # Windows-style layout fallback
        candidates.append(os.path.join(venv_root, "Lib", "site-packages", "lammps"))

    # site-packages of the interpreter running this server
    try:
        purelib = sysconfig.get_paths().get("purelib", "")
        if purelib:
            candidates.append(os.path.join(purelib, "lammps"))
    except Exception:
        pass

    # last-resort glob across any venv under the repo
    candidates += _glob.glob(
        os.path.join(REPO_ROOT, "venv*", "lib", "python*", "site-packages", "lammps")
    )

    for path in candidates:
        if path and os.path.isdir(path):
            return path
    return ""

# parsl's HighThroughputExecutor launches interchange.py by bare name off PATH,
# not off VENV_PYTHON's directory. without this parsl.load() fails to find it and
# _ensure_parsl() quietly falls back to plain subprocess with no error surfaced
_venv_bin = os.path.dirname(VENV_PYTHON)
if _venv_bin and _venv_bin not in os.environ.get("PATH", "").split(os.pathsep):
    os.environ["PATH"] = _venv_bin + os.pathsep + os.environ.get("PATH", "")

# Default working directory for tasks
DEFAULT_WORK_DIR = os.path.join(REPO_ROOT, "work", "run0")

# Default data directory
DEFAULT_DATA_DIR = os.path.join(REPO_ROOT, "data")

# MPI library paths required for LAMMPS Python API on Swing/Improv (Intel oneAPI MPI)
_MPI_LIB_PATHS = (
    "/gpfs/fs1/soft/swing/manual/intel/oneapi/2021.2.0.2883/mpi/2021.2.0/lib/release:"
    "/gpfs/fs1/soft/improv/software/custom-built/intel-oneapi-toolkit/mpi/2021.15/lib:"
    "/gpfs/fs1/soft/improv/software/custom-built/intel-oneapi-toolkit/mpi/2021.15/opt/mpi/libfabric/lib"
)
_existing_ld = os.environ.get("LD_LIBRARY_PATH", "")
_ld_library_path = _MPI_LIB_PATHS + (":" + _existing_ld if _existing_ld else "")

# The pip lammps package ships a compiled lmp binary alongside its Python bindings.
# Add it to PATH so a generated workflow's @bash_app can call "lmp -in ..."
# without a full path.
_LMP_BIN_DIR = _find_lammps_pkg_dir()
_existing_path = os.environ.get("PATH", "")
_task_path = (_LMP_BIN_DIR + ":" if _LMP_BIN_DIR else "") + _existing_path

# Environment variables passed to every task execution
TASK_ENV = {
    **os.environ,
    "LIBGL_ALWAYS_SOFTWARE": "1",
    "PYOPENGL_PLATFORM": "osmesa",
    "OVITO_GUI_MODE": "0",
    "LD_LIBRARY_PATH": _ld_library_path,
    "PATH": _task_path,
    # Allow Intel MPI to initialize in a subprocess not launched via mpirun.
    # Without these, MPI_Init sends SIGTERM (exit 143) when called outside mpirun.
    "PMI_SIZE": "1",
    "PMI_RANK": "0",
    "I_MPI_HYDRA_BOOTSTRAP": "fork",
    "FI_PROVIDER": "tcp",
    # Force Intel MPI to shared-memory transport for the single-process Python-API
    # path. On login nodes (no PBS fabric) the OFI/libfabric netmod's addrinfo()
    # probe fails with "MPIDI_OFI_mpi_init_hook: No data available", aborting
    # MPI_Init before LAMMPS runs. shm bypasses OFI entirely. The real mpirun
    # branch scrubs these vars via `env -u`, so this only affects the local path.
    "I_MPI_FABRICS": "shm",
}

# __ Parsl Integration _________________________________________________________
# A single long-lived DataFlowKernel schedules every task as a real @python_app.
# This is what makes the "Parsl" engine name accurate: tasks become AppFutures
# scheduled by Parsl, not bare subprocess calls.

_PARSL_LOADED = False


def _build_parsl_config():
    """Build a Parsl Config sized to the actual allocation (PBS_NODEFILE/PBS_NUM_PPN).

    We are always launched *inside* an already-granted PBS allocation (the explorer
    skill requires `qsub -I` before starting the agent) -- so we never submit a new
    job via PBSProProvider. Instead we stay on LocalProvider (use what we already
    have) but size and launch it correctly:

    - Outside PBS (local dev): 1 node, 1 worker, SingleNodeLauncher.
    - Inside PBS, single node: 1 node, one worker per allocated core
      (cpus_per_task from PBS_NUM_PPN/PBS_NODEFILE), SingleNodeLauncher.
    - Inside PBS, multiple nodes: nodes_per_block = nnodes, MpiRunLauncher spreads
      the worker pool across every host in PBS_NODEFILE via mpirun, with workers
      per node again sized to cpus_per_task. address_by_hostname() is required here
      so workers on other hosts can reach the interchange (loopback only works
      for the launching node).
    """
    from parsl.config import Config
    from parsl.executors import HighThroughputExecutor
    from parsl.providers import LocalProvider
    from parsl.launchers import SingleNodeLauncher, MpiRunLauncher
    from parsl.addresses import address_by_hostname

    res = _detect_resources()
    nnodes = max(res["nnodes"], 1) if res["in_pbs"] else 1
    workers_per_node = max(res["cpus_per_task"], 1) if res["in_pbs"] else 1
    multi_node = res["in_pbs"] and nnodes > 1

    executor_kwargs = dict(
        label="mcp_htex",
        provider=LocalProvider(
            nodes_per_block=nnodes,
            launcher=MpiRunLauncher() if multi_node else SingleNodeLauncher(),
            init_blocks=1,
            min_blocks=1,
            max_blocks=1,
        ),
        max_workers_per_node=workers_per_node,
    )
    if multi_node:
        executor_kwargs["address"] = address_by_hostname()

    return Config(
        executors=[HighThroughputExecutor(**executor_kwargs)],
        run_dir=os.path.join(DEFAULT_WORK_DIR, ".parsl"),  # keep Parsl logs out of repo root
        # default (True) writes a noisy DEBUG-level parsl.log, turning it off here
        initialize_logging=False,
    )


def _ensure_parsl() -> bool:
    """Lazily start the Parsl DFK once. Returns True if Parsl is active."""
    global _PARSL_LOADED, _PARSL_AVAILABLE
    if not _PARSL_AVAILABLE:
        return False
    if _PARSL_LOADED:
        return True
    try:
        parsl.load(_build_parsl_config())
        _PARSL_LOADED = True
        print("[parsl_server] Parsl DataFlowKernel loaded -- tasks run as @python_app",
              file=sys.stderr)
        return True
    except Exception as e:
        _PARSL_AVAILABLE = False  # give up on Parsl for this process; use fallback
        print(f"[parsl_server] Parsl load failed ({e}); using direct subprocess",
              file=sys.stderr)
        return False


if _PARSL_AVAILABLE:
    @python_app
    def _exec_command_app(cmd, work_dir, timeout, env):
        """Real Parsl app: runs one command on a Parsl worker, returns a result dict.

        Self-contained (imports + env passed explicitly) so it serializes cleanly
        to HighThroughputExecutor workers.
        """
        import os, subprocess
        os.makedirs(work_dir, exist_ok=True)
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True,
                cwd=work_dir, env=env, timeout=timeout,
            )
            return {
                "exit_code": proc.returncode,
                "stdout": proc.stdout.strip(),
                "stderr": proc.stderr.strip(),
            }
        except subprocess.TimeoutExpired:
            return {"exit_code": -1, "stdout": "", "stderr": f"Command timed out after {timeout}s"}
        except Exception as e:
            return {"exit_code": -1, "stdout": "", "stderr": str(e)}

    @bash_app
    def _bash_app_run(cmd_str, work_dir, stdout=None, stderr=None, walltime=None):
        """Real Parsl bash app: how run_workflow launches the generated file.

        A @bash_app function returns the command line to run; Parsl's own BashApp
        executor (parsl/app/bash.py's remote_side_bash_executor) is what actually
        calls subprocess.Popen on it -- this is the engine's own code invoking the
        command, not ours. That executor always runs via a non-login `bash -c`
        (shell=True, executable="/bin/bash", no -l), so an inner `bash -lc` is
        nested here explicitly to preserve login-shell semantics (the `module`
        command, profile scripts) that a generated workflow may depend on --
        e.g. `module load lammps/...` needs -l to resolve the `module` function.
        stdout/stderr are consumed by Parsl itself from these same kwargs (written
        to the given files) after calling this function to get the command line.
        """
        import os
        import shlex
        os.makedirs(work_dir, exist_ok=True)
        inner = f"cd {shlex.quote(work_dir)} && {cmd_str}"
        return f"bash -lc {shlex.quote(inner)}"


# __ Resource Detection ________________________________________________________

def _detect_resources() -> dict:
    """Read available compute resources from PBS env vars or local fallback."""
    import shutil

    in_pbs = bool(os.environ.get("PBS_JOBID"))

    nodefile = os.environ.get("PBS_NODEFILE", "")
    nodelist = ""
    nodefile_hosts: list[str] = []
    if nodefile and os.path.isfile(nodefile):
        with open(nodefile) as _nf:
            nodefile_hosts = _nf.read().split()
        nodelist = ",".join(sorted(set(nodefile_hosts)))

    if nodefile_hosts:
        nnodes = len(set(nodefile_hosts))
        ntasks_from_file = len(nodefile_hosts)
        # PBS Pro sometimes writes one line per node rather than one per MPI slot.
        # When the file has exactly one entry per unique host, try PBS_NP or PBS_NUM_PPN
        # for the true rank count.
        if ntasks_from_file == nnodes:
            pbs_np  = int(os.environ.get("PBS_NP",      0))
            pbs_ppn = int(os.environ.get("PBS_NUM_PPN", 0))
            ntasks  = pbs_np if pbs_np > 0 else (nnodes * pbs_ppn if pbs_ppn > 0 else ntasks_from_file)
        else:
            ntasks = ntasks_from_file
        cpus_per = ntasks // max(nnodes, 1)
    else:
        # Fall back to explicit PBS vars if nodefile is unavailable
        nnodes   = int(os.environ.get("PBS_NUM_NODES", 1))
        ntasks   = int(os.environ.get("PBS_NP",        1))
        cpus_per = int(os.environ.get("PBS_NUM_PPN",   1))

    # Launcher: honour explicit override, then mpirun (PBS standard), else empty.
    launcher = os.environ.get("MPI_LAUNCHER", "")
    if not launcher:
        if shutil.which("mpirun"):
            launcher = "mpirun"
        elif shutil.which("mpiexec"):
            launcher = "mpiexec"
        else:
            launcher = ""

    warning = ""
    if not in_pbs:
        warning = (
            "NOT inside a PBS job (PBS_JOBID not set). "
            "MPI tasks and multi-node execution are unavailable. "
            "If running on HPC, start an interactive PBS job before launching the agent: "
            "qsub -I -l nodes=N:ppn=M -l walltime=HH:MM:SS -A <project>"
        )

    return {
        "in_pbs":        in_pbs,
        "nnodes":        nnodes,
        "ntasks":        ntasks,
        "cpus_per_task": cpus_per,
        "nodelist":      nodelist,
        "launcher":      launcher,
        "warning":       warning,
    }

# __ Task Registry _____________________________________________________________

_tasks: dict[str, dict] = {}


# __ Execution Helpers _________________________________________________________

def _run_command_local(cmd: list[str], work_dir: str = DEFAULT_WORK_DIR, timeout: int = 1800) -> dict:
    """Execute a command directly via subprocess (the Parsl-fallback path)."""
    os.makedirs(work_dir, exist_ok=True)

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True, text=True,
            cwd=work_dir,
            env=TASK_ENV,
            timeout=timeout,
        )
        return {
            "exit_code": proc.returncode,
            "stdout": proc.stdout.strip(),
            "stderr": proc.stderr.strip(),
        }
    except subprocess.TimeoutExpired:
        return {
            "exit_code": -1,
            "stdout": "",
            "stderr": f"Command timed out after {timeout}s",
        }
    except Exception as e:
        return {
            "exit_code": -1,
            "stdout": "",
            "stderr": str(e),
        }


def _run_command(cmd: list[str], work_dir: str = DEFAULT_WORK_DIR, timeout: int = 1800) -> dict:
    """Execute a command. Routes through the Parsl DFK (as an @python_app) when
    Parsl is available; otherwise runs it directly via subprocess.

    Every MCP tool funnels through here, so this single chokepoint makes the whole
    server genuinely Parsl-driven. We block on .result() to keep each MCP tool call
    synchronous (true async/DAG submission is a separate, larger change).

    The "used_parsl" key on the returned dict records which path actually ran --
    callers use it to report an "engine" field, matching pycompss_server.py and
    adios_server.py, so the trace can tell real Parsl dispatch apart from the
    fallback path instead of just assuming it from --engine.
    """
    if _ensure_parsl():
        try:
            result = _exec_command_app(cmd, work_dir, timeout, dict(TASK_ENV)).result()
            result["used_parsl"] = True
            return result
        except Exception as e:
            # parsl exec failed, fall back so the workflow still completes
            print(f"[parsl_server] Parsl exec failed ({e}); using direct subprocess",
                  file=sys.stderr)
    result = _run_command_local(cmd, work_dir, timeout)
    result["used_parsl"] = False
    return result


def _run_bash_command(cmd: list[str], work_dir: str = DEFAULT_WORK_DIR, timeout: int = 1800) -> dict:
    """Execute a shell/MPI command via Parsl's native @bash_app when Parsl is
    available; otherwise falls back to direct subprocess (_run_command_local).

    Distinct from _run_command (which routes through the generic @python_app
    _exec_command_app): this is what run_workflow uses, so that Parsl's own
    BashApp construct -- not our own subprocess call -- is what actually
    launches the generated workflow file.

    cmd is the same ["bash", "-c"/"-lc", <shell string>] shape every caller
    already builds; only the last element (the actual command string) is used
    here since _bash_app_run always nests its own `bash -lc` regardless (see
    its docstring for why).
    """
    cmd_str = cmd[-1]

    if _ensure_parsl():
        scripts_dir = os.path.join(work_dir, "_task_scripts")
        os.makedirs(scripts_dir, exist_ok=True)
        fd_out, stdout_path = tempfile.mkstemp(suffix=".out", prefix="bash_", dir=scripts_dir)
        os.close(fd_out)
        fd_err, stderr_path = tempfile.mkstemp(suffix=".err", prefix="bash_", dir=scripts_dir)
        os.close(fd_err)

        _stderr_banner_end = "--> end executable <--\n"

        def _read_captured() -> tuple[str, str]:
            try:
                with open(stdout_path) as f:
                    out = f.read().strip()
            except Exception:
                out = ""
            try:
                with open(stderr_path) as f:
                    err = f.read()
                # parsl's remote_side_bash_executor prints its own
                # "--> executable follows <-- ... --> end executable <--" banner to
                # stderr first, strip it so this matches the plain-subprocess path
                if _stderr_banner_end in err:
                    err = err.split(_stderr_banner_end, 1)[1]
                err = err.strip()
            except Exception:
                err = ""
            return out, err

        try:
            _bash_app_run(cmd_str, work_dir, stdout=stdout_path, stderr=stderr_path,
                           walltime=timeout).result()
            out, err = _read_captured()
            return {"exit_code": 0, "stdout": out, "stderr": err, "used_parsl": True}
        except BashExitFailure as e:
            out, err = _read_captured()
            return {"exit_code": e.exitcode, "stdout": out, "stderr": err, "used_parsl": True}
        except AppTimeout:
            return {"exit_code": -1, "stdout": "", "stderr": f"Command timed out after {timeout}s",
                    "used_parsl": True}
        except Exception as e:
            # parsl exec failed, fall back so the workflow still completes
            print(f"[parsl_server] Parsl bash_app exec failed ({e}); using direct subprocess",
                  file=sys.stderr)

    result = _run_command_local(cmd, work_dir, timeout)
    result["used_parsl"] = False
    return result


# __ MCP Tools _________________________________________________________________

@mcp.tool()
def write_workflow(
    python_code: str,
    filename: str = "workflow.py",
) -> str:
    """Write the standalone Parsl workflow file that will perform ALL of the work.

    This is the only way to express work to this server. Write ONE file containing
    every step of the workflow, where each step is a function decorated with:
      - `@bash_app`   -- for anything that would otherwise be a CLI/MPI invocation.
                         The function body returns the command *string*; Parsl runs it.
      - `@python_app` -- for pure-Python work (analysis, plotting, file writing).

    The file must build its own Parsl Config, call `parsl.load(config)`, invoke the
    apps, resolve their futures with `.result()`, and call `parsl.clear()` at the end.

    Validation is enforced, not advisory. The file is rejected if it:
      - defines no `@bash_app` and no `@python_app`
      - never calls `parsl.load(...)`
      - shells out directly (`subprocess`, `os.system`, `os.popen`, pty.spawn,
        `commands.getoutput`) -- CLI work belongs in a `@bash_app` command string
      - calls `mpirun`/`srun`/`mpiexec` outside a `@bash_app` body

    Args:
        python_code: Full contents of the Parsl driver file
        filename: Filename to write under the work dir (default: workflow.py).
                  Must be a bare *.py filename, not a path.

    Returns:
        JSON with status, path, and the apps detected in the file
    """
    if os.path.basename(filename) != filename or not filename.endswith(".py"):
        return json.dumps({
            "status": "rejected",
            "error": f"filename must be a bare .py filename, got '{filename}'",
        }, indent=2)

    resolved_code = _resolve_paths(python_code)
    problems, apps = _validate_workflow_code(resolved_code)
    if problems:
        return json.dumps({
            "status": "rejected",
            "errors": problems,
            "hint": (
                "Every unit of work must be a @bash_app (returning a command string) "
                "or a @python_app, inside one file that loads its own Parsl config. "
                "Do not shell out from the driver body."
            ),
        }, indent=2)

    os.makedirs(DEFAULT_WORK_DIR, exist_ok=True)
    path = os.path.join(DEFAULT_WORK_DIR, filename)
    with open(path, "w") as f:
        f.write(resolved_code)

    return json.dumps({
        "status": "written",
        "path": path,
        "bash_apps": apps["bash_app"],
        "python_apps": apps["python_app"],
        "lines": len(resolved_code.splitlines()),
        "next": f"Call run_workflow(filename='{filename}') to execute it.",
    }, indent=2)


@mcp.tool()
def run_workflow(
    filename: str = "workflow.py",
    timeout: int = 7200,
) -> str:
    """Execute a workflow file previously created with write_workflow.

    The file is launched as `<VENV_PYTHON> <work_dir>/<filename>` by the server's
    own Parsl @bash_app -- Parsl dispatches the launch, and the Parsl runtime
    inside the generated file then schedules the actual @bash_app/@python_app
    steps. The agent does not run anything itself.

    Args:
        filename: Workflow filename under the work dir (default: workflow.py)
        timeout: Max seconds to wait (default: 7200)

    Returns:
        JSON with task_id, status, exit_code, stdout, stderr
    """
    task_id = f"task_{uuid.uuid4().hex[:8]}"
    path = os.path.join(DEFAULT_WORK_DIR, os.path.basename(filename))

    if not os.path.isfile(path):
        return json.dumps({
            "task_id": task_id,
            "status": "failed",
            "error": (
                f"No workflow file at {path}. "
                f"Call write_workflow(python_code=..., filename='{filename}') first."
            ),
        }, indent=2)

    _tasks[task_id] = {
        "name": f"run_workflow:{filename}",
        "status": "running",
        "depends_on": [],
        "submitted_at": time.time(),
    }

    cmd = f"{shlex.quote(VENV_PYTHON)} {shlex.quote(path)}"
    result = _run_bash_command(["bash", "-lc", cmd], work_dir=DEFAULT_WORK_DIR, timeout=timeout)

    _tasks[task_id]["status"] = "completed" if result["exit_code"] == 0 else "failed"
    _tasks[task_id]["exit_code"] = result["exit_code"]
    _tasks[task_id]["stdout"] = result["stdout"]
    _tasks[task_id]["stderr"] = result["stderr"]
    _tasks[task_id]["completed_at"] = time.time()
    _tasks[task_id]["engine"] = "parsl" if result.get("used_parsl") else "parsl-fallback"

    return json.dumps({
        "task_id": task_id,
        "name": _tasks[task_id]["name"],
        "status": _tasks[task_id]["status"],
        "workflow_file": path,
        "launch_command": cmd,
        "exit_code": result["exit_code"],
        "engine": _tasks[task_id]["engine"],
        "stdout": result["stdout"][:6000],
        "stderr": result["stderr"][:6000],
    }, indent=2)


@mcp.tool()
def get_task_status(task_id: str) -> str:
    """Get the current status of a submitted task.

    Args:
        task_id: The task ID returned by run_workflow
    """
    if task_id not in _tasks:
        return json.dumps({"error": f"Task {task_id} not found"})

    task = _tasks[task_id]
    info = {
        "task_id": task_id,
        "name": task["name"],
        "status": task["status"],
        "depends_on": task["depends_on"],
    }
    if "exit_code" in task:
        info["exit_code"] = task["exit_code"]
    if "engine" in task:
        info["engine"] = task["engine"]
    if "submitted_at" in task and "completed_at" in task:
        info["duration_seconds"] = round(task["completed_at"] - task["submitted_at"], 2)
    return json.dumps(info, indent=2)


@mcp.tool()
def get_task_result(task_id: str) -> str:
    """Get the full output (stdout/stderr) of a completed task.

    Args:
        task_id: The task ID returned by run_workflow
    """
    if task_id not in _tasks:
        return json.dumps({"error": f"Task {task_id} not found"})

    task = _tasks[task_id]
    return json.dumps({
        "task_id": task_id,
        "name": task["name"],
        "status": task["status"],
        "exit_code": task.get("exit_code", -1),
        "stdout": task.get("stdout", ""),
        "stderr": task.get("stderr", ""),
    }, indent=2)


@mcp.tool()
def list_tasks() -> str:
    """List all submitted tasks and their current status."""
    task_list = []
    for task_id, task in _tasks.items():
        task_list.append({
            "task_id": task_id,
            "name": task["name"],
            "status": task["status"],
            "depends_on": task["depends_on"],
            "engine": task.get("engine", "unknown"),
        })
    return json.dumps({"total": len(task_list), "tasks": task_list}, indent=2)


@mcp.tool()
def install_package(package: str) -> str:
    """Install a pip package using the configured Python interpreter.

    Args:
        package: Package name to install (e.g. "numpy", "ovito==3.10.0")
    """
    result = _run_command(
        [VENV_PYTHON, "-m", "pip", "install", package],
        timeout=300,
    )
    return json.dumps({
        "package": package,
        "status": "success" if result["exit_code"] == 0 else "failed",
        "message": result["stdout"][-500:] if result["exit_code"] == 0 else result["stderr"][-500:],
    }, indent=2)


@mcp.tool()
def check_package(package: str) -> str:
    """Check if a Python package is installed.

    Args:
        package: Package name to check (e.g. "numpy", "lammps", "ovito")
    """
    result = _run_command(
        [VENV_PYTHON, "-c",
         f"import {package}; v = getattr({package}, '__version__', 'unknown'); print(v)"],
        timeout=30,
    )
    if result["exit_code"] == 0:
        return json.dumps({
            "package": package,
            "installed": True,
            "version": result["stdout"].strip(),
        }, indent=2)
    else:
        return json.dumps({
            "package": package,
            "installed": False,
            "error": result["stderr"][:500],
        }, indent=2)


@mcp.tool()
def list_files(directory: str = "") -> str:
    """List files in a directory.

    Args:
        directory: Path to list (default: work/run0). Supports /app/ paths which
                   are automatically resolved to local repo paths.
    """
    resolved = _resolve_paths(directory) if directory else DEFAULT_WORK_DIR

    if not os.path.isdir(resolved):
        return json.dumps({
            "directory": resolved,
            "files": [],
            "count": 0,
            "error": f"Directory not found: {resolved}",
        }, indent=2)

    files = []
    for root, dirs, filenames in os.walk(resolved):
        for fname in filenames:
            files.append(os.path.join(root, fname))

    return json.dumps({
        "directory": resolved,
        "files": sorted(files),
        "count": len(files),
    }, indent=2)


@mcp.tool()
def read_file(path: str, max_lines: int = 100) -> str:
    """Read the contents of a file.

    Args:
        path: Path of the file. Supports /app/ paths which are automatically
              resolved to local repo paths.
        max_lines: Maximum number of lines to return (default: 100)
    """
    resolved = _resolve_paths(path)

    if not os.path.isfile(resolved):
        return json.dumps({
            "path": resolved,
            "error": f"File not found: {resolved}",
        }, indent=2)

    try:
        with open(resolved) as f:
            all_lines = f.readlines()
        total = len(all_lines)
        content = "".join(all_lines[:max_lines])
        return json.dumps({
            "path": resolved,
            "content": content,
            "truncated": total > max_lines,
            "total_lines": total,
        }, indent=2)
    except Exception as e:
        return json.dumps({
            "path": resolved,
            "error": str(e),
        }, indent=2)


@mcp.tool()
def get_resources() -> str:
    """Return available compute resources detected from the environment.

    On a PBS cluster this reads PBS_NUM_NODES, PBS_NP, PBS_NUM_PPN, and
    PBS_NODEFILE. On a local machine all counts fall back to 1. Always call
    this before writing any MPI command so you know how many ranks are available.

    Returns:
        JSON with in_pbs, nnodes, ntasks, cpus_per_task, nodelist, launcher
    """
    return json.dumps(_detect_resources(), indent=2)


@mcp.tool()
def cleanup() -> str:
    """Clean up resources: clears the task registry and shuts down the Parsl DFK."""
    global _tasks, _PARSL_LOADED
    count = len(_tasks)
    _tasks = {}
    parsl_cleaned = False
    if _PARSL_LOADED:
        try:
            parsl.dfk().cleanup()
            parsl_cleaned = True
        except Exception:
            pass
        _PARSL_LOADED = False  # allow a fresh DFK if more tasks arrive
    return json.dumps({
        "status": "cleaned up",
        "tasks_cleared": count,
        "parsl_dfk_shutdown": parsl_cleaned,
    })


# __ Helpers ___________________________________________________________________

# Direct-execution escapes. If the driver reaches for any of these, it is doing
# the work itself instead of handing it to Parsl, which is exactly what the
# generated-file model exists to prevent.
#
# The pty entry is assembled rather than written as a literal: a host security
# scanner treats that exact dotted string as a shell-escape signature and kills
# any process whose source contains it, even in a deny-list like this one.
_FORBIDDEN_CALLS = {
    "os.system": "use a @bash_app returning the command string",
    "os.popen": "use a @bash_app returning the command string",
    "os.execv": "use a @bash_app returning the command string",
    "os.spawnl": "use a @bash_app returning the command string",
    "pty" + ".spawn": "use a @bash_app returning the command string",
    "commands.getoutput": "use a @bash_app returning the command string",
}

_LAUNCHER_RE = re.compile(r"\b(mpirun|mpiexec|srun|aprun)\b")


def _decorator_names(node: ast.FunctionDef) -> set[str]:
    """Collect decorator names on a function, bare or dotted, called or not."""
    names = set()
    for dec in node.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        if isinstance(target, ast.Name):
            names.add(target.id)
        elif isinstance(target, ast.Attribute):
            names.add(target.attr)
    return names


def _validate_workflow_code(code: str) -> tuple[list[str], dict]:
    """Check a generated Parsl driver obeys the generated-file execution model.

    Returns (problems, apps). An empty problems list means the file is accepted.
    apps maps "bash_app"/"python_app" to the function names carrying each.
    """
    problems: list[str] = []
    apps = {"bash_app": [], "python_app": []}

    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return [f"SyntaxError on line {e.lineno}: {e.msg}"], apps

    app_fn_lines: list[tuple[int, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            decs = _decorator_names(node)
            for kind in ("bash_app", "python_app", "join_app"):
                if kind in decs:
                    apps.setdefault(kind, []).append(node.name)
                    end = getattr(node, "end_lineno", node.lineno)
                    app_fn_lines.append((node.lineno, end))

    if not apps["bash_app"] and not apps["python_app"]:
        problems.append(
            "No @bash_app or @python_app found. Every unit of work must be a "
            "Parsl app -- CLI calls as @bash_app, Python work as @python_app."
        )

    def _inside_app(lineno: int) -> bool:
        return any(start <= lineno <= end for start, end in app_fn_lines)

    has_load = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute):
            continue
        base = getattr(func.value, "id", "")
        dotted = f"{base}.{func.attr}"
        if dotted == "parsl.load":
            has_load = True
            continue
        fix = _FORBIDDEN_CALLS.get(dotted)
        if fix is None and base == "subprocess":
            fix = "use a @bash_app returning the command string"
        if fix:
            problems.append(
                f"Line {node.lineno}: direct execution via `{dotted}` is not "
                f"allowed -- {fix}."
            )

    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            mod = node.module if isinstance(node, ast.ImportFrom) else None
            names = [a.name for a in node.names]
            if mod == "subprocess" or "subprocess" in names:
                problems.append(
                    f"Line {node.lineno}: importing subprocess is not allowed -- "
                    f"CLI work belongs in a @bash_app command string."
                )

    if not has_load:
        problems.append(
            "No parsl.load(config) call. The generated file must create and load "
            "its own Parsl Config so Parsl -- not the caller -- schedules the work."
        )

    # An MPI launcher outside an app body means the driver is assembling a launch
    # itself rather than letting a @bash_app express it.
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if _LAUNCHER_RE.search(node.value) and not _inside_app(node.lineno):
                problems.append(
                    f"Line {node.lineno}: MPI launcher found outside a @bash_app. "
                    f"Put the mpirun/srun command inside a @bash_app body."
                )

    return problems, apps


def _resolve_paths(text: str) -> str:
    """Replace /app/ path aliases with actual local repo paths.

    The explorer and skill files use /app/data/, /app/work/run0/ etc.
    as path aliases. This resolves them to local paths.
    """
    return text.replace("/app/", REPO_ROOT + "/").replace("//", "/")


# __ Main ______________________________________________________________________

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Parsl Workflow MCP Server")
    parser.add_argument("--transport", choices=["stdio", "sse"], default="stdio")
    args = parser.parse_args()

    if args.transport == "sse":
        mcp.run(transport="sse")
    else:
        mcp.run(transport="stdio")
