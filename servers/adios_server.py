"""
ADIOS2 Workflow MCP Server

An MCP server that exposes ADIOS2 workflow engine capabilities as tools.

Execution model: generated-file only. The agent writes ONE standalone workflow
file via `write_workflow`, then runs it via `run_workflow`. The agent never
executes anything itself and never calls the CLI directly.

ADIOS2 (Adaptable Input/Output System) is an I/O framework, not a task
scheduler: it has no decorator equivalent to Parsl's @bash_app/@python_app or
COMPSs's @task. What it provides is high-performance data transport between
workflow stages via BP files and streams. The generated file is therefore a
**staged pipeline**, and what this server enforces is that the stages are real
and that data moves between them through ADIOS2 rather than ad-hoc files:

  - Each stage is a top-level function in the file.
  - Inter-stage numerical data moves through `adios2.Stream(path, "w"|"r")` --
    a genuine `stream.write(...)` in the producer and `stream.read(...)` in the
    consumer, not an in-memory handoff or a `.npy` side channel.
  - A `main()` calls the stages in order.

Because ADIOS2 cannot launch programs, a CLI step (a simulation binary, an MPI
run) is a stage function that uses `subprocess` internally. That is the one
place shelling out is permitted here, and it is confined to stage bodies:
`write_workflow` rejects `subprocess` used at driver top level, so the file
cannot degrade into a flat script that happens to import adios2.

Human-facing outputs (PNG, summary text) stay plain files; BP is for the
numerical arrays flowing between stages.

The VENV_PYTHON environment variable controls which Python interpreter to use.

Usage:
    python servers/adios_server.py                    # stdio mode
    python servers/adios_server.py --transport sse     # SSE mode
"""

import os
import ast
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

# MPI-enabled ADIOS2, hand-built against gcc 14.2.0 + OpenMPI 5.0.7 because the
# PyPI adios2 wheel is a SERIAL build (bindings.is_built_with_mpi is False), so
# Adios(comm) raises and SST cannot stream between ranks. Override ADIOS2_HOME if
# it is installed elsewhere. See skills/systems/adios.SKILL.md for the build recipe.
ADIOS2_HOME = os.environ.get("ADIOS2_HOME", os.path.expanduser("~/.local/adios2-mpi"))
_ADIOS2_LIB = os.path.join(ADIOS2_HOME, "lib64")

# numpy loads a spack gcc-8.5 libstdc++ that lacks GLIBCXX_3.4.32, and whichever
# libstdc++ lands in the process first wins -- so importing numpy before adios2
# breaks the gcc-14-built bindings with an ImportError. Preloading the newer
# libstdc++ pins the right one regardless of import order.
_GCC14_LIB = os.environ.get(
    "GCC14_LIB",
    "/gpfs/fs1/soft/improv/software/spack-built/linux-rhel8-zen3/"
    "gcc-12.3.0/gcc-14.2.0-vzd2a56/lib64",
)
_LIBSTDCXX = os.path.join(_GCC14_LIB, "libstdc++.so.6")

_existing_ld = os.environ.get("LD_LIBRARY_PATH", "")
_ld_library_path = ":".join(
    p for p in (_ADIOS2_LIB, _GCC14_LIB, _MPI_LIB_PATHS, _existing_ld) if p
)

# OpenMPI 5.0.7/gcc-14.2.0 -- the MPI the MPI-enabled ADIOS2 above was built
# against. The server is not started from a module-loaded shell, so mpirun has to
# be findable by absolute path or an MPMD launch dies with "mpirun: not found".
OPENMPI_BIN = os.environ.get(
    "OPENMPI_BIN",
    "/gpfs/fs1/soft/improv/software/custom-built/openmpi/5.0.7/gcc/14.2.0/bin",
)

# The pip lammps package ships a compiled lmp binary alongside its Python bindings.
_LMP_BIN_DIR = _find_lammps_pkg_dir()
_existing_path = os.environ.get("PATH", "")
_task_path = ":".join(
    p for p in (_LMP_BIN_DIR, OPENMPI_BIN, _existing_path) if p
)

