"""
PyCOMPSs Workflow MCP Server

An MCP server that exposes PyCOMPSs workflow engine capabilities as tools.
The explorer agent connects to this server via MCP protocol to submit tasks,
check status, get results, and manage the workflow execution.

PyCOMPSs uses the @task decorator and COMPSs runtime for task parallelism.
When the COMPSs runtime is available, tasks are submitted through it with
dependency tracking via compss_wait_on(). When the runtime is NOT available
(e.g., local development without COMPSs installed), tasks fall back to
direct Python execution -- same result, just no COMPSs orchestration.

When COMPSs is available AND running inside a PBS job, tasks are launched via
the real `runcompss` binary against this node's own hostname (see
_compss_config_files). Outside a PBS job, COMPSs tasks still run for real but
via a "direct link" (self-managed compss_start()/compss_stop(), no runcompss
launcher) -- runcompss's default SSH-based worker launch would otherwise hit
this cluster's login-node SSH-key+Duo policy.

This server executes tasks locally (or in a virtual environment) using subprocess.
Compatible with HPC environments where COMPSs is installed.

The VENV_PYTHON environment variable controls which Python interpreter to use.

Usage:
    python servers/pycompss_server.py                    # stdio mode
    python servers/pycompss_server.py --transport sse     # SSE mode
"""

import os
import re
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
        # need the absolute path here, not just "mpirun". submit_mpi_task's command
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

    Backstop for crash modes the try/except in _wrap_as_compss_task/_wrap_shell_task
    doesn't cover: runcompss forks a JVM master, which forks Python worker processes
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


def _run_python_script(script: str, work_dir: str = DEFAULT_WORK_DIR, timeout: int = 1800) -> dict:
    """Write a Python script to a temp file and execute it via plain VENV_PYTHON.

    Used for non-task code (e.g. run_lammps's Python-API fallback) that has
    nothing to do with the COMPSs runtime. See _run_compss_task_script() for
    how submit_task's @task-wrapped scripts are launched.
    """
    scripts_dir = os.path.join(work_dir, "_task_scripts")
    os.makedirs(scripts_dir, exist_ok=True)

    fd, script_path = tempfile.mkstemp(suffix=".py", prefix="task_", dir=scripts_dir)
    with os.fdopen(fd, "w") as f:
        f.write(script)
    return _run_command([VENV_PYTHON, script_path], work_dir=work_dir, timeout=timeout)


def _run_compss_task_script(script: str, via_runcompss: bool, work_dir: str = DEFAULT_WORK_DIR,
                             timeout: int = 1800) -> dict:
    """Write a @task-wrapped script (from _wrap_as_compss_task) and execute it.

    via_runcompss=True: launch through the real `runcompss` binary, targeting
    this node's own hostname (see _compss_config_files -- only available inside
    a PBS job). The script must NOT self-manage compss_start()/compss_stop() in
    this mode; runcompss's own launcher owns that lifecycle, and calling them
    again inside the script double-initializes the runtime.

    via_runcompss=False: "direct link" mode -- plain VENV_PYTHON, script
    self-manages compss_start()/@task/compss_wait_on()/compss_stop() itself.
    Used outside a PBS job, where runcompss's SSH-based worker launch would hit
    this cluster's login-node SSH-key+Duo policy.
    """
    # kept in _task_scripts/ after running, not deleted, so the actual code submitted
    # (including the @task/compss_wait_on wrapping) is inspectable after the run,
    # not just visible in the trace.json args field
    scripts_dir = os.path.join(work_dir, "_task_scripts")
    os.makedirs(scripts_dir, exist_ok=True)

    fd, script_path = tempfile.mkstemp(suffix=".py", prefix="task_", dir=scripts_dir)
    with os.fdopen(fd, "w") as f:
        f.write(script)

    if via_runcompss:
        project_path, resources_path = _compss_config_files()
        cmd = [
            "runcompss", "--lang=python",
            f"--project={project_path}",
            f"--resources={resources_path}",
            f"--python_interpreter={VENV_PYTHON}",
            script_path,
        ]
    else:
        cmd = [VENV_PYTHON, script_path]

    result = _run_command(cmd, work_dir=work_dir, timeout=timeout)
    # Surfaced in the task result (and therefore trace.json) so the exact
    # launch command is visible without having to infer it from side effects.
    result["launch_command"] = " ".join(cmd)
    return result


