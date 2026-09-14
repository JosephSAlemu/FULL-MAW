"""
ADIOS2 Workflow MCP Server

An MCP server that exposes ADIOS2 workflow engine capabilities as tools.
The explorer agent connects to this server via MCP protocol to submit tasks,
check status, get results, and manage the workflow execution.

ADIOS2 (Adaptable Input/Output System) is a high-performance I/O framework
for scientific simulations. It provides streaming data transport between
workflow stages, supporting both file-based (BP format) and in-memory
(SST, DataMan) data exchange.

When the ADIOS2 Python bindings are available, tasks use adios2 for data I/O
between pipeline stages. When ADIOS2 is NOT available, tasks fall back to
numpy file I/O (.npy/.npz) — same result, just without ADIOS2 optimizations.

This server executes tasks locally (or in a virtual environment) using subprocess.
Compatible with HPC environments where ADIOS2 is installed.

The VENV_PYTHON environment variable controls which Python interpreter to use.

Usage:
    python servers/adios_server.py                    # stdio mode
    python servers/adios_server.py --transport sse     # SSE mode
"""

import os
import sys
import json
import subprocess
import uuid
import time
import tempfile
from typing import Optional
from fastmcp import FastMCP

# __ Server Setup ______________________________________________________________

mcp = FastMCP(
    "ADIOS2 Workflow Engine",
    instructions="MCP server exposing ADIOS2 workflow engine for scientific workflow execution with high-performance I/O",
)

# __ Configuration _____________________________________________________________

REPO_ROOT = os.environ.get(
    "REPO_ROOT",
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
)

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

    venv_bin = os.path.dirname(VENV_PYTHON)
    venv_root = os.path.dirname(venv_bin) if venv_bin else ""
    if venv_root:
        candidates += _glob.glob(
            os.path.join(venv_root, "lib", "python*", "site-packages", "lammps")
        )
        candidates.append(os.path.join(venv_root, "Lib", "site-packages", "lammps"))

    try:
        purelib = sysconfig.get_paths().get("purelib", "")
        if purelib:
            candidates.append(os.path.join(purelib, "lammps"))
    except Exception:
        pass

    candidates += _glob.glob(
        os.path.join(REPO_ROOT, "venv*", "lib", "python*", "site-packages", "lammps")
    )

    for path in candidates:
        if path and os.path.isdir(path):
            return path
    return ""


DEFAULT_WORK_DIR = os.path.join(REPO_ROOT, "work", "run0")
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
_LMP_BIN_DIR = _find_lammps_pkg_dir()
_existing_path = os.environ.get("PATH", "")
_task_path = (_LMP_BIN_DIR + ":" if _LMP_BIN_DIR else "") + _existing_path

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

# __ ADIOS2 Runtime Detection __________________________________________________

_adios2_available: Optional[bool] = None


def _check_adios2() -> bool:
    """Check if the ADIOS2 Python bindings are available."""
    global _adios2_available
    if _adios2_available is not None:
        return _adios2_available

    result = _run_command(
        [VENV_PYTHON, "-c", "import adios2; print(adios2.__version__)"],
        timeout=15,
    )
    _adios2_available = result["exit_code"] == 0
    return _adios2_available


# __ Real ADIOS2 Usage Verification ____________________________________________
# unlike pycompss's @task wrapping, we can't structurally force ADIOS2 usage for
# arbitrary submit_task code, it's an I/O library not a task scheduler, so there's
# no single call-site to wrap. this is the server-side source of truth for whether
# submitted code actually called a real API vs just having adios2 importable.
# the "engine" field below is what mcp_explorer.py's trace classifier reads now,
# the content scan used to live client-side and moved here

_ADIOS_API_MARKERS = (
    "adios2.open(", ".declare_io(", ".set_engine(", "adios2.ADIOS(",
    ".begin_step(", ".end_step(", "adios2.Stream(", ".Stream(",
)
# write_bp/read_bp pre-open the Stream themselves (see _wrap_as_bp_stream_task),
# so what matters there is whether the user code calls a method on it, not
# whether it opened one.
_STREAM_USAGE_MARKERS = (".write(", ".read(", ".write_attribute(", ".read_attribute(")
_ADIOS_IMPORT_MARKERS = ("import adios2", "from adios2")


