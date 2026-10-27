"""
Base class for every workflow-engine MCP server.

One class, `Server`, owns the whole MCP surface. A concrete engine server is a
subclass that supplies three things:

    ENGINE            the engine's name, e.g. "parsl"
    DESCRIPTION       one line, used as the MCP server's instructions
    launch_command()  how this engine turns a workflow file into an argv list

Everything else -- the eight tools the explorer actually calls -- is implemented
here once and inherited:

    write_file        write any file (workflow .py, XML, JSON, params, ...)
    read_file         read a file back
    list_files        list a directory tree
    check_package     is a Python package importable
    install_package   pip install into the configured interpreter
    get_status        status of workflow runs started by run_workflow
    get_resources     nodes / ranks / launcher visible to this process
    run_workflow      run THE workflow file, once, in one subprocess

Adding a new engine is therefore a subclass with ENGINE, DESCRIPTION and
launch_command(), and nothing else. Nothing in this file imports or touches
parsl, PyCOMPSs or ADIOS2.

Hooks a subclass may override (all optional, all have working defaults):

    launch_env()      environment for the workflow subprocess
    resource_info()   extra engine facts appended to get_resources
    workflow_notes()  structural warnings about a generated workflow file
    prepare_run()     anything to do just before a run (clear stale state, ...)
    register_tools()  register engine-only tools (e.g. PyCOMPSs's write_xml)

Environment
-----------
This file resolves nothing at runtime beyond what the environment states, and
hardcodes no site paths. Everything is read from the environment, which the
operator exports from a shell script before launching:

    MAW_REPO_ROOT      repo root; /app/ aliases resolve to it.
                       Default: the directory above this one.
    MAW_WORK_DIR       where generated files are written and run.
                       Default: $MAW_REPO_ROOT/work/run0
    MAW_DATA_DIR       input data directory.  Default: $MAW_REPO_ROOT/data
    MAW_PYTHON         interpreter that runs workflows and pip.
                       Default: $VENV_PYTHON, else the server's own.
    MAW_MPI_LAUNCHER   mpirun/mpiexec/srun, used by engines that launch ranks.
                       Default: "mpirun".

Subclasses read their own variables the same way -- see each server file.

Subprocesses
------------
Exactly one level, ever. run_workflow spawns the workflow; the workflow may do
whatever its engine does. check_package / install_package spawn a short-lived
probe. Nothing here spawns a process that spawns another on our behalf.
"""

from __future__ import annotations

import ast
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from abc import ABC, abstractmethod
from typing import Optional

from fastmcp import FastMCP