def _wrap_as_compss_task(python_code: str, via_runcompss: bool = False) -> str:
    """Wrap user code in a PyCOMPSs-compatible script.

    If COMPSs runtime is available, wraps code with @task decorator. When
    via_runcompss is True, compss_start()/compss_stop() are omitted -- runcompss's
    own launcher manages that lifecycle, and calling them again here would
    double-initialize the runtime. When False ("direct link" mode), the script
    manages compss_start()/compss_stop() itself. Otherwise (no COMPSs runtime),
    wraps with plain try/except for direct execution.
    """
    if _check_compss() and via_runcompss:
        # runcompss mode: no compss_start/stop here, runcompss's own launcher owns
        # the runtime lifecycle.
        #
        # don't wrap the outer `result = _user_task()` call in try/except. pycompss's
        # binding runs the script's top level twice, once for task registration before
        # the runtime link is up (self.compss is still None), then again for real.
        # wrapping the @task call leaks that first pass's harmless AttributeError into
        # our output instead of letting the binding swallow it like normal.
        #
        # the user code itself DOES need a try/except inside the task body though.
        # an uncaught exception there blows through pycompss's own dispatch frames and
        # kills the worker mid-protocol instead of just reporting a failed task, which
        # wedges every later runcompss call on this node until the worker gets cleaned
        # up. catching it here and returning a plain string keeps the handshake alive
        # no matter what the user's code did
        return f"""\
from pycompss.api.task import task
from pycompss.api.api import compss_wait_on

@task(returns=str)
def _user_task():
    try:
{_indent(python_code, 8)}
    except Exception as _e:
        import traceback as _tb_exc
        return f"__TASK_FAILED__: {{_e}}\\n{{_tb_exc.format_exc()}}"
    return "__TASK_SUCCESS__"

result = _user_task()
result = compss_wait_on(result)
print(result)
"""
    elif _check_compss():
        # Direct-link mode: script self-manages compss_start()/compss_stop()
        return f"""\
import sys, os, traceback

os.makedirs("{DEFAULT_WORK_DIR}", exist_ok=True)
os.chdir("{DEFAULT_WORK_DIR}")

from pycompss.api.api import compss_start, compss_stop, compss_wait_on
from pycompss.api.task import task
from pycompss.api.parameter import INOUT, IN

try:
    compss_start()

    @task(returns=str)
    def _user_task():
{_indent(python_code, 8)}
        return "__TASK_SUCCESS__"

    result = _user_task()
    result = compss_wait_on(result)
    print(result)

    compss_stop()
except Exception as e:
    print(f"__TASK_FAILED__: {{e}}", file=sys.stderr)
    traceback.print_exc(file=sys.stderr)
    try:
        compss_stop()
    except Exception:
        pass
    sys.exit(1)
"""
    else:
        # Fallback mode: direct execution without COMPSs
        return f"""\
import sys, os, traceback

os.makedirs("{DEFAULT_WORK_DIR}", exist_ok=True)
os.chdir("{DEFAULT_WORK_DIR}")

try:
    # --- User task code (PyCOMPSs fallback: direct execution) ---
{_indent(python_code, 4)}
    # --- End user code ---
    print("__TASK_SUCCESS__")
except Exception as e:
    print(f"__TASK_FAILED__: {{e}}", file=sys.stderr)
    traceback.print_exc(file=sys.stderr)
    sys.exit(1)
"""


_SHELL_TASK_RESULT_MARKER = "__SHELL_TASK_RESULT__"


