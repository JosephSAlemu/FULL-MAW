"""
ADIOS2 workflow MCP server -- BOTH variants, selected at startup.

One file, two server classes, because the choice between them is now a
RUNTIME property rather than an install-time one:

    python -c 'import adios2'                      # serial wheel from venv3
    module load adios2/<ver>-<mpi>                 # prepends its site-packages
    python -c 'import adios2'                      # the +mpi build
    module unload adios2/<ver>-<mpi>               # serial again

PYTHONPATH sits ahead of the venv's site-packages in sys.path, so a loaded
module shadows the wheel and unloading brings it back. Nothing is installed or
removed to switch. bootstrap.sh therefore keeps BOTH: the wheel in venv3 and
the +mpi build in spack_mpi/, exposed as a generated module.

    |                          | SST (serial wheel)  | MPMD (+mpi module)   |
    |--------------------------|---------------------|----------------------|
    | SST streaming            | yes, over TCP       | yes                  |
    | MPMD                     | no                  | yes                  |
    | collective parallel write| no                  | yes                  |
    | works for domains using  | any MPI             | only this env's MPI  |

WHY DISPATCH ON MAW_ADIOS AND NOT ON WHAT WE CAN IMPORT. This server process
starts BEFORE any task runs, and the `module load` happens inside each task's
own command string afterwards. So importing adios2 here reports what the
SERVER sees, which may be the serial wheel even on a run whose tasks will all
load the MPI build. The operator's declared intent is the only thing available
at startup -- and it is verified against the generated module actually being
on disk, which is checkable without loading anything.

THE PAIRING THAT MUST HOLD. With MAW_ADIOS=mpi, every task command must load
the adios2 module as well as the domain's MPI. Without it the MPMD server is
running while `import adios2` still finds the serial wheel, and the task dies
on

    RuntimeError: Cannot use MPI since ADIOS2 was built without MPI support

inside mpirun. AdiosMpmdServer's prelude loads it, and prepare_run refuses to
launch when the module is missing.

Environment
-----------
In addition to the variables in servers/server.py:

    MAW_ADIOS            serial | mpi. Default: serial.
    MAW_MPI_LAUNCHER     mpirun/mpiexec/srun. Default: "mpirun".

The MPI module itself is NOT loaded here. Which MPI a run needs is a property
of the domain's tools, stated in its skill file, and the agent writes that
`module load` into the task's own command string.
"""

import glob
import os
import shutil
import subprocess
import time

if __package__:
    from .server import Server
else:
    from server import Server


class _AdiosBase(Server):
    """What both variants share: the engine name and SST state cleanup."""

    ENGINE = "adios"

    #: Package name of the generated module for the +mpi build. Produced by
    #: `spack -e spack_mpi module lmod refresh`, with the MPI in the
    #: projection, e.g. adios2/2.13.0-openmpi-4.1.7.
    MPI_MODULE_PACKAGE = "adios2"

    def mpi_module(self) -> str | None:
        """The generated adios2 module name, or None if it was never built.

        Read off the filesystem rather than rebuilt from spack.yaml's
        `projections` template: reconstructing that string here would be a
        second implementation of it that drifts from the first.
        """
        root = os.environ.get("MAW_REPO_ROOT", self.repo_root)
        d = os.path.join(root, "spack_mpi", "modules", self.MPI_MODULE_PACKAGE)
        if not os.path.isdir(d):
            return None
        names = sorted(f[:-4] for f in os.listdir(d)
                       if f.endswith(".lua") or f.endswith(".tcl"))
        if not names:
            names = sorted(f for f in os.listdir(d)
                           if os.path.isfile(os.path.join(d, f)))
        return f"{self.MPI_MODULE_PACKAGE}/{names[-1]}" if names else None

    def module_root(self) -> str | None:
        root = os.environ.get("MAW_REPO_ROOT", self.repo_root)
        d = os.path.join(root, "spack_mpi", "modules")
        return d if os.path.isdir(d) else None

    def prepare_run(self, workflow_path: str) -> dict:
        """Remove stale SST state left by a previous run.

        `*.sst` is a DIRECTORY, not a file -- it holds data.N, md.N, md.idx,
        mmd.0 and profiling.json. An earlier version used os.remove(), which
        raises IsADirectoryError on a directory; that is an OSError, so the
        except clause swallowed it and nothing was ever actually cleared.

        WHY IT MATTERS, verified rather than assumed. A lone SST writer in this
        build does NOT block waiting for a reader -- it completes immediately
        and leaves the .sst directory behind. A later reader then reads THAT,
        with no writer running at all, and reports steps it never received
        live. So a stale directory does not hang the next run; it makes a
        broken pipeline look like a working one, which is worse.
        """
        cleared = []
        for stale in glob.glob(os.path.join(self.work_dir, "**", "*.sst"),
                               recursive=True):
            try:
                if os.path.isdir(stale):
                    shutil.rmtree(stale)
                else:
                    os.remove(stale)
                cleared.append(os.path.relpath(stale, self.work_dir))
            except OSError:
                continue
        return {"stale_sst_cleared": cleared} if cleared else {}