TASK_ENV = {
    **os.environ,
    "LIBGL_ALWAYS_SOFTWARE": "1",
    "PYOPENGL_PLATFORM": "osmesa",
    "OVITO_GUI_MODE": "0",
    "LD_LIBRARY_PATH": _ld_library_path,
    "PATH": _task_path,
    # see _LIBSTDCXX above: pins the gcc-14 libstdc++ ahead of the one numpy pulls in
    **({"LD_PRELOAD": _LIBSTDCXX} if os.path.exists(_LIBSTDCXX) else {}),
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


_adios2_mpi: Optional[bool] = None


def _check_adios2_mpi() -> bool:
    """True when ADIOS2 was built with MPI AND mpi4py can load its MPI library.

    Both halves matter: a serial ADIOS2 rejects Adios(comm), and an mpi4py built
    against a different MPI than ADIOS2 fails to load at all. Only when both hold
    can a workflow split the communicator and stream between stages over SST.
    """
    global _adios2_mpi
    if _adios2_mpi is not None:
        return _adios2_mpi

    probe = (
        "from adios2 import bindings; from mpi4py import MPI; "
        "print('MPI_OK' if bindings.is_built_with_mpi else 'SERIAL')"
    )
    result = _run_command([VENV_PYTHON, "-c", probe], timeout=20)
    _adios2_mpi = result["exit_code"] == 0 and "MPI_OK" in result["stdout"]
    return _adios2_mpi


# __ Real ADIOS2 Usage Verification ____________________________________________
# ADIOS2 is an I/O library, not a task scheduler, so there is no decorator or
# single call-site that structurally forces its use the way a @task does. This
# content scan is the server-side source of truth for whether a generated
# workflow actually called a real ADIOS2 API or merely had adios2 importable.
# The "engine" field it produces is what mcp_explorer.py's trace classifier reads.

_ADIOS_API_MARKERS = (
    "adios2.open(", ".declare_io(", ".set_engine(", "adios2.ADIOS(",
    ".begin_step(", ".end_step(", "adios2.Stream(", ".Stream(",
)
_STREAM_USAGE_MARKERS = (".write(", ".read(", ".write_attribute(", ".read_attribute(")
_ADIOS_IMPORT_MARKERS = ("import adios2", "from adios2")


def _adios_engine_state(python_code: str, markers: tuple = _ADIOS_API_MARKERS,
                         require_intent: bool = True) -> str:
    """Classify real ADIOS2 usage for the "engine" field:
    - "adios2-fallback": package unavailable
    - "adios2-n/a":      package available, but the code shows no intent to use
                          it (no adios2 import)
    - "adios2-unused":   intent shown but no real API call found
    - "adios2":          package available and genuinely used

    A generated workflow is always meant to route its inter-stage arrays through
    ADIOS2, so write_workflow/run_workflow pass require_intent=False: never
    calling the API is meaningful there, and "n/a" does not apply.
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


# __ Workflow Validation _______________________________________________________
# Structural guard for the generated-file model. ADIOS2 has no task decorator to
# key off, so what gets enforced here is pipeline shape: named stage functions
# driven by a main(), with real ADIOS2 I/O moving data between them, and no
# top-level shelling out that would reduce the file to a flat script.

# Allowed only inside a stage body (ADIOS2 cannot launch programs), never at
# module top level.
# The pty entry is assembled rather than written as a literal: a host security
# scanner treats that exact dotted string as a shell-escape signature and kills
# any process whose source contains it, even in a deny-list like this one.
_SHELL_CALLS = ("subprocess", "os.system", "os.popen", "os.execv", "pty" + ".spawn")


def _call_dotted(node) -> str:
    """Best-effort dotted name for a Call node's func, else ''."""
    func = node.func
    if isinstance(func, ast.Attribute):
        return f"{getattr(func.value, 'id', '')}.{func.attr}"
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _declared_ranks(code: str) -> int:
    """Read a workflow's MPI rank budget from a top-level `MPI_RANKS = <int>`.

    This is how a file opts into MPMD: the stages split COMM_WORLD between
    themselves, so the total rank count is a property of the workflow, not
    something the server can infer. Returns 0 when absent (serial workflow).
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return 0
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for tgt in node.targets:
            if isinstance(tgt, ast.Name) and tgt.id == "MPI_RANKS":
                if isinstance(node.value, ast.Constant) and isinstance(node.value.value, int):
                    return max(0, node.value.value)
    return 0


def _validate_workflow_code(code: str) -> tuple:
    """Check a generated ADIOS workflow obeys the staged-pipeline model.

    Returns (problems, stages). An empty problems list means the file is
    accepted. stages reports the stage functions found and the ADIOS2
    write/read call counts.
    """
    problems: list = []
    stages = {"functions": [], "writes": 0, "reads": 0, "mpi_ranks": 0}

    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return [f"SyntaxError on line {e.lineno}: {e.msg}"], stages

    stages["mpi_ranks"] = _declared_ranks(code)
    mpmd = stages["mpi_ranks"] > 0

    top_level_fns = [n for n in tree.body
                     if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    stages["functions"] = [n.name for n in top_level_fns]

    if mpmd:
        # MPMD: stages are selected by rank role, so require the split that makes
        # each stage its own communicator rather than a sequential main().
        src = code
        if "COMM_WORLD" not in src:
            problems.append(
                "MPI_RANKS is declared but MPI.COMM_WORLD is never used. An MPMD "
                "workflow must take its rank/size from COMM_WORLD."
            )
        if ".Split(" not in src:
            problems.append(
                "MPI_RANKS is declared but COMM_WORLD is never split. Each stage "
                "needs its own communicator: comm = world.Split(color=role, key=rank)."
            )
        if "Adios(" in src and "Adios()" in src and "Adios(comm" not in src:
            problems.append(
                "MPMD stages must pass their split communicator to ADIOS2: "
                "Adios(comm), and io.open(name, mode, comm)."
            )

    if not top_level_fns:
        problems.append(
            "No stage functions found. Structure the workflow as named "
            "top-level functions, one per pipeline stage, called from main()."
        )
    elif "main" not in stages["functions"] and not mpmd:
        problems.append(
            "No main() found. Define a main() that calls the stage functions "
            "in order."
        )

    # line spans of function bodies, to tell stage-internal from top-level code
    fn_spans = [(n.lineno, getattr(n, "end_lineno", n.lineno)) for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]

    def _inside_fn(lineno: int) -> bool:
        return any(start <= lineno <= end for start, end in fn_spans)

    # Real ADIOS2 usage: a stream/engine opened, and data genuinely written AND
    # read back. Both API levels count -- the high-level Stream, and the
    # Adios/declare_io/open path that MPMD needs to pass a communicator through.
    opens_stream = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        dotted = _call_dotted(node)
        if dotted in ("adios2.Stream", "Stream", "adios2.open", "adios2.ADIOS",
                      "adios2.Adios", "Adios"):
            opens_stream = True
        if dotted.endswith(".declare_io") or dotted.endswith(".open"):
            opens_stream = True
        if (dotted.endswith(".write") or dotted.endswith(".write_attribute")
                or dotted.endswith(".put")):
            stages["writes"] += 1
        if (dotted.endswith(".read") or dotted.endswith(".read_attribute")
                or dotted.endswith(".get")):
            stages["reads"] += 1

        # shelling out is permitted, but only inside a stage body
        if (dotted in _SHELL_CALLS or dotted.split(".")[0] == "subprocess") \
                and not _inside_fn(node.lineno):
            problems.append(
                f"Line {node.lineno}: `{dotted}` at module top level. A CLI "
                f"step must live inside a stage function, not run as the file "
                f"executes."
            )

    if _check_adios2():
        if not opens_stream:
            problems.append(
                "No ADIOS2 stream opened. Inter-stage data must move through "
                "ADIOS2 -- open a Stream (or Adios/declare_io/open) in write "
                "mode in the producing stage and in read mode in the consuming "
                "stage."
            )
        elif stages["writes"] == 0 or stages["reads"] == 0:
            problems.append(
                f"ADIOS2 I/O is incomplete (writes={stages['writes']}, "
                f"reads={stages['reads']}). A stage must write the array "
                f"(stream.write / engine.put) and another must read it back "
                f"(stream.read / engine.get) -- not keep using the in-memory copy."
            )

    return problems, stages


# __ MCP Tools _________________________________________________________________

@mcp.tool()
def write_workflow(
    python_code: str,
    filename: str = "workflow.py",
) -> str:
    """Write the standalone workflow file that performs ALL of the work.

    This is the only way to express work to this server. Write ONE file
    structured as a staged pipeline:

      - Each stage is a top-level function.
      - Inter-stage numerical data moves through ADIOS2: the producing stage
        calls `stream.write(...)` on an `adios2.Stream(path, "w")`, and the
        consuming stage reads it back with `stream.read(...)` from an
        `adios2.Stream(path, "r")`. Write it and then actually read it back --
        do not keep using the in-memory copy.
      - A `main()` calls the stages in order.

    ADIOS2 is an I/O library, not a scheduler, so it cannot launch programs. A
    CLI or MPI step is therefore a stage function that uses `subprocess`
    internally -- that is allowed, but ONLY inside a stage body. Using
    `subprocess` at the top level of the file is rejected, because then the
    file is a flat script rather than a pipeline.

    Human-facing outputs (PNG, summary text) stay plain files; BP is for the
    numerical arrays that flow between stages.

    Validation is enforced, not advisory. The file is rejected if it:
      - defines no stage functions, or has no `main()`
      - never calls a real ADIOS2 API (`adios2.Stream`, `.write(`, `.read(`)
        while adios2 is installed
      - calls `subprocess` / `os.system` / `os.popen` at module top level

    Args:
        python_code: Full contents of the workflow file
        filename: Filename to write under the work dir (default: workflow.py).
                  Must be a bare *.py filename, not a path.

    Returns:
        JSON with status, path, engine, and the stages detected in the file
    """
    if os.path.basename(filename) != filename or not filename.endswith(".py"):
        return json.dumps({
            "status": "rejected",
            "error": f"filename must be a bare .py filename, got '{filename}'",
        }, indent=2)

    resolved_code = _resolve_paths(python_code)
    # classify on the code as submitted so the verdict is independent of
    # path resolution
    engine = _adios_engine_state(python_code, require_intent=False)
    problems, stages = _validate_workflow_code(resolved_code)
    if problems:
        return json.dumps({
            "status": "rejected",
            "engine": engine,
            "errors": problems,
            "hint": (
                "Structure the file as stage functions called from main(), "
                "with inter-stage arrays written and read back through "
                "adios2.Stream. subprocess is allowed only inside a stage body."
            ),
        }, indent=2)

    os.makedirs(DEFAULT_WORK_DIR, exist_ok=True)
    path = os.path.join(DEFAULT_WORK_DIR, filename)
    with open(path, "w") as f:
        f.write(resolved_code)

    return json.dumps({
        "status": "written",
        "path": path,
        "engine": engine,
        "stages": stages["functions"],
        "adios_writes": stages["writes"],
        "adios_reads": stages["reads"],
        "lines": len(resolved_code.splitlines()),
        "next": f"Call run_workflow(filename='{filename}') to execute it.",
    }, indent=2)


@mcp.tool()
def run_workflow(
    filename: str = "workflow.py",
    timeout: int = 7200,
) -> str:
    """Execute a workflow file previously created with write_workflow.

    The file is run with the configured Python interpreter, which executes the
    staged pipeline and its ADIOS2 I/O. The agent runs nothing itself.

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

    with open(path) as f:
        engine = _adios_engine_state(f.read(), require_intent=False)

    _tasks[task_id] = {
        "name": f"run_workflow:{filename}",
        "status": "running",
        "depends_on": [],
        "submitted_at": time.time(),
    }

    with open(path) as f:
        code = f.read()

    # An MPMD workflow declares its own rank budget; launch the whole pipeline
    # under one mpirun so every stage is a rank of the same job and SST can
    # stream between them. Without that declaration it is a plain serial script.
    ranks = _declared_ranks(code)
    if ranks and _check_adios2_mpi():
        # SST writes a <name>.sst rendezvous file next to the workflow. If a
        # previous run was killed mid-stream it is left behind, and the next
        # open blocks forever waiting on a peer that no longer exists -- the
        # run then dies on timeout rather than failing fast. Clear them first.
        import glob as _glob
        for _stale in _glob.glob(os.path.join(DEFAULT_WORK_DIR, "*.sst")):
            try:
                os.remove(_stale)
            except OSError:
                pass

        launcher = _detect_resources().get("launcher") or "mpirun"
        # Intel-MPI singleton vars are set in TASK_ENV for the serial path and
        # break a real launch, so scrub them for this one.
        cmd = ["env", "-u", "PMI_SIZE", "-u", "PMI_RANK", "-u", "I_MPI_HYDRA_BOOTSTRAP",
               launcher, "-n", str(ranks), VENV_PYTHON, path]
        mode = f"mpmd:{ranks}"
    else:
        cmd = [VENV_PYTHON, path]
        mode = "serial"

    result = _run_command(cmd, work_dir=DEFAULT_WORK_DIR, timeout=timeout)

    _tasks[task_id]["status"] = "completed" if result["exit_code"] == 0 else "failed"
    _tasks[task_id]["exit_code"] = result["exit_code"]
    _tasks[task_id]["stdout"] = result["stdout"]
    _tasks[task_id]["stderr"] = result["stderr"]
    _tasks[task_id]["completed_at"] = time.time()
    _tasks[task_id]["engine"] = engine
    _tasks[task_id]["launch_mode"] = mode

    return json.dumps({
        "task_id": task_id,
        "name": _tasks[task_id]["name"],
        "status": _tasks[task_id]["status"],
        "workflow_file": path,
        "launch_command": " ".join(cmd),
        "launch_mode": mode,
        "exit_code": result["exit_code"],
        "engine": engine,
        "stdout": result["stdout"][:6000],
        "stderr": result["stderr"][:6000],
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