def _wrap_shell_task(shell_cmd: str, work_dir: str, via_runcompss: bool) -> str:
    """Wrap an arbitrary shell command (mpirun or plain) as a PyCOMPSs @task.

    Used for both run_lammps and submit_mpi_task -- any tool that launches an
    external binary (rather than inline Python) via a shell command, where the
    binary's own exit code matters and can't be reduced to submit_task's plain
    success/failure marker (e.g. run_lammps special-cases `lmp`'s SIGSEGV-on-
    cleanup exit 11 when trajectory frames were still written).

    Mirrors _wrap_as_compss_task's via_runcompss/direct-link/fallback split, but
    returns the actual subprocess exit_code/stdout/stderr as JSON (behind
    _SHELL_TASK_RESULT_MARKER) instead of a plain success marker. As with
    _wrap_as_compss_task's via_runcompss branch, the outer `result = _user_task()`
    call has no try/except around it -- wrapping it leaks PyCOMPSs's internal
    pre-runtime registration pass into task output instead of the binding
    absorbing it silently. The task body itself IS wrapped (see
    _wrap_as_compss_task for why: an uncaught exception there desyncs the
    piped-invoker protocol and wedges the whole runcompss runtime instead of
    just failing this one task).
    """
    inner = f"""\
import subprocess, json as _json
proc = subprocess.run(["bash", "-lc", {shell_cmd!r}], cwd={work_dir!r},
                       capture_output=True, text=True)
_RESULT = _json.dumps({{"exit_code": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}})
"""
    if _check_compss() and via_runcompss:
        # tried a real @binary task here (COMPSs's own binary-invocation runtime
        # instead of our subprocess call). @binary(binary="date") with no args works
        # fine, but the moment args= gets populated at all, even something trivial
        # like @binary(binary="echo", args=("hi",)), runcompss hangs forever. tested
        # this across several runs, not specific to bash/-lc, nothing to fix on our
        # side. sticking with the generic @task + subprocess approach below
        return f"""\
from pycompss.api.task import task
from pycompss.api.api import compss_wait_on

@task(returns=str)
def _user_task():
    try:
{_indent(inner, 8)}
    except Exception:
        import json as _json_exc, traceback as _tb_exc
        return _json_exc.dumps({{"exit_code": -1, "stdout": "", "stderr": _tb_exc.format_exc()}})
    return _RESULT

result = _user_task()
result = compss_wait_on(result)
print("{_SHELL_TASK_RESULT_MARKER}" + result)
"""
    elif _check_compss():
        return f"""\
from pycompss.api.api import compss_start, compss_stop, compss_wait_on
from pycompss.api.task import task

compss_start()

@task(returns=str)
def _user_task():
{_indent(inner, 4)}
    return _RESULT

result = _user_task()
result = compss_wait_on(result)
print("{_SHELL_TASK_RESULT_MARKER}" + result)

compss_stop()
"""
    else:
        return f"""\
{inner}
print("{_SHELL_TASK_RESULT_MARKER}" + _RESULT)
"""


def _run_shell_as_compss_task(shell_cmd: str, work_dir: str, via_runcompss: bool, timeout: int) -> dict:
    """Run a shell command through the same runcompss/direct-link machinery as
    submit_task's Python tasks, and unpack the real exit_code/stdout/stderr from
    the _SHELL_TASK_RESULT_MARKER payload (see _wrap_shell_task)."""
    wrapped = _wrap_shell_task(shell_cmd, work_dir, via_runcompss)
    outer = _run_compss_task_script(wrapped, via_runcompss=via_runcompss, work_dir=work_dir, timeout=timeout)

    idx = outer["stdout"].find(_SHELL_TASK_RESULT_MARKER)
    if idx == -1:
        # launch failed before the task ever ran, just surface the outer process's
        # own exit_code/stdout/stderr as-is
        outer["launch_command"] = outer.get("launch_command", "")
        return outer

    payload = json.loads(outer["stdout"][idx + len(_SHELL_TASK_RESULT_MARKER):].strip().splitlines()[0])
    payload["launch_command"] = outer.get("launch_command", "")
    return payload


# __ MCP Tools _________________________________________________________________

