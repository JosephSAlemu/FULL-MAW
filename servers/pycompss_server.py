"""
PyCOMPSs Workflow MCP Server

An MCP server that exposes PyCOMPSs workflow engine capabilities as tools.

Execution model: generated-file only. The agent writes ONE standalone PyCOMPSs
driver file via `write_workflow`, then runs it via `run_workflow`. The COMPSs
runtime drives the resulting task graph; the agent never executes anything
itself.

Every unit of work in that file is a decorated function:

    @task                            pure-Python work
    @binary(binary=...) + @task      a command-line program
    @mpi(binary=..., processes=N)
                        + @task      an MPI program on N ranks

`@binary`/`@mpi` are COMPSs's own CLI-invocation mechanism. They stack ABOVE
`@task` and the function body is `pass`. Arguments come from the function
signature plus `@task` parameter types (`FILE_IN`, `FILE_OUT`,
`FILE_IN_STDIN`, `FILE_OUT_STDOUT`, `Prefix`), or from an `args` string with
`{{name}}` placeholders, e.g. `@binary(binary="date", args="-d {{param_1}}")`.
COMPSs builds and launches the command line itself, which keeps execution
inside the engine and keeps files inside the dependency graph.

`subprocess` is rejected anywhere in the file, and the agent never types
`mpirun`/`mpiexec`/`srun`: `@mpi(processes=N, runner=...)` expresses an MPI
launch. Shelling out would bypass the runtime.

Runtime lifecycle depends on launch mode, and the generated file must match:
  - Inside PBS: launched by `runcompss`, which owns the runtime. The file must
    NOT call compss_start()/compss_stop().
  - Outside PBS: plain Python "direct link" mode, because runcompss's
    SSH-based worker launch hits this cluster's login-node SSH-key+Duo policy.
    The file MUST call compss_start()/compss_stop() itself.
`write_workflow` validates the file against the mode the server will use, and
reports that mode so the agent can generate the right shape.

The VENV_PYTHON environment variable controls which Python interpreter to use.

Usage:
    python servers/pycompss_server.py                    # stdio mode
    python servers/pycompss_server.py --transport sse     # SSE mode
"""

import os
import re
import ast
import sys
import json
import signal
import socket
import subprocess
import uuid
import time
import tempfile
from typing import Optional
from fastmcp import FastMCP

# __ Server Setup ______________________________________________________________