def _adios_engine_state(python_code: str, markers: tuple = _ADIOS_API_MARKERS,
                         require_intent: bool = True) -> str:
    """Classify real ADIOS2 usage for the "engine" field:
    - "adios2-fallback": package unavailable
    - "adios2-n/a":      package available, but the code shows no intent to use
                          it (no adios2 import) -- most submit_task calls in an
                          ADIOS run (LAMMPS, OVITO, rendering, ...) have nothing
                          to do with ADIOS2 at all, and flagging those as
                          "unused" would be noise, not a real signal.
    - "adios2-unused":   intent shown (an import, or for write_bp/read_bp just
                          being called at all) but no real API call found
    - "adios2":          package available and genuinely used

    require_intent=True (submit_task/submit_shell_task/submit_mpi_task): only
    "adios2-unused" when the code actually tries to touch adios2 but never
    calls its API; "adios2-n/a" when it doesn't mention adios2 at all.
    require_intent=False (write_bp/read_bp): every call is meant to do real
    I/O -- the server already opened the Stream for the caller, so never
    calling .write(/.read( on it is always meaningful. No "n/a" case applies.
    """
    if not _check_adios2():
        return "adios2-fallback"
    if any(marker in python_code for marker in markers):
        return "adios2"
    if require_intent and not any(m in python_code for m in _ADIOS_IMPORT_MARKERS):
        return "adios2-n/a"
    return "adios2-unused"


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
        if ntasks_from_file == nnodes:
            pbs_np  = int(os.environ.get("PBS_NP",      0))
            pbs_ppn = int(os.environ.get("PBS_NUM_PPN", 0))
            ntasks  = pbs_np if pbs_np > 0 else (nnodes * pbs_ppn if pbs_ppn > 0 else ntasks_from_file)
        else:
            ntasks = ntasks_from_file
        cpus_per = ntasks // max(nnodes, 1)
    else:
        nnodes   = int(os.environ.get("PBS_NUM_NODES", 1))
        ntasks   = int(os.environ.get("PBS_NP",        1))
        cpus_per = int(os.environ.get("PBS_NUM_PPN",   1))

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

def _run_command(cmd: list[str], work_dir: str = DEFAULT_WORK_DIR, timeout: int = 1800) -> dict:
    """Execute a command locally (or in venv) and return results."""
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


def _run_python_script(script: str, work_dir: str = DEFAULT_WORK_DIR, timeout: int = 1800) -> dict:
    """Write a Python script to a file and execute it.

    Kept (not deleted after running) in _task_scripts/ so the actual code submitted
    to this engine -- including the ADIOS2 read/write wrapping _wrap_as_adios_task()
    injects -- is inspectable after the run, not just visible in the trace.json args
    field.
    """
    scripts_dir = os.path.join(work_dir, "_task_scripts")
    os.makedirs(scripts_dir, exist_ok=True)

    fd, script_path = tempfile.mkstemp(suffix=".py", prefix="task_", dir=scripts_dir)
    with os.fdopen(fd, "w") as f:
        f.write(script)
    return _run_command([VENV_PYTHON, script_path], work_dir=work_dir, timeout=timeout)


def _wrap_as_adios_task(python_code: str) -> str:
    """Wrap user code in an ADIOS2-aware script.

    If ADIOS2 is available, injects adios2 import and makes the adios2 module
    accessible to the user code. Otherwise, wraps with plain try/except for
    direct execution using numpy file I/O as fallback.
    """
    if _check_adios2():
        return f"""\
import sys, os, traceback

os.makedirs("{DEFAULT_WORK_DIR}", exist_ok=True)
os.chdir("{DEFAULT_WORK_DIR}")

# ADIOS2 is available -- import it for use in task code
import adios2

try:
    # --- User task code (ADIOS2 mode) ---
{_indent(python_code, 4)}
    # --- End user code ---
    print("__TASK_SUCCESS__")
except Exception as e:
    print(f"__TASK_FAILED__: {{e}}", file=sys.stderr)
    traceback.print_exc(file=sys.stderr)
    sys.exit(1)
"""
    else:
        return f"""\
import sys, os, traceback

os.makedirs("{DEFAULT_WORK_DIR}", exist_ok=True)
os.chdir("{DEFAULT_WORK_DIR}")

try:
    # --- User task code (ADIOS2 fallback: numpy file I/O) ---
{_indent(python_code, 4)}
    # --- End user code ---
    print("__TASK_SUCCESS__")
except Exception as e:
    print(f"__TASK_FAILED__: {{e}}", file=sys.stderr)
    traceback.print_exc(file=sys.stderr)
    sys.exit(1)
"""


