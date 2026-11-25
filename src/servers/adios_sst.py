"""
ADIOS2 workflow MCP server -- SST variant, for the SERIAL build.

This is the server for `MAW_ADIOS=serial`: the `adios2` PyPI wheel, which is a
serial build -- nothing matching libmpi in its DT_NEEDED -- that nonetheless
reports ADIOS2_HAVE_SST TRUE and ships EVPath, FFS, atl and dill. Streaming
between pipeline stages therefore works over TCP with no MPI in the picture,
and a task may load whatever MPI its domain names without putting a second one
in the process.

Pick the other variant, src/servers/adios_mpmd.py, when the workflow needs MPMD or
a collective parallel write. See the table in that file's header.

What is specific to this engine:

  - run_pipeline. SST only streams while writer and reader are alive AT THE SAME
    TIME, and this server has no MPI to launch them with. So stages are separate
    FILES, started as concurrent OS processes and waited on together. One
    process per stage, no launcher, no ranks.
  - write_bp / read_bp. The Stream is opened by the server and handed to the
    submitted code already open, so a call that does no I/O is visible as such
    rather than silently producing an empty file.
  - clearing stale SST state before a run. A `.sst` is a DIRECTORY holding
    the stream's data and metadata. Verified in this build: a lone writer does
    not block waiting for a reader -- it completes and leaves that directory
    behind, and a later reader reads it with no writer running, reporting steps
    it never received live. Stale state makes a broken pipeline look like a
    working one.

WHAT THIS BUILD CANNOT DO, and it is not nothing: a serial ADIOS2 cannot do a
collective parallel write. N processes writing one global array to one .bp race
on the metadata index -- it exits 0 and can produce plausible-but-wrong output
rather than failing. Two patterns are safe and both are checked for here: one
.bp file per writer, or gather to one writer. A workflow that needs a single
collectively-written global array needs adios_mpmd.py.

Environment
-----------
Only what src/servers/server.py already reads. This engine adds nothing: no
launcher, no MPI. check_package("adios2") is how the agent confirms the
bindings import.
"""

import glob
import json
import os
import subprocess
import time
from typing import Optional

if __package__:
    from .server import Server
else:
    from server import Server


class AdiosSstServer(Server):

    ENGINE = "adios"
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

    # __ Engine specifics ______________________________________________________

    def prepare_run(self, workflow_path: str) -> dict:
        """Remove stale SST state left by a previous run.

        `*.sst` is a DIRECTORY, not a file -- it holds data.N, md.N, md.idx,
        mmd.0 and profiling.json. The original version of this used
        os.remove(), which raises IsADirectoryError on a directory; that is an
        OSError, so the except clause swallowed it and nothing was ever
        actually cleared. shutil.rmtree is what removes one.

        WHY IT MATTERS, verified rather than assumed. A lone SST writer in this
        build does NOT block waiting for a reader -- it completes immediately
        and leaves the .sst directory behind. A later reader then reads THAT,
        with no writer running at all, and reports steps it never received
        live. So a stale directory does not hang the next run; it makes a
        broken pipeline look like a working one, which is worse.
        """
        import shutil

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


if __name__ == "__main__":
    AdiosSstServer.main()