class AdiosSstServer(_AdiosBase):

    DESCRIPTION = (
        "MCP server for ADIOS2 workflows over SST. Write one file per pipeline "
        "stage, then run them concurrently so they stream to each other."
    )

    LAUNCH_NOTE = (
        "The file is run as `<python> workflow.py`. `ranks` is ignored -- this "
        "is a serial ADIOS2 build with no MPI. To have stages stream to each "
        "other they must be ALIVE AT THE SAME TIME, which means separate files "
        "and run_pipeline, not run_workflow."
    )

    WORKFLOW_CHECKS = (
        (
            ("import adios2", "from adios2"),
            "adios2 is never imported, so no data moves through ADIOS2. The "
            "point of this pipeline is that stages exchange arrays through it.",
        ),
        (
            (".write(", ".read(", "stream.write", "stream.read"),
            "Nothing is written to or read from an ADIOS2 stream. A producing "
            "stage opens a stream in write mode and writes; a consuming stage "
            "reads it back rather than reusing an in-memory copy.",
        ),
        (
            ("comm=",),
            "",  # placeholder, replaced by workflow_notes below
        ),
    )

    #: Substrings that mean the file is trying to use MPI. This build has none,
    #: so each is a failure the agent can fix before a run rather than an
    #: import error inside a subprocess.
    MPI_MARKERS = (
        "from mpi4py", "import mpi4py", "MPI.COMM_WORLD", "comm=", "comm =",
    )

    def launch_command(self, workflow_path: str, args: list[str], ranks: int) -> list[str]:
        """`<python> workflow.py [args...]`.

        `ranks` is deliberately ignored. There is no MPI in this build, so a
        launcher would start N independent copies of the same stage, each
        believing it is the only writer -- which is the unsynchronised race
        described in this file's header. run_pipeline is how concurrency is
        expressed here.
        """
        return [self.python, workflow_path, *args]

    def workflow_notes(self, code: str) -> list[str]:
        """WORKFLOW_CHECKS, plus the MPI warning this build needs.

        Overridden rather than declared, because the MPI check is the inverse
        of the others: it fires on something being PRESENT.
        """
        notes = [
            message for alternatives, message in self.WORKFLOW_CHECKS
            if message and not any(alt in code for alt in alternatives)
        ]
        found = [m for m in self.MPI_MARKERS if m in code]
        if found:
            notes.append(
                f"This file uses MPI ({', '.join(found)}), which this ADIOS2 "
                f"build does not have -- adios2.Stream raises 'Cannot use MPI "
                f"since ADIOS2 was built without MPI support' on a comm= "
                f"argument. Either drop the communicator and use run_pipeline "
                f"for concurrency, or switch to the adios_mpmd server."
            )
        if ".write(" in code and "shape" in code and "start" in code:
            notes.append(
                "This looks like a decomposed write (shape/start/count). With "
                "a serial build, several processes writing one global array to "
                "one .bp race on the metadata index and can exit 0 with wrong "
                "output. Write one .bp per process, or gather to one writer."
            )
        return notes

    # __ Engine-only tools ____________________________________________________

    def register_tools(self) -> None:
        """Add the three tools that only make sense for a streaming I/O engine."""
        for method in (self.run_pipeline, self.write_bp, self.read_bp):
            self.register_tool(method)

    def run_pipeline(self, stages: list, timeout: int = 7200) -> str:
        """Run several stage files CONCURRENTLY so they can stream over SST.

        Use this, not run_workflow, whenever one stage writes an SST stream
        another reads. run_workflow runs a single file to completion, so a
        writer started that way streams to nobody: in this build it does not
        even fail -- it completes, leaves the .sst directory on disk, and a
        later reader picks that up as though it had been streamed. The pipeline
        looks like it worked. Starting the stages together is what makes the
        transport actually live.

        Each stage is a dict:
          - `file`:  stage file, relative to the work directory. Required.
          - `args`:  arguments passed to it (optional)
          - `delay`: seconds to wait before starting this stage (optional).
                     Use a small delay on readers so the writer has opened the
                     stream first; SST tolerates either order, but a reader
                     that starts first spends its timeout in rendezvous.

        So this:

            [{"file": "sim.py"},
             {"file": "analysis.py", "delay": 2},
             {"file": "plot.py", "delay": 2}]

        starts three processes, waits for all of them, and reports each one's
        exit code separately.

        Every stage must select the SST engine -- with BP, a reader waits on a
        file the writer has not closed, which is the same hang by another
        route.

        Args:
            stages: stage descriptions, in start order. At least two.
            timeout: seconds before every surviving stage is killed
                     (default: 7200)

        Returns:
            JSON with run_id, overall status, and per-stage exit_code, stdout
            and stderr paths
        """
        run_id = self.new_run_id()

        problems = self._check_stages(stages)
        if problems:
            return self._json({
                "run_id": run_id,
                "status": "rejected",
                "errors": problems,
                "hint": (
                    "Each stage needs a `file` that exists in the work "
                    "directory. Two stages minimum -- with one, nothing "
                    "streams to anything, so use run_workflow instead."
                ),
            })

        extra = self.prepare_run(
            self._resolve_write_path(stages[0]["file"], must_be_relative=False)
        )

        procs: list[dict] = []
        env = self.launch_env()
        started = time.time()

        try:
            for i, stage in enumerate(stages):
                if stage.get("delay"):
                    time.sleep(float(stage["delay"]))

                path = self._resolve_write_path(stage["file"], must_be_relative=False)
                out_path = os.path.join(self.work_dir, f"{run_id}_{i}.out")
                err_path = os.path.join(self.work_dir, f"{run_id}_{i}.err")
                command = [self.python, path,
                           *[str(a) for a in stage.get("args", [])]]

                # Output to files rather than pipes. Several concurrent stages
                # on pipes deadlock as soon as one fills its buffer, and this
                # is exactly the case where they all produce output at once.
                out_f = open(out_path, "w")
                err_f = open(err_path, "w")
                procs.append({
                    "index": i,
                    "file": stage["file"],
                    "command": command,
                    "proc": subprocess.Popen(
                        command, cwd=self.work_dir, env=env,
                        stdout=out_f, stderr=err_f,
                        start_new_session=True,
                    ),
                    "handles": (out_f, err_f),
                    "stdout_file": out_path,
                    "stderr_file": err_path,
                })

            # Wait on all of them against ONE shared deadline: a per-stage
            # timeout would let a hung writer burn the full timeout and then
            # let a hung reader burn it again.
            deadline = started + timeout
            for entry in procs:
                remaining = max(1, int(deadline - time.time()))
                try:
                    entry["exit_code"] = entry["proc"].wait(timeout=remaining)
                except subprocess.TimeoutExpired:
                    self._kill_group(entry["proc"])
                    entry["exit_code"] = -1
                    entry["timed_out"] = True
        finally:
            # A stage still running when another has already failed has lost
            # its peer and will never make progress. Kill the group and close
            # the handles whatever happened.
            for entry in procs:
                if entry["proc"].poll() is None:
                    self._kill_group(entry["proc"])
                    entry.setdefault("exit_code", -1)
                    entry["killed_after_peer_exit"] = True
                for handle in entry["handles"]:
                    handle.close()

        results = []
        for entry in procs:
            results.append({
                "index": entry["index"],
                "file": entry["file"],
                "command": " ".join(entry["command"]),
                "exit_code": entry.get("exit_code", -1),
                "stdout_file": entry["stdout_file"],
                "stderr_file": entry["stderr_file"],
                "stdout_tail": self._tail_file(entry["stdout_file"]),
                "stderr_tail": self._tail_file(entry["stderr_file"]),
                **({"timed_out": True} if entry.get("timed_out") else {}),
                **({"killed_after_peer_exit": True}
                   if entry.get("killed_after_peer_exit") else {}),
            })

        failed = [r for r in results if r["exit_code"] != 0]
        payload = {
            "run_id": run_id,
            "engine": self.ENGINE,
            "status": "completed" if not failed else "failed",
            "launch_mode": f"sst_pipeline:{len(stages)}stages",
            "duration_s": round(time.time() - started, 2),
            "stages": results,
            **extra,
        }
        if failed:
            payload["hint"] = (
                "If a stage produced no output, check that every stage selects "
                "the SST engine and that the stream names match exactly. If a "
                "reader reported steps while the writer failed, it read a stale "
                ".sst directory from an earlier run rather than receiving "
                "anything live."
            )

        self.runs[run_id] = {
            "engine": self.ENGINE,
            "status": payload["status"],
            "launch_mode": payload["launch_mode"],
            "stages": [r["file"] for r in results],
            "started_at": started,
            "completed_at": time.time(),
        }
        return self._json(payload)

    def _check_stages(self, stages: list) -> list:
        """Validate a pipeline layout before starting any of it."""
        problems: list = []
        if not isinstance(stages, list) or len(stages) < 2:
            return ["`stages` must be a list of at least two stage descriptions."]
        for i, stage in enumerate(stages):
            if not isinstance(stage, dict):
                problems.append(f"Stage {i} is not an object.")
                continue
            name = stage.get("file")
            if not name:
                problems.append(f"Stage {i}: needs a `file`.")
                continue
            path = self._resolve_write_path(str(name), must_be_relative=False)
            if not os.path.isfile(path):
                problems.append(
                    f"Stage {i}: no file at {path}. Write it with write_file first."
                )
        return problems

    def write_bp(self, name: str, bp_path: str, python_code: str,
                 timeout: int = 1800) -> str:
        """Write to a BP file through a Stream the SERVER opens.

        `python_code` runs with `stream` already bound to an open
        adios2.Stream(bp_path, "w"). Call stream.write(var, data, shape, start,
        count) and stream.write_attribute(...) on it directly. Do NOT import
        adios2 or open your own Stream -- the server did both, and opening the
        same path twice corrupts it.

        Use this for a one-shot write of a result. For a stage that streams to
        another stage, write a stage FILE and use run_pipeline: this tool runs
        to completion before returning, so nothing can be reading while it
        writes.

        Args:
            name: descriptive name for this write
            bp_path: output .bp path (/app/ aliases are resolved)
            python_code: code that calls stream.write(...)
            timeout: seconds before the write is killed (default: 1800)

        Returns:
            JSON with status, bp_path, exit_code, stdout, stderr, and whether
            the code actually called stream.write
        """
        return self._stream_task("w", name, bp_path, python_code, timeout)

    def read_bp(self, name: str, bp_path: str, python_code: str,
                timeout: int = 1800) -> str:
        """Read a BP file through a Stream the SERVER opens.

        `python_code` runs with `stream` already bound to an open
        adios2.Stream(bp_path, "r"). Iterate `for _ in stream.steps():` and
        call stream.read(var) / stream.read_attribute(...) on it. Do NOT import
        adios2 or open your own Stream.

        Args:
            name: descriptive name for this read
            bp_path: input .bp path (/app/ aliases are resolved)
            python_code: code that calls stream.read(...)
            timeout: seconds before the read is killed (default: 1800)

        Returns:
            JSON with status, bp_path, exit_code, stdout, stderr, and whether
            the code actually called stream.read
        """
        return self._stream_task("r", name, bp_path, python_code, timeout)

    #: Calls that mean the submitted code used the Stream it was handed. The
    #: server opened it either way, so a call that never touches it produces an
    #: empty .bp and exits 0 -- worth reporting rather than discovering later.
    STREAM_USAGE = (".write(", ".read(", ".write_attribute(", ".read_attribute(")

    def _stream_task(self, mode: str, name: str, bp_path: str,
                     python_code: str, timeout: int) -> str:
        """Shared body of write_bp and read_bp."""
        run_id = self.new_run_id()
        resolved_bp = self.resolve_aliases(bp_path)
        code = self.resolve_aliases(python_code)

        used_stream = any(marker in python_code for marker in self.STREAM_USAGE)
        opened_own = "adios2.Stream(" in python_code or "import adios2" in python_code

        if opened_own:
            return self._json({
                "run_id": run_id,
                "status": "rejected",
                "engine": self.ENGINE,
                "error": (
                    "This code imports adios2 or opens its own Stream. The "
                    "server already opened one and bound it to `stream`; "
                    "opening the same path twice corrupts the file."
                ),
                "hint": "Use the `stream` object directly, or use write_file "
                        "plus run_pipeline if you need to own the Stream.",
            })

        script_dir = os.path.join(self.work_dir, "_bp_tasks")
        os.makedirs(script_dir, exist_ok=True)
        script_path = os.path.join(script_dir, f"{run_id}_{mode}.py")

        # Written to a file and kept, not passed with -c: the generated wrapper
        # is then inspectable after the run, which is the only way to see what
        # actually executed when the submitted code is wrong.
        with open(script_path, "w") as f:
            f.write(self._stream_wrapper(code, resolved_bp, mode))

        result = self._run([self.python, script_path], timeout=timeout)

        payload = {
            "run_id": run_id,
            "name": name,
            "engine": self.ENGINE,
            "status": "completed" if result.get("exit_code") == 0 else "failed",
            "mode": "write" if mode == "w" else "read",
            "bp_path": resolved_bp,
            "script": script_path,
            "exit_code": result.get("exit_code"),
            "stdout": (result.get("stdout") or "")[:3000],
            "stderr": (result.get("stderr") or "")[:3000],
            "stream_used": used_stream,
        }
        if not used_stream:
            payload["warning"] = (
                f"This code never called stream.{'write' if mode == 'w' else 'read'}"
                f"(...), so the server opened a Stream it did no I/O on. For a "
                f"write that means an empty .bp that exits 0."
            )
        return self._json(payload)

    @staticmethod
    def _stream_wrapper(code: str, bp_path: str, mode: str) -> str:
        """Wrap submitted code in a server-opened Stream context."""
        body = "\n".join("        " + line for line in code.splitlines()) or "        pass"
        return (
            "import sys, traceback\n"
            "import adios2\n"
            "\n"
            "try:\n"
            f"    with adios2.Stream({bp_path!r}, {mode!r}) as stream:\n"
            f"{body}\n"
            "except Exception as exc:\n"
            "    traceback.print_exc(file=sys.stderr)\n"
            "    sys.exit(1)\n"
        )

    def resource_info(self) -> dict:
        """State plainly that this build has no MPI, before a run proves it."""
        return {
            "adios2_build": "serial (PyPI wheel)",
            "mpi": "none -- this build has no MPI; `ranks` is ignored",
            "note": (
                "Stages stream over SST/TCP and must run CONCURRENTLY, which "
                "means separate files passed to run_pipeline. A collective "
                "parallel write is NOT possible here: write one .bp per "
                "process or gather to one writer. For MPMD or a collective "
                "write, the operator must rebuild with MAW_ADIOS=mpi and the "
                "adios_mpmd server."
            ),
        }