def _wrap_as_bp_stream_task(python_code: str, bp_path: str, mode: str) -> str:
    """Wrap user code inside a server-opened adios2.Stream(bp_path, mode) context.

    Structural enforcement rather than just detection -- a real Stream object
    exists regardless of what the user code does, the same idea as
    pycompss_server.py's _wrap_as_compss_task forcing real @task usage, adapted
    for an I/O library (one open call-site) instead of a task scheduler.
    """
    return f"""\
import sys, os, traceback
import adios2

os.makedirs("{DEFAULT_WORK_DIR}", exist_ok=True)
os.chdir("{DEFAULT_WORK_DIR}")

try:
    with adios2.Stream("{bp_path}", "{mode}") as stream:
{_indent(python_code, 8)}
    print("__TASK_SUCCESS__")
except Exception as e:
    print(f"__TASK_FAILED__: {{e}}", file=sys.stderr)
    traceback.print_exc(file=sys.stderr)
    sys.exit(1)
"""


def _run_and_finalize(task_id: str, name: str, wrapped_script: str,
                       timeout: int, engine: str, work_dir: str = DEFAULT_WORK_DIR,
                       depends_on: list[str] | None = None) -> dict:
    """Shared tail for submit_task/write_bp/read_bp: register, run, update status,
    and build the response dict. Returns the dict (caller still json.dumps's it)."""
    _tasks[task_id] = {
        "name": name,
        "status": "running",
        "depends_on": depends_on or [],
        "submitted_at": time.time(),
        "engine": engine,
    }

    result = _run_python_script(wrapped_script, work_dir=work_dir, timeout=timeout)

    if result["exit_code"] == 0 and "__TASK_SUCCESS__" in result["stdout"]:
        _tasks[task_id]["status"] = "completed"
        _tasks[task_id]["exit_code"] = 0
        _tasks[task_id]["stdout"] = result["stdout"].replace("__TASK_SUCCESS__", "").strip()
        _tasks[task_id]["stderr"] = result["stderr"]
    else:
        _tasks[task_id]["status"] = "failed"
        _tasks[task_id]["exit_code"] = result["exit_code"]
        _tasks[task_id]["stdout"] = result["stdout"]
        _tasks[task_id]["stderr"] = result["stderr"]
    _tasks[task_id]["completed_at"] = time.time()

    return {
        "task_id": task_id,
        "name": name,
        "status": _tasks[task_id]["status"],
        "exit_code": result["exit_code"],
        "stdout": result["stdout"][:3000],
        "stderr": result["stderr"][:3000],
        "engine": engine,
    }


# __ MCP Tools _________________________________________________________________

@mcp.tool()
def submit_task(
    name: str,
    python_code: str,
    depends_on: list[str] | None = None,
    timeout: int = 1800,
) -> str:
    """Submit a Python task for execution via the ADIOS2 workflow engine.

    The task runs locally using the configured Python interpreter.
    When ADIOS2 is available, the adios2 module is pre-imported for
    high-performance I/O. Otherwise falls back to numpy file I/O.
    If depends_on is specified, the task waits for those tasks to complete first.

    Args:
        name: Descriptive name for this task (e.g. "run_simulation", "analyze_data")
        python_code: Python code to execute (multi-line string, self-contained)
        depends_on: List of task IDs that must complete before this task runs (optional)
        timeout: Max seconds to wait for execution (default: 1800)

    Returns:
        JSON with task_id, status, and execution results
    """
    task_id = f"task_{uuid.uuid4().hex[:8]}"

    # Check dependencies
    if depends_on:
        for dep_id in depends_on:
            if dep_id not in _tasks:
                return json.dumps({
                    "task_id": task_id,
                    "status": "failed",
                    "error": f"Dependency {dep_id} not found",
                })
            if _tasks[dep_id]["status"] != "completed":
                return json.dumps({
                    "task_id": task_id,
                    "status": "failed",
                    "error": f"Dependency {dep_id} has status '{_tasks[dep_id]['status']}', not 'completed'",
                })

    # Resolve /app/ path aliases in user code so os.chdir("/app/work/run0") etc. work
    resolved_code = _resolve_paths(python_code)

    # classify on the code as submitted, before path resolution and wrapping,
    # so the fallback/unused verdict is independent of the wrapper
    engine = _adios_engine_state(python_code)

    wrapped_script = _wrap_as_adios_task(resolved_code)
    response = _run_and_finalize(task_id, name, wrapped_script, timeout, engine,
                                  depends_on=depends_on)
    return json.dumps(response, indent=2)