@mcp.tool()
def submit_task(
    name: str,
    python_code: str,
    depends_on: list[str] | None = None,
    timeout: int = 1800,
) -> str:
    """Submit a Python task for execution via the PyCOMPSs workflow engine.

    The task runs as a PyCOMPSs @task when the COMPSs runtime is available,
    or falls back to direct Python execution otherwise.
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

    # Redirect LAMMPS code to the dedicated run_lammps tool
    if "from lammps import" in python_code or "import lammps" in python_code:
        return json.dumps({
            "task_id": task_id,
            "status": "rejected",
            "error": (
                "LAMMPS must be run via the `run_lammps` tool, not submit_task. "
                "Call: run_lammps(script='in.watbox', work_dir='/app/work/run0'). "
                "The run_lammps tool handles HPC vs local execution automatically."
            ),
        })

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

    compss_ok = _check_compss()
    via_runcompss = bool(compss_ok and _compss_launch_hostname())

    # Register task
    _tasks[task_id] = {
        "name": name,
        "status": "running",
        "depends_on": depends_on or [],
        "submitted_at": time.time(),
        "engine": "pycompss" if compss_ok else "pycompss-fallback",
        "launched_via": "runcompss" if via_runcompss else ("direct" if compss_ok else "fallback"),
    }

    # Resolve /app/ path aliases in user code
    resolved_code = _resolve_paths(python_code)

    # Wrap and execute
    wrapped_script = _wrap_as_compss_task(resolved_code, via_runcompss=via_runcompss)
    result = _run_compss_task_script(wrapped_script, via_runcompss=via_runcompss, timeout=timeout)

    # Update task status
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
    _tasks[task_id]["launch_command"] = result.get("launch_command", "")

    return json.dumps({
        "task_id": task_id,
        "name": name,
        "status": _tasks[task_id]["status"],
        "exit_code": result["exit_code"],
        "stdout": result["stdout"][:3000],
        "stderr": result["stderr"][:3000],
        "engine": _tasks[task_id]["engine"],
        "launched_via": _tasks[task_id]["launched_via"],
        "launch_command": _tasks[task_id]["launch_command"],
    }, indent=2)


@mcp.tool()
def submit_shell_task(
    name: str,
    command: str,
    work_dir: str = "",
    timeout: int = 1800,
) -> str:
    """Submit a shell command for execution.

    Use this for file operations, system commands, and non-Python tasks.

    Args:
        name: Descriptive name for this task (e.g. "copy_data_files", "create_directories")
        command: Shell command to execute (e.g. "mkdir -p /app/work/run0/frames")
        work_dir: Working directory (default: repo work/run0)
        timeout: Max seconds to wait

    Returns:
        JSON with task_id, status, and execution results
    """
    task_id = f"task_{uuid.uuid4().hex[:8]}"

    if (re.search(r"(^|[/\s])lmp(_mpi|_serial)?(\s|$)", command)
            or "from lammps import" in command
            or re.search(r"\bimport\s+lammps\b", command)):
        return json.dumps({
            "task_id": task_id,
            "status": "rejected",
            "error": (
                "LAMMPS must be run via the `run_lammps` tool, not submit_shell_task. "
                "Call: run_lammps(script='in.watbox', work_dir='/app/work/run0')."
            ),
        })

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
def get_task_status(task_id: str) -> str:
    """Get the current status of a submitted task.

    Args:
        task_id: The task ID returned by submit_task or submit_shell_task
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
        task_id: The task ID returned by submit_task or submit_shell_task
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
def submit_mpi_task(
    name: str,
    command: str,
    num_ranks: int = 0,
    work_dir: str = "",
    timeout: int = 1800,
) -> str:
    """Run a command under MPI (mpirun -np N <command>).

    Use for MPI-capable executables: LAMMPS binary (lmp), mpi4py scripts, etc.
    This is the correct way to use all allocated nodes on HPC.

    Args:
        name: Descriptive name for this task
        command: Command to run (e.g. "lmp -in in.watbox"). Do NOT include mpirun.
        num_ranks: Number of MPI ranks. 0 = auto-detect from PBS_NP (recommended).
        work_dir: Working directory (default: repo work/run0)
        timeout: Max seconds to wait (default: 1800)
    """
    import shutil
    task_id = f"task_{uuid.uuid4().hex[:8]}"
    _work = _resolve_paths(work_dir) if work_dir else DEFAULT_WORK_DIR

    res = _detect_resources()
    launcher = res["launcher"]
    if not launcher:
        return json.dumps({
            "task_id": task_id,
            "status": "failed",
            "error": "No MPI launcher (mpirun/mpiexec) found in PATH.",
        }, indent=2)

    ranks = num_ranks if num_ranks > 0 else res["ntasks"]
    resolved_cmd = _resolve_paths(command)
    # unset PMI_SIZE/PMI_RANK/I_MPI_HYDRA_BOOTSTRAP before the real launcher.
    # TASK_ENV always sets these so LAMMPS's non-mpirun python API can init MPI
    # without hanging, but a real mpirun/mpiexec inheriting PMI_SIZE=1/PMI_RANK=0
    # thinks it's already inside a size-1 MPI job and hangs launching new ranks.
    # saw this hang hacc_tpm via mpiexec directly, run_lammps has the same fix
    full_cmd = f"env -u PMI_SIZE -u PMI_RANK -u I_MPI_HYDRA_BOOTSTRAP {launcher} -np {ranks} {resolved_cmd}"

    compss_ok = _check_compss()
    via_runcompss = bool(compss_ok and _compss_launch_hostname())

    _tasks[task_id] = {
        "name": name,
        "status": "running",
        "depends_on": [],
        "submitted_at": time.time(),
        "engine": "pycompss" if compss_ok else "pycompss-fallback",
        "launched_via": "runcompss" if via_runcompss else ("direct" if compss_ok else "fallback"),
    }

    # run as a real @task (runcompss inside PBS, direct-link otherwise) instead of a
    # bare subprocess, same coverage submit_task and run_lammps already get, so
    # MPI-launched binaries show up as PyCOMPSs-orchestrated too
    result = _run_shell_as_compss_task(full_cmd, _work, via_runcompss, timeout)

    _tasks[task_id]["status"] = "completed" if result["exit_code"] == 0 else "failed"
    _tasks[task_id]["exit_code"] = result["exit_code"]
    _tasks[task_id]["stdout"] = result["stdout"]
    _tasks[task_id]["stderr"] = result["stderr"]
    _tasks[task_id]["launch_command"] = result.get("launch_command", "")
    _tasks[task_id]["completed_at"] = time.time()

    return json.dumps({
        "task_id": task_id,
        "name": name,
        "status": _tasks[task_id]["status"],
        "exit_code": result["exit_code"],
        "launcher": launcher,
        "ranks": ranks,
        "stdout": result["stdout"][:3000],
        "stderr": result["stderr"][:3000],
        "engine": _tasks[task_id]["engine"],
        "launched_via": _tasks[task_id]["launched_via"],
        "launch_command": _tasks[task_id]["launch_command"],
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

    frames_dir = os.path.join(_work, "frames")
    if os.path.isdir(frames_dir):
        for _old in _glob.glob(os.path.join(frames_dir, "*.lammpstrj")):
            os.remove(_old)

    res = _detect_resources()
    use_mpi = res["in_pbs"] and bool(res["launcher"])
    method = "mpi" if use_mpi else "python_api"
    ranks = res["ntasks"] if use_mpi else 1

    compss_ok = _check_compss()
    via_runcompss = bool(compss_ok and _compss_launch_hostname())

    _tasks[task_id] = {
        "name": f"run_lammps:{script}",
        "status": "running",
        "depends_on": [],
        "submitted_at": time.time(),
        "method": method,
        "engine": "pycompss" if compss_ok else "pycompss-fallback",
        "launched_via": "runcompss" if via_runcompss else ("direct" if compss_ok else "fallback"),
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
        # same deal as submit_mpi_task, run this as a real @task so the actual
        # simulation shows up as PyCOMPSs-orchestrated, not just the post-processing
        result = _run_shell_as_compss_task(cmd, _work, via_runcompss, timeout)
    else:
        py_script = f"""\
import os
os.chdir("{_work}")
from lammps import lammps
lmp = lammps(cmdargs=["-screen", "none"])
lmp.file("{script}")
lmp.close()
"""
        wrapped = _wrap_as_compss_task(py_script, via_runcompss=via_runcompss)
        raw = _run_compss_task_script(wrapped, via_runcompss=via_runcompss, work_dir=_work, timeout=timeout)
        success = raw["exit_code"] == 0 and "__TASK_SUCCESS__" in raw["stdout"]
        result = {
            "exit_code": 0 if success else (raw["exit_code"] or 1),
            "stdout": raw["stdout"].replace("__TASK_SUCCESS__", "").strip(),
            "stderr": raw["stderr"],
            "launch_command": raw.get("launch_command", ""),
        }

    exit_code = result["exit_code"]
    frames_written = _glob.glob(os.path.join(frames_dir, "*.lammpstrj")) if os.path.isdir(frames_dir) else []
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
    _tasks[task_id]["launch_command"] = result.get("launch_command", "")
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
        "engine":    _tasks[task_id]["engine"],
        "launched_via": _tasks[task_id]["launched_via"],
        "launch_command": _tasks[task_id]["launch_command"],
    }, indent=2)


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

def _indent(text: str, spaces: int) -> str:
    """Indent every line of text by the given number of spaces."""
    prefix = " " * spaces
    return "\n".join(prefix + line for line in text.splitlines())


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