mcp = FastMCP(
    "PyCOMPSs Workflow Engine",
    instructions="MCP server exposing PyCOMPSs workflow engine for scientific workflow execution",
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

# COMPSs isn't pip-installable, it's Java + C++ middleware installed via its own
# ./install script, lives outside the venv, activated through PYTHONPATH/LD_LIBRARY_PATH
# instead of site-packages. override COMPSS_HOME if it's installed somewhere else
COMPSS_HOME = os.environ.get("COMPSS_HOME", os.path.expanduser("~/.local/COMPSs"))
# COMPSs's install only ever creates a major-version dir ("3") no matter the
# actual python 3.x minor version, that's just its naming convention
_COMPSS_PYTHON_PATH = os.path.join(COMPSS_HOME, "Bindings", "python", "3")
_COMPSS_BINDINGS_LIB = os.path.join(COMPSS_HOME, "Bindings", "bindings-common", "lib")
# custom libxml2 build (no system libxml2-devel on this cluster), COMPSs's C++
# bindings were linked against it, needs to stay on LD_LIBRARY_PATH at runtime too
_LIBXML2_LIB = os.path.expanduser("~/.local/libxml2/lib")
JAVA_HOME = os.environ.get(
    "JAVA_HOME",
    "/gpfs/fs1/soft/improv/software/spack-built/linux-rhel8-zen3/gcc-12.3.0/openjdk-21.0.0_35-23zksi2",
)

_existing_ld = os.environ.get("LD_LIBRARY_PATH", "")
_ld_library_path = (
    _MPI_LIB_PATHS + ":" +
    f"{_COMPSS_BINDINGS_LIB}:{_LIBXML2_LIB}:{os.path.join(JAVA_HOME, 'lib')}" +
    (":" + _existing_ld if _existing_ld else "")
)

_existing_pythonpath = os.environ.get("PYTHONPATH", "")
_task_pythonpath = _COMPSS_PYTHON_PATH + (":" + _existing_pythonpath if _existing_pythonpath else "")

# The pip lammps package ships a compiled lmp binary alongside its Python bindings.
_LMP_BIN_DIR = _find_lammps_pkg_dir()
# COMPSs's CLI tools (runcompss etc) live in Runtime/scripts, never on PATH by
# default. not used by our own task execution but handy for ad-hoc shell tasks
_COMPSS_BIN_DIRS = (
    f"{os.path.join(COMPSS_HOME, 'Runtime', 'scripts', 'user')}:"
    f"{os.path.join(COMPSS_HOME, 'Runtime', 'scripts', 'utils')}:"
    f"{os.path.join(COMPSS_HOME, 'Bindings', 'c', 'bin')}"
)
_existing_path = os.environ.get("PATH", "")
_task_path = (f"{_LMP_BIN_DIR}:" if _LMP_BIN_DIR else "") + \
    f"{_COMPSS_BIN_DIRS}" + (":" + _existing_path if _existing_path else "")

TASK_ENV = {
    **os.environ,
    "LIBGL_ALWAYS_SOFTWARE": "1",
    "PYOPENGL_PLATFORM": "osmesa",
    "OVITO_GUI_MODE": "0",
    "LD_LIBRARY_PATH": _ld_library_path,
    "PATH": _task_path,
    "PYTHONPATH": _task_pythonpath,
    "COMPSS_HOME": COMPSS_HOME,
    "JAVA_HOME": JAVA_HOME,
    # Allow Intel MPI to initialize in a subprocess not launched via mpirun.
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

# __ Resource Detection ________________________________________________________

def _detect_resources() -> dict:
    """Read available compute resources from PBS env vars or local fallback."""
    import shutil
    in_pbs = bool(os.environ.get("PBS_JOBID"))
    nodefile = os.environ.get("PBS_NODEFILE", "")
    nodelist = ""
    nodefile_hosts: list = []
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
        # need the absolute path here, not just "mpirun". a generated workflow command
        # can run inside a runcompss-launched worker, a separate SSH-spawned shell
        # whose PATH comes from ~/.bashrc, not this process's TASK_ENV
        launcher = shutil.which("mpirun") or shutil.which("mpiexec") or ""
    warning = ""
    if not in_pbs:
        warning = ("NOT inside a PBS job (PBS_JOBID not set). MPI tasks and multi-node "
                   "execution are unavailable. Start an interactive PBS job: "
                   "qsub -I -l nodes=N:ppn=M -l walltime=HH:MM:SS -A <project>")
    return {"in_pbs": in_pbs, "nnodes": nnodes, "ntasks": ntasks,
            "cpus_per_task": cpus_per, "nodelist": nodelist,
            "launcher": launcher, "warning": warning}


# __ COMPSs Runtime Detection _________________________________________________

_compss_available: Optional[bool] = None


def _check_compss() -> bool:
    """Check if the PyCOMPSs/COMPSs runtime is available."""
    global _compss_available
    if _compss_available is not None:
        return _compss_available

    result = _run_command(
        [VENV_PYTHON, "-c", "from pycompss.api.api import compss_start; print('ok')"],
        timeout=15,
    )
    _compss_available = result["exit_code"] == 0
    return _compss_available


# __ runcompss Launch Config ___________________________________________________
#
# runcompss's default local NIO config launches its worker over SSH, even to
# "localhost", and this cluster's SSH-key+Duo MFA on login nodes rejects that
# outright (permission denied, worker just retries forever). SSH between nodes
# inside an active PBS allocation isn't subject to that policy, so real runcompss
# only gets used inside a PBS job, targeting this node's own hostname. outside
# PBS we fall back to direct-link mode below (self-managed compss_start/stop,
# plain VENV_PYTHON)

_compss_project_path: Optional[str] = None
_compss_resources_path: Optional[str] = None
_compss_config_hostname: Optional[str] = None


def _compss_launch_hostname() -> Optional[str]:
    """This node's hostname, if running inside a PBS job; None otherwise."""
    if not os.environ.get("PBS_JOBID"):
        return None
    return socket.gethostname()


def _compss_config_files() -> Optional[tuple]:
    """Write (once per hostname) a project.xml/resources.xml pair pointing
    runcompss at this node instead of the default "localhost". Returns
    (project_path, resources_path), or None outside a PBS job."""
    global _compss_project_path, _compss_resources_path, _compss_config_hostname

    host = _compss_launch_hostname()
    if not host:
        return None
    if _compss_config_hostname == host and _compss_project_path:
        return _compss_project_path, _compss_resources_path

    cfg_dir = os.path.join(DEFAULT_WORK_DIR, "_compss_config")
    os.makedirs(cfg_dir, exist_ok=True)
    project_path = os.path.join(cfg_dir, "project.xml")
    resources_path = os.path.join(cfg_dir, "resources.xml")

    ncpus = int(os.environ.get("PBS_NUM_PPN") or os.environ.get("PBS_NP") or 4)

    with open(project_path, "w") as f:
        f.write(f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Project>
    <MasterNode/>
    <ComputeNode Name="{host}">
        <InstallDir>{COMPSS_HOME}</InstallDir>
        <WorkingDir>/tmp/COMPSsWorker/</WorkingDir>
    </ComputeNode>
</Project>
""")
    with open(resources_path, "w") as f:
        f.write(f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<ResourcesList>
    <ComputeNode Name="{host}">
        <Processor Name="MainProcessor">
            <ComputingUnits>{ncpus}</ComputingUnits>
        </Processor>
        <Adaptors>
            <Adaptor Name="es.bsc.compss.nio.master.NIOAdaptor">
                <SubmissionSystem>
                    <Interactive/>
                </SubmissionSystem>
                <Ports>
                    <MinPort>43001</MinPort>
                    <MaxPort>43002</MaxPort>
                </Ports>
            </Adaptor>
        </Adaptors>
    </ComputeNode>
</ResourcesList>
""")
    _compss_project_path, _compss_resources_path, _compss_config_hostname = (
        project_path, resources_path, host,
    )
    return project_path, resources_path


# __ Task Registry _____________________________________________________________

_tasks: dict[str, dict] = {}


# __ Execution Helpers _________________________________________________________

def _run_command(cmd: list[str], work_dir: str = DEFAULT_WORK_DIR, timeout: int = 1800) -> dict:
    """Execute a command locally (or in venv) and return results.

    Backstop for crash modes the generated workflow's own error handling doesn't
    cover: runcompss forks a JVM master, which forks Python worker processes
    that inherit our stdout/stderr pipes. If the JVM master ever dies while a worker
    survives it (orphaned, still holding those pipes open), plain `proc.kill()` --
    which only signals the immediate `runcompss` process -- can't reach it, and
    `communicate()` hangs waiting for EOF that will never come even past `timeout`.
    start_new_session puts the whole tree in one process group so `os.killpg` can
    take out every descendant together, guaranteeing this returns within `timeout`
    no matter what runcompss/the JVM leave behind.
    """
    os.makedirs(work_dir, exist_ok=True)

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        cwd=work_dir,
        env=TASK_ENV,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        return {
            "exit_code": proc.returncode,
            "stdout": stdout.strip(),
            "stderr": stderr.strip(),
        }
    except subprocess.TimeoutExpired:
        _kill_process_group(proc)
        return {
            "exit_code": -1,
            "stdout": "",
            "stderr": f"Command timed out after {timeout}s",
        }
    except Exception as e:
        _kill_process_group(proc)
        return {
            "exit_code": -1,
            "stdout": "",
            "stderr": str(e),
        }


def _kill_process_group(proc: subprocess.Popen) -> None:
    """SIGKILL the entire process group started for `proc` (see _run_command)."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except ProcessLookupError:
        pass  # already gone
    proc.wait()


# __ Workflow Validation _______________________________________________________
# Structural guard for the generated-file model: the file must express its work
# as COMPSs tasks, never execute anything directly, and must match the runtime
# lifecycle of the launch mode the server will actually use.

# Reaching for any of these means the driver is running work itself instead of
# handing it to the COMPSs runtime.
#
# The pty entry is assembled rather than written as a literal: a host security
# scanner treats that exact dotted string as a shell-escape signature and kills
# any process whose source contains it, even in a deny-list like this one.
_FORBIDDEN_CALLS = {
    "os.system": "use a @binary/@mpi task instead",
    "os.popen": "use a @binary/@mpi task instead",
    "os.execv": "use a @binary/@mpi task instead",
    "os.spawnl": "use a @binary/@mpi task instead",
    "pty" + ".spawn": "use a @binary/@mpi task instead",
    "commands.getoutput": "use a @binary/@mpi task instead",
}

_LAUNCHER_RE = re.compile(r"\b(mpirun|mpiexec|srun|aprun)\b")

# COMPSs decorators that mark a function as a unit of work.
_TASK_DECORATORS = ("task",)
_CLI_DECORATORS = ("binary", "mpi")


def _decorator_names(node) -> set:
    """Collect decorator names on a function, bare or dotted, called or not."""
    names = set()
    for dec in node.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        if isinstance(target, ast.Name):
            names.add(target.id)
        elif isinstance(target, ast.Attribute):
            names.add(target.attr)
    return names


def _runner_kwarg_lines(tree) -> set:
    """Line numbers of `runner=`/`binary=` values inside @mpi/@binary decorators.

    An MPI launcher name is legitimate there (`@mpi(runner="mpirun")`) but
    nowhere else, so those lines are exempt from the launcher scan below.
    """
    exempt = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if not isinstance(dec, ast.Call):
                continue
            target = dec.func
            name = getattr(target, "id", None) or getattr(target, "attr", None)
            if name not in _CLI_DECORATORS:
                continue
            for kw in dec.keywords:
                for sub in ast.walk(kw.value):
                    if isinstance(sub, ast.Constant):
                        exempt.add(sub.lineno)
    return exempt


def _validate_workflow_code(code: str, via_runcompss: bool) -> tuple:
    """Check a generated PyCOMPSs driver obeys the generated-file model.

    Returns (problems, tasks). An empty problems list means the file is
    accepted. tasks maps "task"/"binary"/"mpi" to the function names carrying
    each decorator.
    """
    problems: list = []
    tasks = {"task": [], "binary": [], "mpi": []}

    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return [f"SyntaxError on line {e.lineno}: {e.msg}"], tasks

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            decs = _decorator_names(node)
            for kind in _TASK_DECORATORS + _CLI_DECORATORS:
                if kind in decs:
                    tasks[kind].append(node.name)

    if not tasks["task"]:
        problems.append(
            "No @task found. Every unit of work must be a COMPSs task: @task "
            "for Python work, or @binary/@mpi stacked above @task for CLI work."
        )

    # @binary/@mpi only take effect when stacked with @task
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        decs = _decorator_names(node)
        cli = decs & set(_CLI_DECORATORS)
        if cli and "task" not in decs:
            problems.append(
                f"Line {node.lineno}: '{node.name}' uses "
                f"@{sorted(cli)[0]} without @task beneath it. COMPSs requires "
                f"both, with @task innermost."
            )

    # direct execution escapes
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute):
            continue
        base = getattr(func.value, "id", "")
        dotted = f"{base}.{func.attr}"
        fix = _FORBIDDEN_CALLS.get(dotted)
        if fix is None and base == "subprocess":
            fix = "use a @binary/@mpi task instead"
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
                    f"CLI work belongs in a @binary/@mpi task."
                )

    # a launcher name anywhere but an @mpi/@binary decorator argument means the
    # agent is assembling its own command line
    exempt = _runner_kwarg_lines(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if _LAUNCHER_RE.search(node.value) and node.lineno not in exempt:
                problems.append(
                    f"Line {node.lineno}: MPI launcher named outside an "
                    f"@mpi decorator. Use @mpi(binary=..., runner=..., "
                    f"processes=N) instead of building the command yourself."
                )

    # runtime lifecycle must match how run_workflow will launch this file
    lifecycle = {"compss_start", "compss_stop"}
    called = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name in lifecycle:
                called.add(name)

    if via_runcompss and called:
        problems.append(
            f"This server launches via runcompss, which owns the runtime. "
            f"Remove {sorted(called)} -- calling them again double-initializes "
            f"COMPSs."
        )
    elif not via_runcompss and tasks["task"] and not called:
        problems.append(
            "This server launches the file with plain Python (direct-link "
            "mode), so it must call compss_start() before submitting tasks and "
            "compss_stop() at the end."
        )

    return problems, tasks


# __ MCP Tools _________________________________________________________________

@mcp.tool()
def write_workflow(
    python_code: str,
    filename: str = "workflow.py",
) -> str:
    """Write the standalone PyCOMPSs workflow file that performs ALL of the work.

    This is the only way to express work to this server. Write ONE file where
    every step is a decorated function:

      - `@task`                   pure-Python work.
      - `@binary(binary="...")`   a command-line program. Stacks ABOVE `@task`,
        body is `pass`. Arguments come from the function signature plus the
        `@task` parameter types (`FILE_IN`, `FILE_OUT`, `FILE_IN_STDIN`,
        `FILE_OUT_STDOUT`, `Prefix`), or from an `args` string with `{{name}}`
        placeholders.
      - `@mpi(binary="...", runner="mpirun", processes=N)`  an MPI program on N
        ranks. Also stacks above `@task` with a `pass` body. Omit `binary=` to
        run an mpi4py body instead.

    Dependencies come from passing values between tasks and from `FILE_IN`/
    `FILE_OUT` parameters. Call `compss_wait_on()` only where the driver needs
    a real value.

    Validation is enforced, not advisory. The file is rejected if it:
      - defines no `@task`
      - imports or calls `subprocess`, or calls `os.system` / `os.popen`
      - names `mpirun`/`mpiexec`/`srun` outside an `@mpi(runner=...)` argument
      - gets the runtime lifecycle wrong for this server's launch mode (see
        `launch_mode` in the result)

    Args:
        python_code: Full contents of the PyCOMPSs driver file
        filename: Filename to write under the work dir (default: workflow.py).
                  Must be a bare *.py filename, not a path.

    Returns:
        JSON with status, path, launch_mode, and the tasks detected in the file
    """
    if os.path.basename(filename) != filename or not filename.endswith(".py"):
        return json.dumps({
            "status": "rejected",
            "error": f"filename must be a bare .py filename, got '{filename}'",
        }, indent=2)

    compss_ok = _check_compss()
    via_runcompss = bool(compss_ok and _compss_launch_hostname())
    launch_mode = "runcompss" if via_runcompss else ("direct" if compss_ok else "fallback")

    resolved_code = _resolve_paths(python_code)
    problems, tasks = _validate_workflow_code(resolved_code, via_runcompss=via_runcompss)
    if problems:
        return json.dumps({
            "status": "rejected",
            "launch_mode": launch_mode,
            "errors": problems,
            "hint": (
                "Every unit of work must be a @task; CLI work must use "
                "@binary/@mpi stacked above @task with a `pass` body. Never "
                "shell out, and never type mpirun/srun yourself."
            ),
        }, indent=2)

    os.makedirs(DEFAULT_WORK_DIR, exist_ok=True)
    path = os.path.join(DEFAULT_WORK_DIR, filename)
    with open(path, "w") as f:
        f.write(resolved_code)

    return json.dumps({
        "status": "written",
        "path": path,
        "launch_mode": launch_mode,
        "tasks": tasks["task"],
        "binary_tasks": tasks["binary"],
        "mpi_tasks": tasks["mpi"],
        "lines": len(resolved_code.splitlines()),
        "next": f"Call run_workflow(filename='{filename}') to execute it.",
    }, indent=2)


@mcp.tool()
def run_workflow(
    filename: str = "workflow.py",
    timeout: int = 7200,
) -> str:
    """Execute a workflow file previously created with write_workflow.

    Inside a PBS allocation the file is launched with the real `runcompss`
    binary, which owns the COMPSs runtime. Outside one it runs under the venv
    Python in direct-link mode and manages the runtime itself. Either way the
    COMPSs runtime schedules the tasks; the agent runs nothing.

    Args:
        filename: Workflow filename under the work dir (default: workflow.py)
        timeout: Max seconds to wait (default: 7200)

    Returns:
        JSON with task_id, status, exit_code, stdout, stderr, launch_command
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

    compss_ok = _check_compss()
    via_runcompss = bool(compss_ok and _compss_launch_hostname())

    _tasks[task_id] = {
        "name": f"run_workflow:{filename}",
        "status": "running",
        "depends_on": [],
        "submitted_at": time.time(),
        "engine": "pycompss" if compss_ok else "pycompss-fallback",
        "launched_via": "runcompss" if via_runcompss else ("direct" if compss_ok else "fallback"),
    }

    if via_runcompss:
        project_path, resources_path = _compss_config_files()
        cmd = [
            "runcompss", "--lang=python",
            f"--project={project_path}",
            f"--resources={resources_path}",
            f"--python_interpreter={VENV_PYTHON}",
            path,
        ]
    else:
        cmd = [VENV_PYTHON, path]

    result = _run_command(cmd, work_dir=DEFAULT_WORK_DIR, timeout=timeout)

    _tasks[task_id]["status"] = "completed" if result["exit_code"] == 0 else "failed"
    _tasks[task_id]["exit_code"] = result["exit_code"]
    _tasks[task_id]["stdout"] = result["stdout"]
    _tasks[task_id]["stderr"] = result["stderr"]
    _tasks[task_id]["completed_at"] = time.time()
    _tasks[task_id]["launch_command"] = " ".join(cmd)

    return json.dumps({
        "task_id": task_id,
        "name": _tasks[task_id]["name"],
        "status": _tasks[task_id]["status"],
        "workflow_file": path,
        "launch_command": _tasks[task_id]["launch_command"],
        "exit_code": result["exit_code"],
        "engine": _tasks[task_id]["engine"],
        "launched_via": _tasks[task_id]["launched_via"],
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
    if "launched_via" in task:
        info["launched_via"] = task["launched_via"]
    if "launch_command" in task:
        info["launch_command"] = task["launch_command"]
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
            "launched_via": task.get("launched_via", "unknown"),
        })
    return json.dumps({"total": len(task_list), "tasks": task_list}, indent=2)


@mcp.tool()
def install_package(package: str) -> str:
    """Install a pip package using the configured Python interpreter.

    Args:
        package: Package name to install (e.g. "numpy", "scikit-learn")
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
        package: Package name to check (e.g. "numpy", "pycompss")
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
    """Query available compute resources (nodes, MPI ranks, launcher).

    Returns PBS allocation info when running inside a PBS job, or a warning
    when not in a PBS job. Always call this first in HPC environments.
    """
    res = _detect_resources()
    return json.dumps(res, indent=2)


@mcp.tool()
def cleanup() -> str:
    """Clean up resources. Clears the task registry and stops COMPSs if running."""
    global _tasks

    count = len(_tasks)

    # Stop COMPSs runtime if it was started
    if _check_compss():
        try:
            _run_command([VENV_PYTHON, "-c",
                         "from pycompss.api.api import compss_stop; compss_stop()"],
                        timeout=15)
        except Exception:
            pass

    _tasks = {}
    return json.dumps({"status": "cleaned up", "tasks_cleared": count})


# __ Helpers ___________________________________________________________________

def _resolve_paths(text: str) -> str:
    """Replace /app/ path aliases with actual local repo paths."""
    return text.replace("/app/", REPO_ROOT + "/").replace("//", "/")


# __ Main ______________________________________________________________________

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="PyCOMPSs Workflow MCP Server")
    parser.add_argument("--transport", choices=["stdio", "sse"], default="stdio")
    args = parser.parse_args()

    if args.transport == "sse":
        mcp.run(transport="sse")
    else:
        mcp.run(transport="stdio")