@mcp.tool()
def submit_shell_task(
    name: str,
    command: str,
    work_dir: str = "",
    timeout: int = 1800,
) -> str:
    """Submit a shell command for execution.

    Args:
        name: Descriptive name for this task
        command: Shell command to execute
        work_dir: Working directory (default: repo work/run0)
        timeout: Max seconds to wait
    """
    task_id = f"task_{uuid.uuid4().hex[:8]}"
    _work = _resolve_paths(work_dir) if work_dir else DEFAULT_WORK_DIR

    _tasks[task_id] = {
        "name": name,
        "status": "running",
        "depends_on": [],
        "submitted_at": time.time(),
    }

    resolved_cmd = _resolve_paths(command)
    result = _run_command(["bash", "-c", resolved_cmd], work_dir=_work, timeout=timeout)

    _tasks[task_id]["status"] = "completed" if result["exit_code"] == 0 else "failed"
    _tasks[task_id]["exit_code"] = result["exit_code"]
    _tasks[task_id]["stdout"] = result["stdout"]
    _tasks[task_id]["stderr"] = result["stderr"]
    _tasks[task_id]["completed_at"] = time.time()

    return json.dumps({
        "task_id": task_id,
        "name": name,
        "status": _tasks[task_id]["status"],
        "exit_code": result["exit_code"],
        "stdout": result["stdout"][:3000],
        "stderr": result["stderr"][:3000],
    }, indent=2)


@mcp.tool()
def write_bp(name: str, bp_path: str, python_code: str, timeout: int = 1800) -> str:
    """Write data to a BP file using a real, server-opened adios2.Stream.

    python_code runs with `stream` already bound to an open
    adios2.Stream(bp_path, "w") -- call stream.write(var_name, data, shape, start, count)
    and stream.write_attribute(...) on it directly. Do NOT open your own Stream or
    import adios2 yourself; the server already did both -- writing your own
    `with adios2.Stream(...)` here would open the same file twice.

    Requires ADIOS2 to be installed. Check the "engine" field in the response:
    "adios2-fallback" means ADIOS2 isn't available here -- use submit_task with
    plain numpy file I/O instead. "adios2-unused" means ADIOS2 was available but
    your code never called stream.write(...) -- the file won't have real data in it.

    Args:
        name: Descriptive name for this task
        bp_path: Output .bp path (supports /app/ paths)
        python_code: Code that calls stream.write(...)/stream.write_attribute(...)
        timeout: Max seconds to wait (default: 1800)
    """
    task_id = f"task_{uuid.uuid4().hex[:8]}"

    if not _check_adios2():
        return json.dumps({
            "task_id": task_id, "status": "rejected", "engine": "adios2-fallback",
            "error": "ADIOS2 is not available in this environment -- use submit_task "
                     "with plain numpy file I/O instead.",
        }, indent=2)

    resolved_bp = _resolve_paths(bp_path)
    resolved_code = _resolve_paths(python_code)
    engine = _adios_engine_state(python_code, _STREAM_USAGE_MARKERS, require_intent=False)

    wrapped_script = _wrap_as_bp_stream_task(resolved_code, resolved_bp, "w")
    response = _run_and_finalize(task_id, name, wrapped_script, timeout, engine)
    response["bp_path"] = resolved_bp
    return json.dumps(response, indent=2)