class AdiosMpmdServer(_AdiosBase):

    def prepare_run(self, workflow_path: str) -> dict:
        """Clear stale SST state, then refuse if the +mpi build is unreachable.

        THE PAIRING CHECK. This server is running because MAW_ADIOS=mpi, which
        asserts that tasks will load the generated adios2 module. If that
        module was never built, every task gets the serial wheel from venv3
        instead and dies on

            RuntimeError: Cannot use MPI since ADIOS2 was built without MPI
            support

        inside mpirun, after the launch. Checked here because it is checkable
        without loading anything, and because the alternative is discovering
        it one allocation later.
        """
        extra = super().prepare_run(workflow_path)

        module = self.mpi_module()
        if module is None:
            raise RuntimeError(
                "MAW_ADIOS=mpi but no generated adios2 module exists under "
                f"{self.module_root() or 'spack_mpi/modules'}. Tasks would get "
                "the serial wheel from venv3 and raise 'Cannot use MPI since "
                "ADIOS2 was built without MPI support' inside mpirun.\n"
                "  Build it:\n"
                "    module load <the MPI your domains use>\n"
                "    cd spack_mpi && cp spack.yaml.in spack.yaml\n"
                "    # uncomment the adios2 +mpi +python +sst spec\n"
                "    spack -e . external find openmpi mpich intel-oneapi-mpi\n"
                "    spack -e . install && spack -e . module lmod refresh -y\n"
                "  Or set MAW_ADIOS=serial and use the SST server instead."
            )
        extra["adios2_module"] = module
        return extra

    DESCRIPTION = (
        "MCP server for ADIOS2 MPMD workflows. Write one file per stage, then "
        "launch them as rank blocks of a single MPI job so they stream live."
    )

    LAUNCH_NOTE = (
        "run_workflow runs ONE file, optionally as `mpirun -n <ranks> <python> "
        "file` -- every rank runs the same program. For stages that must be "
        "alive at the same time, and for an external binary as one of the "
        "roles, use run_mpmd with one file per stage."
    )

    WORKFLOW_CHECKS = (
        (
            ("import adios2", "from adios2"),
            "adios2 is never imported, so no data moves through ADIOS2.",
        ),
        (
            (".write(", ".read(", ".put(", ".get("),
            "Nothing is written to or read from an ADIOS2 stream.",
        ),
        (
            ("from mpi4py", "import mpi4py"),
            "mpi4py is not imported. This is the MPI build, and an MPMD stage "
            "must split MPI.COMM_WORLD and pass the result to ADIOS2.",
        ),
        (
            ("Split(", "Create(", "comm="),
            "No communicator is split and none is passed to ADIOS2. Every "
            "block of an MPMD launch shares one MPI_COMM_WORLD, so a stage "
            "that passes COMM_WORLD to ADIOS2 is writing on behalf of every "
            "other stage's ranks too.",
        ),
    )

    #: Exported to each stage block so the stage does not have to invent a
    #: color. Two stages with the same color merge communicators silently.
    COLOR_VAR = "ADIOS2_STAGE_COLOR"

    def launch_command(self, workflow_path: str, args: list[str], ranks: int) -> list[str]:
        """`<python> file`, or `mpirun -n <ranks> <python> file` when asked.

        One launcher, one level: nothing wraps the launcher and the launcher
        starts the interpreter directly. With ranks, every rank runs this same
        file, so it must still split COMM_WORLD by role.
        """
        if ranks and ranks > 0:
            return [self.mpi_launcher, "-n", str(ranks),
                    self.python, workflow_path, *args]
        return [self.python, workflow_path, *args]

    # __ Engine-only tools ____________________________________________________

    def register_tools(self) -> None:
        """Add run_mpmd and write_stage, which exist for this engine alone."""
        for method in (self.run_mpmd, self.write_stage):
            self.register_tool(method)

    def write_stage(self, stage: str, content: str, overwrite: bool = True) -> str:
        """Write one stage file, as `stage_<name>.py` in the work directory.

        A convenience over write_file that enforces the naming run_mpmd expects
        and applies this engine's structural checks to each stage separately --
        so a stage that forgot to split its communicator is reported as that
        stage, not as a line number in a combined file.

        Args:
            stage: stage name, e.g. "sim", "analysis", "render". Becomes
                   stage_<name>.py
            content: the stage's full Python source
            overwrite: replace an existing file (default: true)

        Returns:
            JSON with the path written and any structural warnings
        """
        safe = "".join(c for c in stage if c.isalnum() or c in "_-")
        if not safe:
            return self._json({
                "status": "rejected",
                "error": f"stage name {stage!r} has no usable characters.",
            })
        return self.write_file(f"stage_{safe}.py", content, overwrite=overwrite)

    def run_mpmd(self, blocks: list, timeout: int = 7200) -> str:
        """Run the pipeline as ONE mpirun with a rank block per program.

        This is how stages stream to each other live: every block is part of a
        single MPI job, started together, sharing one MPI_COMM_WORLD. It is
        also the only way an external compiled binary can be one of the roles.

        Each block is a dict with `ranks` plus either:
          - `file`: a stage file in the work directory, run as
            `<python> stage_x.py`. Write it with write_stage.
          - `exe`: an external program to launch directly.
        plus optional `args`.

        So this:

            [{"ranks": 8, "exe": "/path/sim", "args": ["in.params"]},
             {"ranks": 4, "file": "stage_analysis.py"},
             {"ranks": 1, "file": "stage_render.py"}]

        launches:

            mpirun -n 8 /path/sim in.params \\
                 : -n 4 <python> stage_analysis.py \\
                 : -n 1 <python> stage_render.py

        THE SERVER ASSIGNS THE COMMUNICATOR COLORS. Each block gets
        ADIOS2_STAGE_COLOR exported as its index + 1, and each stage must read
        it and split MPI.COMM_WORLD on it. Two stages sharing a color merge
        into one communicator and the writer's rank count then includes the
        reader's ranks.

        Select the SST engine in every stage. With BP, a consumer reads a
        file the producer has not finished writing -- which does not error, it
        just reads whatever is there.

        Args:
            blocks: rank blocks, in launch order. At least two.
            timeout: seconds before the run is killed (default: 7200)

        Returns:
            JSON with run_id, status, exit_code, the exact command, stdout,
            stderr, and the color assigned to each block
        """
        run_id = self.new_run_id()

        problems = self._check_blocks(blocks)
        if problems:
            return self._json({
                "run_id": run_id,
                "status": "rejected",
                "errors": problems,
                "hint": (
                    "Each block needs an integer `ranks` of at least 1 and "
                    "either `file` (written with write_stage) or `exe`. Two "
                    "blocks minimum -- with one, nothing streams to anything, "
                    "so use run_workflow instead."
                ),
            })

        extra = self.prepare_run(self.work_dir)

        # mpirun exports one environment to all blocks, so a per-block color
        # cannot be an `-x VAR=value` -- that would be the same value
        # everywhere. Each block is therefore wrapped in `env VAR=n`, which is
        # per-argv and launcher-independent: `-x` is Open MPI's spelling,
        # `--env` is Intel's, and srun uses neither.
        # EVERY BLOCK must have the +mpi adios2 on its PYTHONPATH, or `import
        # adios2` finds the serial wheel from venv3 and raises on comm=. The
        # module is loaded once, in front of the launcher, so all blocks
        # inherit it -- unlike the per-block colour below, which cannot be
        # shared.
        #
        # `module use` first: the generated tree lives under spack_mpi/, not on
        # the site MODULEPATH. maw.env prepends it at activation, but a task
        # command that ran `module purge` has dropped it again.
        prelude = []
        module = self.mpi_module()
        if module:
            root = self.module_root()
            if root:
                prelude.append(f"module use {root}")
            prelude.append(f"module load {module}")

        command = [self.mpi_launcher]
        colors = {}
        for i, block in enumerate(blocks):
            if i:
                command.append(":")
            color = i + 1
            command += ["-n", str(block["ranks"])]
            command += ["env", f"{self.COLOR_VAR}={color}"]

            if block.get("exe"):
                target = str(block["exe"])
                command.append(target)
                colors[target] = color
            else:
                path = self._resolve_write_path(str(block["file"]),
                                                must_be_relative=False)
                command += [self.python, path]
                colors[str(block["file"])] = color

            command += [str(a) for a in block.get("args", [])]

        # The module load is shell, the launcher is argv. Joined through
        # `bash -lc` so both survive -- launch_and_record takes a list.
        if prelude:
            command = ["bash", "-lc",
                       " && ".join(prelude) + " && " + " ".join(command)]

        return self.launch_and_record(run_id, command, self.work_dir, timeout, {
            "launch_mode": f"mpmd:{sum(b['ranks'] for b in blocks)}:{len(blocks)}blocks",
            "blocks": blocks,
            "stage_colors": colors,
            "color_var": self.COLOR_VAR,
            "adios2_module": module,
            **extra,
        })

    def _check_blocks(self, blocks: list) -> list:
        """Validate an MPMD layout before building a command line from it."""
        problems: list = []
        if not isinstance(blocks, list) or len(blocks) < 2:
            return ["`blocks` must be a list of at least two rank blocks."]

        for i, block in enumerate(blocks):
            if not isinstance(block, dict):
                problems.append(f"Block {i} is not an object.")
                continue

            ranks = block.get("ranks")
            if not isinstance(ranks, int) or ranks < 1:
                problems.append(f"Block {i}: `ranks` must be an integer >= 1.")

            if block.get("exe"):
                continue
            name = block.get("file")
            if not name:
                problems.append(f"Block {i}: needs either `file` or `exe`.")
                continue

            # Checked here rather than left to mpirun: a missing program makes
            # mpirun abort the WHOLE job with a message naming only the first
            # failure, so the other blocks' problems stay invisible.
            path = self._resolve_write_path(str(name), must_be_relative=False)
            if not os.path.isfile(path):
                problems.append(
                    f"Block {i}: no stage file at {path}. Write it with "
                    f"write_stage(stage=..., content=...) first."
                )
                continue

            with open(path) as f:
                code = f.read()
            if self.COLOR_VAR not in code:
                problems.append(
                    f"Block {i} ({name}): does not read {self.COLOR_VAR}. The "
                    f"server assigns each block a communicator color in that "
                    f"variable, and a stage that ignores it either hardcodes a "
                    f"color that may collide or passes COMM_WORLD to ADIOS2."
                )
        return problems

    def resource_info(self) -> dict:
        """Report the build and the one constraint it imposes."""
        return {
            "adios2_build": "MPI (+mpi +python +sst)",
            "launcher": self.mpi_launcher,
            "color_var": self.COLOR_VAR,
            "note": (
                "Stages are separate files launched as rank blocks of one "
                "mpirun by run_mpmd. Every block shares one MPI_COMM_WORLD and "
                "must split it on the color the server exports in "
                f"{self.COLOR_VAR}. This build is bound to ONE MPI: a domain "
                "whose tools use a different one cannot use this engine, and a "
                "binary needing the MPI C++ bindings cannot be a block at all "
                "if this was built against Open MPI 5. Confirm adios2 and "
                "mpi4py import with check_package first."
            ),
        }


#: MAW_ADIOS -> server class. See this file's header for why the choice is the
#: operator's declared intent rather than an import probe.
VARIANTS = {"serial": AdiosSstServer, "mpi": AdiosMpmdServer}


def main() -> None:
    """Start the variant MAW_ADIOS names. Default and fallback: serial.

    mcp_explorer normally exports the canonical word already; this is the
    backstop for a server started by hand. The warning goes to stderr because
    stdout is the JSON-RPC channel.
    """
    import sys

    raw = os.environ.get("MAW_ADIOS", "")
    choice = (raw or "").strip().lower()
    if choice not in VARIANTS:
        print(f"[adios] MAW_ADIOS={raw!r} is not 'serial' or 'mpi'; "
              f"starting the serial server.", file=sys.stderr)
        choice = "serial"
    VARIANTS[choice].main()


if __name__ == "__main__":
    main()