class Server(ABC):
    """An MCP server exposing one workflow engine.

    Subclasses set ENGINE and DESCRIPTION and implement launch_command().
    """

    # __ Subclass contract _____________________________________________________

    ENGINE: str = ""
    DESCRIPTION: str = ""

    #: Appended to run_workflow's docstring by the subclass if it wants to say
    #: something engine-specific about how the file is launched.
    LAUNCH_NOTE: str = ""

    @abstractmethod
    def launch_command(self, workflow_path: str, args: list[str], ranks: int) -> list[str]:
        """Build the argv that runs `workflow_path` under this engine.

        Called by run_workflow, which then spawns it as a single subprocess.

        Args:
            workflow_path: absolute path of the workflow file to run
            args: arguments to pass to the workflow file itself
            ranks: rank count the caller asked for; 0 means "unset"

        Returns:
            argv list, e.g. ["/path/python", "/work/workflow.py"]
        """

    # __ Construction __________________________________________________________

    def __init__(self) -> None:
        if not self.ENGINE:
            raise ValueError(
                f"{type(self).__name__} must set ENGINE -- it names the engine "
                f"in every tool result and in the trace."
            )

        self.repo_root = os.environ.get(
            "MAW_REPO_ROOT",
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        ).rstrip("/")
        self.work_dir = os.environ.get(
            "MAW_WORK_DIR", os.path.join(self.repo_root, "work", "run0")
        )
        self.data_dir = os.environ.get(
            "MAW_DATA_DIR", os.path.join(self.repo_root, "data")
        )
        # One interpreter for everything the server runs: the workflow, pip, and
        # import probes. They must agree, or check_package answers about a venv
        # the workflow never sees.
        self.python = os.environ.get(
            "MAW_PYTHON", os.environ.get("VENV_PYTHON", sys.executable)
        )
        self.mpi_launcher = os.environ.get("MAW_MPI_LAUNCHER", "mpirun")

        os.makedirs(self.work_dir, exist_ok=True)

        #: run_id -> record, populated by run_workflow and read by get_status.
        self.runs: dict[str, dict] = {}

        self.mcp = FastMCP(
            f"{self.ENGINE} workflow engine",
            instructions=self.DESCRIPTION or f"MCP server for the {self.ENGINE} workflow engine",
        )
        self._register_common_tools()
        self.register_tools()

    def _register_common_tools(self) -> None:
        """Bind the eight shared tools, plus cleanup, to this server's MCP app.

        Bound methods are registered rather than module-level functions, so each
        tool closes over `self` and a subclass inherits all of them without
        redeclaring anything. FastMCP builds each tool's schema from the
        signature and docstring and drops `self`.
        """
        for method in (
            self.write_file,
            self.read_file,
            self.list_files,
            self.check_package,
            self.install_package,
            self.get_status,
            self.get_resources,
            self.cleanup,
        ):
            self.register_tool(method)

        # run_workflow is the one shared tool whose description depends on the
        # engine: the tool is identical, how the file gets launched is not.
        self.register_tool(self.run_workflow, description=self._run_workflow_help())

    def register_tool(self, method, description: Optional[str] = None) -> None:
        """Expose a bound method as an MCP tool.

        The keyword form of FastMCP's decorator is used deliberately -- it is
        the one form every FastMCP generation accepts, including the copy
        vendored inside the mcp package.
        """
        self.mcp.tool(name=method.__name__, description=description)(method)

    def _run_workflow_help(self) -> str:
        """run_workflow's docstring with this engine's launch note appended."""
        base = (self.run_workflow.__doc__ or "").strip()
        if not self.LAUNCH_NOTE:
            return base
        return f"{base}\n\nFor {self.ENGINE}: {self.LAUNCH_NOTE.strip()}"

    def register_tools(self) -> None:
        """Register engine-only tools. Default: none.

        Override in a subclass to add tools that exist for that engine alone,
        e.g. PyCOMPSs's write_xml:

            def register_tools(self):
                self.mcp.tool(self.write_xml)
        """

    # __ Hooks _________________________________________________________________

    def launch_env(self) -> dict:
        """Environment for the workflow subprocess.

        The inherited environment, which is the one the operator's setup script
        built. Override to add engine variables -- and add, don't replace: the
        Spack/venv paths already in here are what the engine's own libraries
        were linked against.
        """
        return dict(os.environ)

    def resource_info(self) -> dict:
        """Engine facts to merge into get_resources. Default: none."""
        return {}

    #: Structural expectations for a generated workflow file, as data rather
    #: than code, so a subclass declares them instead of implementing a check.
    #: Each entry is (alternatives, message): if none of `alternatives` appears
    #: in the file, `message` is reported. Default: nothing expected.
    WORKFLOW_CHECKS: tuple[tuple[tuple[str, ...], str], ...] = ()

    def workflow_notes(self, code: str) -> list[str]:
        """Structural warnings about a generated workflow file.

        Driven by WORKFLOW_CHECKS, so most subclasses need no code here at all.

        Returned by write_file as `warnings`, never as a rejection: a warning is
        advice the agent can act on, whereas a false rejection leaves it with no
        way to proceed. Only a syntax error rejects a file.
        """
        return [
            message for alternatives, message in self.WORKFLOW_CHECKS
            if not any(alternative in code for alternative in alternatives)
        ]

    def prepare_run(self, workflow_path: str) -> dict:
        """Called immediately before a run. Return facts to include in the result.

        Default: nothing. Override to clear stale engine state, write a config
        the launcher needs, and so on.
        """
        return {}

    # __ Shared tools __________________________________________________________

    def write_file(self, path: str, content: str, overwrite: bool = True) -> str:
        """Write a file: the workflow itself, or anything the workflow needs.

        Use this for every file you generate -- the workflow .py, and any
        configuration it reads (XML, JSON, YAML, .params, input decks). Write
        the configuration BEFORE the workflow that reads it.

        A bare filename lands in the work directory. A relative path is taken
        relative to the work directory, so "params/indat.params" is fine and
        intermediate directories are created. /app/ paths resolve to the repo.

        Python files are parsed before being written and a syntax error is
        rejected, so a broken file never reaches disk. Anything the engine would
        rather you did differently comes back in `warnings`, which does not stop
        the write.

        Args:
            path: file to write, e.g. "workflow.py" or "config/project.xml"
            content: full contents of the file
            overwrite: allow replacing an existing file (default: True)

        Returns:
            JSON with status, path, bytes, lines, and any warnings
        """
        try:
            full = self._resolve_write_path(path)
        except ValueError as e:
            return self._json({"status": "rejected", "error": str(e)})

        if os.path.exists(full) and not overwrite:
            return self._json({
                "status": "rejected",
                "path": full,
                "error": "file exists and overwrite=False",
            })

        resolved = self.resolve_aliases(content)
        warnings: list[str] = []

        if full.endswith(".py"):
            try:
                ast.parse(resolved)
            except SyntaxError as e:
                return self._json({
                    "status": "rejected",
                    "path": full,
                    "error": f"SyntaxError on line {e.lineno}: {e.msg}",
                    "hint": "Fix the syntax and call write_file again.",
                })
            warnings = self.workflow_notes(resolved)

        os.makedirs(os.path.dirname(full) or self.work_dir, exist_ok=True)
        with open(full, "w") as f:
            f.write(resolved)

        result = {
            "status": "written",
            "engine": self.ENGINE,
            "path": full,
            "bytes": len(resolved.encode()),
            "lines": len(resolved.splitlines()),
        }
        if warnings:
            result["warnings"] = warnings
        if full.endswith(".py"):
            result["next"] = (
                f"Call run_workflow(filename='{os.path.relpath(full, self.work_dir)}') "
                f"when every file it needs has been written."
            )
        return self._json(result)

    def read_file(self, path: str, max_lines: int = 200, start_line: int = 1) -> str:
        """Read a file back.

        Use it to inspect outputs, check a generated file, or read a log after a
        failed run. /app/ paths resolve to the repo.

        Args:
            path: path of the file to read
            max_lines: how many lines to return (default: 200)
            start_line: 1-based line to start at (default: 1)

        Returns:
            JSON with content, total_lines, and whether it was truncated
        """
        full = self._resolve_read_path(path)
        if not os.path.isfile(full):
            return self._json({"path": full, "error": f"File not found: {full}"})

        try:
            with open(full, errors="replace") as f:
                lines = f.readlines()
        except OSError as e:
            return self._json({"path": full, "error": str(e)})

        start = max(1, start_line) - 1
        chunk = lines[start:start + max(1, max_lines)]
        return self._json({
            "path": full,
            "content": "".join(chunk),
            "start_line": start + 1,
            "returned_lines": len(chunk),
            "total_lines": len(lines),
            "truncated": start + len(chunk) < len(lines),
        })

    def list_files(self, directory: str = "", max_entries: int = 400,
                   max_depth: int = 4) -> str:
        """List the files under a directory.

        Use it to confirm the outputs you expected actually exist. Engine
        scratch directories are pruned -- they say nothing about whether the
        workflow produced anything.

        Args:
            directory: directory to list (default: the work directory)
            max_entries: stop after this many files (default: 400)
            max_depth: levels below `directory` to descend (default: 4)

        Returns:
            JSON with files, count, and whether the walk was truncated
        """
        full = self._resolve_read_path(directory) if directory else self.work_dir
        if not os.path.isdir(full):
            return self._json({
                "directory": full, "files": [], "count": 0,
                "error": f"Directory not found: {full}",
            })

        files: list[str] = []
        truncated = False
        base_depth = full.rstrip(os.sep).count(os.sep)
        for root, dirs, names in os.walk(full):
            if root.rstrip(os.sep).count(os.sep) - base_depth >= max_depth:
                dirs[:] = []
            else:
                dirs[:] = [d for d in dirs if d not in self.SCRATCH_DIRS]
            for name in names:
                if len(files) >= max_entries:
                    truncated = True
                    break
                files.append(os.path.join(root, name))
            if truncated:
                break

        return self._json({
            "directory": full,
            "files": sorted(files),
            "count": len(files),
            "truncated": truncated,
        })

    def check_package(self, package: str) -> str:
        """Check whether a Python package can be imported.

        Probed in a child process using the same interpreter that runs
        workflows, so the answer is about the environment the workflow will
        actually see. A child is used because some scientific packages
        initialise MPI on import and can take the process down with them.

        Args:
            package: import name, e.g. "numpy", "mpi4py", "adios2"

        Returns:
            JSON with installed (bool) and version when it is
        """
        probe = (
            f"import {package} as _m; "
            f"print(getattr(_m, '__version__', 'unknown'))"
        )
        result = self._run([self.python, "-c", probe], timeout=60)
        if result["exit_code"] == 0:
            return self._json({
                "package": package,
                "installed": True,
                "version": result["stdout"].strip() or "unknown",
            })
        return self._json({
            "package": package,
            "installed": False,
            "error": result["stderr"][-500:],
        })

    def install_package(self, package: str) -> str:
        """pip-install a package into the interpreter that runs workflows.

        For pure-Python dependencies only. The engine itself and anything with
        compiled MPI bindings come from the environment the operator built
        before this server started; pip cannot fix those.

        Args:
            package: pip requirement, e.g. "numpy" or "matplotlib==3.9.0"

        Returns:
            JSON with status and the tail of pip's output
        """
        result = self._run(
            [self.python, "-m", "pip", "install", package], timeout=900,
        )
        ok = result["exit_code"] == 0
        return self._json({
            "package": package,
            "status": "success" if ok else "failed",
            "message": (result["stdout"] if ok else result["stderr"])[-800:],
        })

    def get_status(self, run_id: str = "") -> str:
        """Status of the workflow runs started by run_workflow.

        With no argument, every run and its outcome. With a run_id, that run in
        full, including its complete stdout and stderr.

        Args:
            run_id: the run_id returned by run_workflow (default: all runs)

        Returns:
            JSON with the run record(s)
        """
        if not run_id:
            return self._json({
                "engine": self.ENGINE,
                "total": len(self.runs),
                "runs": [
                    {
                        "run_id": rid,
                        "workflow_file": rec["workflow_file"],
                        "status": rec["status"],
                        "exit_code": rec.get("exit_code"),
                        "duration_seconds": rec.get("duration_seconds"),
                    }
                    for rid, rec in self.runs.items()
                ],
            })

        record = self.runs.get(run_id)
        if record is None:
            return self._json({
                "error": f"No run with id '{run_id}'.",
                "known_runs": list(self.runs),
            })
        return self._json({"run_id": run_id, **record})

    def get_resources(self) -> str:
        """Compute resources visible to this server: nodes, ranks, launcher.

        Call this before writing a workflow that asks for ranks. Under PBS it
        reports the allocation; off a scheduler everything falls back to one
        node and `in_pbs` is false, which means MPI and multi-node work are not
        available no matter what the task list says.

        Returns:
            JSON with in_pbs, nnodes, ntasks, ranks_per_node, nodelist, launcher,
            the work and data directories, and any engine-specific facts
        """
        info = self._detect_resources()
        info.update({
            "engine": self.ENGINE,
            "work_dir": self.work_dir,
            "data_dir": self.data_dir,
            "python": self.python,
        })
        info.update(self.resource_info())
        return self._json(info)

    def run_workflow(self, filename: str = "workflow.py",
                     args: Optional[list[str]] = None,
                     ranks: int = 0,
                     timeout: int = 7200) -> str:
        """Run the workflow file. This is the only way work gets executed.

        The file must already exist -- write it with write_file first, along
        with every configuration file it reads. The server builds this engine's
        launch command for it and runs that, once, as a single subprocess.

        Args:
            filename: workflow file, relative to the work directory
                      (default: workflow.py)
            args: arguments passed to the workflow file itself
            ranks: how many ranks to launch, for engines that launch ranks.
                   0 (default) means the engine decides.
            timeout: seconds to wait before the run is killed (default: 7200)

        While it runs, the workflow's output goes to <run_id>.out and
        <run_id>.err in the work directory -- not held in a pipe until the end
        -- so progress can be watched with read_file on those paths rather than
        waiting for this call to return. Their paths come back in the result.

        Returns:
            JSON with run_id, status, exit_code, the exact command used,
            stdout and stderr tails, and stdout_file / stderr_file
        """
        run_id = f"run_{uuid.uuid4().hex[:8]}"
        path = self._resolve_write_path(filename, must_be_relative=False)

        if not os.path.isfile(path):
            return self._json({
                "run_id": run_id,
                "status": "failed",
                "error": f"No workflow file at {path}.",
                "hint": f"Call write_file(path='{filename}', content=...) first.",
            })

        extra = self.prepare_run(path) or {}

        try:
            command = self.launch_command(path, [str(a) for a in (args or [])], ranks)
        except Exception as e:  # a bad launch config must not kill the server
            return self._json({
                "run_id": run_id,
                "status": "failed",
                "workflow_file": path,
                "error": f"Could not build the {self.ENGINE} launch command: {e}",
            })

        return self.launch_and_record(
            run_id, command, path, timeout,
            {"args": list(args or []), "ranks": ranks, **extra},
        )

    def launch_and_record(self, run_id: str, command: list[str], workflow_path: str,
                          timeout: int, extra: Optional[dict] = None) -> str:
        """Run `command`, record the outcome under `run_id`, return the result JSON.

        Split out of run_workflow so an engine that needs a second way to launch
        -- ADIOS2's MPMD form, where each stage is its own rank block of one
        mpirun -- gets the same record, the same log collection and the same
        result shape without restating any of it.
        """
        extra = extra or {}
        started = time.time()
        # log_prefix => the child writes to <work_dir>/<run_id>.out/.err, which
        # can be tailed while the run is in flight instead of only after it
        # ends. The paths go back in the result so the agent can read_file them
        # and the operator can tail them.
        result = self._run(command, timeout=timeout, env=self.launch_env(),
                           log_prefix=run_id)
        duration = round(time.time() - started, 2)

        record = {
            "engine": self.ENGINE,
            "workflow_file": workflow_path,
            "command": " ".join(command),
            "status": "completed" if result["exit_code"] == 0 else "failed",
            "exit_code": result["exit_code"],
            "stdout": result["stdout"],
            "stderr": result["stderr"],
            "stdout_file": result.get("stdout_file"),
            "stderr_file": result.get("stderr_file"),
            "timed_out": result.get("timed_out", False),
            "duration_seconds": duration,
            "started_at": started,
            **extra,
        }
        self.runs[run_id] = record

        payload = {
            "run_id": run_id,
            "engine": self.ENGINE,
            "status": record["status"],
            "workflow_file": workflow_path,
            "launch_command": record["command"],
            "exit_code": result["exit_code"],
            "duration_seconds": duration,
            "stdout": result["stdout"][-6000:],
            "stderr": result["stderr"][-6000:],
            "stdout_file": result.get("stdout_file"),
            "stderr_file": result.get("stderr_file"),
            **extra,
        }
        if result["exit_code"] != 0:
            payload["logs"] = self.collect_logs()
            payload["hint"] = (
                f"Read the stderr above and the logs. The stdout and stderr "
                f"above are the tail only -- the complete streams are on disk "
                f"at {result.get('stdout_file')} and "
                f"{result.get('stderr_file')}, readable with read_file. Fix the "
                f"workflow file, write it again, then re-run. "
                f"get_status('{run_id}') also has more of the output."
            )
        return self._json(payload)

    def new_run_id(self) -> str:
        """A fresh run id, for an engine-specific runner that needs one."""
        return f"run_{uuid.uuid4().hex[:8]}"

    def cleanup(self) -> str:
        """Clear this server's record of previous runs.

        Files on disk are left alone -- they are the run's output.
        """
        count = len(self.runs)
        self.runs = {}
        return self._json({
            "status": "cleaned up", "engine": self.ENGINE, "runs_cleared": count,
        })

    # __ Shared helpers ________________________________________________________

    #: Engine bookkeeping rather than run output. Pruned from list_files and
    #: skipped when collecting logs.
    SCRATCH_DIRS = (
        "__pycache__", ".git", "runinfo", ".parsl", ".compss", "_task_scripts",
    )

    #: Filenames matching these are tailed into a failed run's result.
    LOG_GLOBS: tuple[str, ...] = ("*.err", "*.out", "*.log", "*.stderr", "*.stdout")

    def collect_logs(self, max_files: int = 4, tail: int = 1500) -> dict:
        """Tail the newest log files under the work directory.

        A failed run's real error is usually not on the launch pipe: engines
        write it to their own per-task log. Surfacing the newest few here saves
        the agent a dozen read_file calls hunting for them.
        """
        import glob

        paths: list[str] = []
        for pattern in self.LOG_GLOBS:
            paths += glob.glob(os.path.join(self.work_dir, "**", pattern),
                               recursive=True)

        paths = [
            p for p in dict.fromkeys(paths)
            if os.path.isfile(p) and os.path.getsize(p) > 0
        ]
        paths.sort(key=os.path.getmtime, reverse=True)

        logs: dict[str, str] = {}
        for p in paths[:max_files]:
            try:
                with open(p, errors="replace") as f:
                    logs[os.path.relpath(p, self.work_dir)] = f.read()[-tail:]
            except OSError:
                continue
        return logs

    def resolve_aliases(self, text: str) -> str:
        """Rewrite /app/ aliases to this repo.

        The skills and prompts talk about /app/data and /app/work/run0; those
        are aliases, not directories. Only the prefix is rewritten, so a URL
        elsewhere in a generated file is left alone.
        """
        return text.replace("/app/work/run0", self.work_dir) \
                   .replace("/app/data", self.data_dir) \
                   .replace("/app/", self.repo_root + "/")

    def _resolve_read_path(self, path: str) -> str:
        """Absolute path for reading: aliases resolved, relative to work_dir."""
        resolved = self.resolve_aliases(path)
        if not os.path.isabs(resolved):
            resolved = os.path.join(self.work_dir, resolved)
        return os.path.normpath(resolved)

    def _resolve_write_path(self, path: str, must_be_relative: bool = True) -> str:
        """Absolute path for writing, confined to the work directory.

        Generated files belong under work_dir: it is the one directory the run
        owns, the one the archiver copies, and the one every engine is pointed
        at. A path that escapes it is rejected rather than quietly relocated,
        so the agent finds out now instead of after a run writes nothing where
        it was looking.
        """
        resolved = self.resolve_aliases(path)
        if not os.path.isabs(resolved):
            resolved = os.path.join(self.work_dir, resolved)
        resolved = os.path.normpath(resolved)

        if must_be_relative:
            root = os.path.normpath(self.work_dir)
            if resolved != root and not resolved.startswith(root + os.sep):
                raise ValueError(
                    f"'{path}' resolves outside the work directory ({root}). "
                    f"Write generated files under the work directory."
                )
        return resolved

    #: How much of a captured stream to return in a tool result. The files on
    #: disk keep everything; this only caps what travels back through MCP.
    STREAM_TAIL_CHARS = 200_000

    def _run(self, command: list[str], timeout: int,
             env: Optional[dict] = None,
             log_prefix: Optional[str] = None) -> dict:
        """Run one command to completion. The only place a process is spawned.

        start_new_session + killpg, not proc.kill(): an engine launcher leaves
        children behind that hold our streams open, and killing only the direct
        child leaves the wait hanging on a process group that is still alive --
        so the timeout would not actually be a bound. One process group makes it
        one.

        With `log_prefix`, the child's stdout and stderr go to FILES under the
        work directory rather than to pipes, and are read back afterwards.

        That matters for anything long-running. A pipe plus communicate() holds
        every byte until the process exits, so a workflow with a two-hour
        timeout shows nothing at all for two hours -- no simulation progress,
        no traceback, nothing to look at while it hangs. The output existed the
        whole time; it was sitting in a pipe nobody could read. Files can be
        tailed while the run is in flight, by the operator and by read_file,
        and they survive a timeout kill, so a hung run leaves its partial
        output behind instead of just the word "Timed out".

        Short probes (check_package, install_package) pass no prefix and keep
        the pipes: they finish in seconds and leaving files behind for each one
        would clutter the directory the run owns.
        """
        os.makedirs(self.work_dir, exist_ok=True)

        out_path = err_path = None
        out_fh = err_fh = None
        if log_prefix:
            out_path = os.path.join(self.work_dir, f"{log_prefix}.out")
            err_path = os.path.join(self.work_dir, f"{log_prefix}.err")
            try:
                out_fh = open(out_path, "w")
                err_fh = open(err_path, "w")
            except OSError as e:
                if out_fh:
                    out_fh.close()
                return {"exit_code": -1, "stdout": "",
                        "stderr": f"cannot open log files under {self.work_dir}: {e}"}

        try:
            proc = subprocess.Popen(
                command,
                stdout=out_fh or subprocess.PIPE,
                stderr=err_fh or subprocess.PIPE,
                text=True,
                cwd=self.work_dir,
                env=env if env is not None else dict(os.environ),
                start_new_session=True,
            )
        except (OSError, ValueError) as e:
            for fh in (out_fh, err_fh):
                if fh:
                    fh.close()
            return {"exit_code": -1, "stdout": "", "stderr": f"{command[0]}: {e}"}

        timed_out = False
        try:
            if log_prefix:
                proc.wait(timeout=timeout)
            else:
                stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            self._kill_group(proc)
            timed_out = True
            if not log_prefix:
                stdout, stderr = "", ""
        finally:
            for fh in (out_fh, err_fh):
                if fh:
                    fh.close()

        if log_prefix:
            # Read back what the child wrote. On a timeout this is the whole
            # point: everything up to the kill is still here.
            stdout = self._tail_file(out_path)
            stderr = self._tail_file(err_path)

        result = {
            "exit_code": -1 if timed_out else proc.returncode,
            "stdout": (stdout or "").strip(),
            "stderr": (stderr or "").strip(),
        }
        if timed_out:
            note = f"Timed out after {timeout}s and was killed."
            result["stderr"] = f"{note}\n{result['stderr']}".strip()
            result["timed_out"] = True
        if log_prefix:
            result["stdout_file"] = out_path
            result["stderr_file"] = err_path
        return result

    def _tail_file(self, path: Optional[str]) -> str:
        """Last STREAM_TAIL_CHARS of a file, or '' if it is not readable."""
        if not path or not os.path.isfile(path):
            return ""
        try:
            size = os.path.getsize(path)
            with open(path, errors="replace") as f:
                if size > self.STREAM_TAIL_CHARS:
                    f.seek(size - self.STREAM_TAIL_CHARS)
                    return "...[earlier output in the file]...\n" + f.read()
                return f.read()
        except OSError:
            return ""

    @staticmethod
    def _kill_group(proc: subprocess.Popen) -> None:
        """SIGKILL the whole process group started for `proc`."""
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass

    def _detect_resources(self) -> dict:
        """Read the allocation from PBS environment variables.

        Environment only. PBS_NODEFILE is a path PBS itself exported, so reading
        it is reading the environment, not searching for anything.
        """
        in_pbs = bool(os.environ.get("PBS_JOBID"))

        hosts: list[str] = []
        nodefile = os.environ.get("PBS_NODEFILE", "")
        if nodefile and os.path.isfile(nodefile):
            try:
                with open(nodefile) as f:
                    hosts = f.read().split()
            except OSError:
                hosts = []

        if hosts:
            unique = list(dict.fromkeys(hosts))
            nnodes = len(unique)
            # PBS Pro sometimes writes one line per node rather than one per
            # slot. When it has, the real rank count is in PBS_NP/PBS_NUM_PPN.
            if len(hosts) == nnodes:
                np_ = self._env_int("PBS_NP")
                ppn = self._env_int("PBS_NUM_PPN")
                ntasks = np_ or (nnodes * ppn if ppn else nnodes)
            else:
                ntasks = len(hosts)
            nodelist = ",".join(unique)
        else:
            nnodes = self._env_int("PBS_NUM_NODES", 1)
            ntasks = self._env_int("PBS_NP", 1)
            nodelist = ""

        info = {
            "in_pbs": in_pbs,
            "nnodes": nnodes,
            "ntasks": ntasks,
            # Rank slots on each node, i.e. the allocation's mpiprocs. Named
            # for what it is rather than for cores: the allocation asks for
            # mpiprocs and no ncpus at all, so there is no core count here to
            # report and nothing should be sized from one.
            "ranks_per_node": max(1, ntasks // max(nnodes, 1)),
            "nodelist": nodelist,
            "launcher": self.mpi_launcher,
        }
        if not in_pbs:
            info["warning"] = (
                "Not inside a PBS job (PBS_JOBID is unset). There is one node "
                "and no scheduler, so multi-node and multi-rank MPI work is "
                "unavailable -- skip any task that requires it rather than "
                "pretending to run it."
            )
        return info

    @staticmethod
    def _env_int(name: str, default: int = 0) -> int:
        """Read an integer environment variable, tolerating junk."""
        try:
            return int(os.environ.get(name, "") or default)
        except ValueError:
            return default

    @staticmethod
    def _json(payload: dict) -> str:
        return json.dumps(payload, indent=2, default=str)

    # __ Entry point ___________________________________________________________

    def run(self, transport: str = "stdio") -> None:
        """Serve. stdio for an MCP client that spawns us, sse over HTTP."""
        self.mcp.run(transport=transport)

    @classmethod
    def main(cls) -> None:
        """CLI entry point, shared by every engine server.

        Each server file ends with `if __name__ == "__main__": XServer.main()`.
        """
        import argparse

        parser = argparse.ArgumentParser(
            description=cls.DESCRIPTION or f"{cls.ENGINE} workflow MCP server",
        )
        parser.add_argument("--transport", choices=["stdio", "sse"], default="stdio")
        args = parser.parse_args()
        cls().run(transport=args.transport)