@mcp.tool()
def read_bp(name: str, bp_path: str, python_code: str, timeout: int = 1800) -> str:
    """Read data from a BP file using a real, server-opened adios2.Stream.

    python_code runs with `stream` already bound to an open
    adios2.Stream(bp_path, "r") -- iterate `for _ in stream.steps():` and call
    stream.read(var_name)/stream.read_attribute(...) on it directly. Do NOT open
    your own Stream or import adios2 yourself; the server already did both.

    Requires ADIOS2 to be installed. Check the "engine" field in the response:
    "adios2-fallback" means ADIOS2 isn't available here. "adios2-unused" means
    your code never called stream.read(...) -- you read nothing from the file.

    Args:
        name: Descriptive name for this task
        bp_path: Input .bp path to read (supports /app/ paths)
        python_code: Code that calls stream.read(...)/stream.read_attribute(...)
        timeout: Max seconds to wait (default: 1800)
    """
    task_id = f"task_{uuid.uuid4().hex[:8]}"

    if not _check_adios2():
        return json.dumps({
            "task_id": task_id, "status": "rejected", "engine": "adios2-fallback",
            "error": "ADIOS2 is not available in this environment -- use submit_task "
                     "with plain numpy file I/O instead.",
        }, indent=2)

    resolved_bp = _resolve_paths(bp_path)
    resolved_code = _resolve_paths(python_code)
    engine = _adios_engine_state(python_code, _STREAM_USAGE_MARKERS, require_intent=False)

    wrapped_script = _wrap_as_bp_stream_task(resolved_code, resolved_bp, "r")
    response = _run_and_finalize(task_id, name, wrapped_script, timeout, engine)
    response["bp_path"] = resolved_bp
    return json.dumps(response, indent=2)


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
def submit_mpi_task(
    name: str,
    command: str,
    num_ranks: int = 0,
    work_dir: str = "",
    timeout: int = 1800,
) -> str:
    """Submit a command to run in parallel under MPI (mpirun/mpiexec).

    Prepends the detected MPI launcher to the given command.

    Args:
        name:      Descriptive name for this task
        command:   The executable and its arguments (without the launcher prefix)
        num_ranks: Number of MPI ranks. 0 (default) uses all available ranks
                   from PBS_NP, or 1 on a local machine.
        work_dir:  Working directory (default: repo work/run0)
        timeout:   Max seconds to wait (default: 1800)

    Returns:
        JSON with task_id, status, exit_code, stdout, stderr
    """
    task_id = f"task_{uuid.uuid4().hex[:8]}"
    _work = _resolve_paths(work_dir) if work_dir else DEFAULT_WORK_DIR

    resources = _detect_resources()
    ranks = num_ranks if num_ranks > 0 else resources["ntasks"]
    launcher = resources["launcher"]

    if launcher == "mpirun":
        full_cmd = f"mpirun -np {ranks} {command}"
    elif launcher == "mpiexec":
        full_cmd = f"mpiexec -n {ranks} {command}"
    else:
        full_cmd = command

    _tasks[task_id] = {
        "name": name,
        "status": "running",
        "depends_on": [],
        "submitted_at": time.time(),
        "mpi_ranks": ranks,
        "launcher": launcher,
    }

    resolved_cmd = _resolve_paths(full_cmd)
    result = _run_command(["bash", "-c", resolved_cmd], work_dir=_work, timeout=timeout)

    _tasks[task_id]["status"] = "completed" if result["exit_code"] == 0 else "failed"
    _tasks[task_id]["exit_code"] = result["exit_code"]
    _tasks[task_id]["stdout"] = result["stdout"]
    _tasks[task_id]["stderr"] = result["stderr"]
    _tasks[task_id]["completed_at"] = time.time()

    return json.dumps({
        "task_id": task_id,
        "name": name,
        "status": _tasks[task_id]["status"],
        "launcher": launcher,
        "ranks": ranks,
        "command": full_cmd,
        "exit_code": result["exit_code"],
        "stdout": result["stdout"][:3000],
        "stderr": result["stderr"][:3000],
    }, indent=2)


@mcp.tool()
def run_lammps(
    script: str = "in.watbox",
    work_dir: str = "",
    timeout: int = 7200,
) -> str:
    """Run a LAMMPS simulation. Automatically selects the right execution method:
    - Inside a PBS job with mpirun available: mpirun -np PBS_NP lmp -in <script>
    - Otherwise (local / no MPI launcher): Python API (single process)

    Args:
        script: Input script filename relative to work_dir (default: in.watbox)
        work_dir: Working directory containing the script and data files.
                  Supports /app/ paths (default: repo work/run0)
        timeout: Max seconds to wait (default: 7200)

    Returns:
        JSON with task_id, status, method (mpi|python_api), ranks, stdout, stderr
    """
    import glob as _glob
    task_id = f"task_{uuid.uuid4().hex[:8]}"
    _work = _resolve_paths(work_dir) if work_dir else DEFAULT_WORK_DIR

    # Clear previous trajectory frames so stale output can't be mistaken for a new run.
    frames_dir = os.path.join(_work, "frames")
    if os.path.isdir(frames_dir):
        for _old in _glob.glob(os.path.join(frames_dir, "*.lammpstrj")):
            os.remove(_old)

    res = _detect_resources()
    use_mpi = res["in_pbs"] and bool(res["launcher"])
    method = "mpi" if use_mpi else "python_api"
    ranks = res["ntasks"] if use_mpi else 1

    _tasks[task_id] = {
        "name": f"run_lammps:{script}",
        "status": "running",
        "depends_on": [],
        "submitted_at": time.time(),
        "method": method,
    }

    if use_mpi:
        # the pip lammps wheel's bundled lmp binary won't load on this cluster's kernel
        # (same elf segment-layout mismatch as parsl_server.py) no matter the MPI
        # runtime, so use the cluster module (gcc 13.2.0 + OpenMPI 5.0.6) instead
        cmd = (
            f"module load lammps/22Jul2025 >/dev/null 2>&1 && "
            f"cd {_work} && "
            f"env -u PMI_SIZE -u PMI_RANK -u I_MPI_HYDRA_BOOTSTRAP "
            f"mpirun -n {ranks} lmp -in {script}"
        )
        result = _run_command(["bash", "-lc", cmd], work_dir=_work, timeout=timeout)
    else:
        py_script = f"""\
import os
os.chdir("{_work}")
from lammps import lammps
lmp = lammps(cmdargs=["-screen", "none"])
lmp.file("{script}")
lmp.close()
"""
        result = _run_python_script(py_script, work_dir=_work, timeout=timeout)

    exit_code = result["exit_code"]
    frames_written = _glob.glob(os.path.join(frames_dir, "*.lammpstrj")) if os.path.isdir(frames_dir) else []
    # exit 11 = SIGSEGV on lmp cleanup, but output was already written before it crashed
    if exit_code == 11 and frames_written:
        status = "completed"
        note = f"lmp exited 11 (SIGSEGV cleanup crash) but {len(frames_written)} trajectory frames were written — treating as success"
    else:
        status = "completed" if exit_code == 0 else "failed"
        note = ""

    _tasks[task_id]["status"] = status
    _tasks[task_id]["exit_code"] = exit_code
    _tasks[task_id]["stdout"] = result["stdout"]
    _tasks[task_id]["stderr"] = result["stderr"]
    _tasks[task_id]["completed_at"] = time.time()

    return json.dumps({
        "task_id":   task_id,
        "name":      f"run_lammps:{script}",
        "status":    status,
        "method":    method,
        "ranks":     ranks,
        "exit_code": exit_code,
        "frames":    len(frames_written),
        "note":      note,
        "stdout":    result["stdout"][:3000],
        "stderr":    result["stderr"][:3000],
    }, indent=2)


@mcp.tool()
def get_task_status(task_id: str) -> str:
    """Get the current status of a submitted task."""
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
    """Get the full output (stdout/stderr) of a completed task."""
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
    """Install a pip package using the configured Python interpreter."""
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
    """Check if a Python package is installed."""
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
    """List files in a directory."""
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
    """Read the contents of a file."""
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
def cleanup() -> str:
    """Clean up resources. Clears the task registry."""
    global _tasks
    count = len(_tasks)
    _tasks = {}
    return json.dumps({"status": "cleaned up", "tasks_cleared": count})


# __ Helpers ___________________________________________________________________

def _indent(text: str, spaces: int) -> str:
    """Indent every line of text by the given number of spaces."""
    prefix = " " * spaces
    return "\n".join(prefix + line for line in text.splitlines())


def _resolve_paths(text: str) -> str:
    """Replace /app/ placeholder paths with actual local repo paths."""
    return text.replace("/app/", REPO_ROOT + "/").replace("//", "/")


# __ Main ______________________________________________________________________

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="ADIOS2 Workflow MCP Server")
    parser.add_argument("--transport", choices=["stdio", "sse"], default="stdio")
    args = parser.parse_args()

    if args.transport == "sse":
        mcp.run(transport="sse")
    else:
        mcp.run(transport="stdio")
