"""
ADIOS2 workflow MCP server -- MPMD variant, for the MPI build.

This is the server for `MAW_ADIOS=mpi`: an ADIOS2 built with +mpi +python +sst
against the same MPI as the rest of the engines environment. It can do what the
serial wheel cannot -- MPMD, and collective parallel writes -- at the cost of
being bound to that one MPI.

    |                          | adios_sst.py        | adios_mpmd.py (here) |
    |--------------------------|---------------------|----------------------|
    | ADIOS2 source            | PyPI wheel, serial  | built, +mpi          |
    | SST streaming            | yes, over TCP       | yes                  |
    | MPMD                     | no                  | yes                  |
    | collective parallel write| no                  | yes                   |
    | works for domains using  | any MPI             | only this env's MPI  |

WHY STAGES ARE SEPARATE FILES HERE. An MPMD launch starts a DIFFERENT program
per rank block. Re-entering one file with `--stage` works, but it means every
block loads every stage's imports and a syntax error in the renderer takes down
the simulation; it also makes the file the only place a block's rank count and
its code can disagree. One file per stage keeps each block's program exactly
what that block runs, which is also what lets an external compiled binary be
one of the blocks without any wrapper.

THE ONE RULE EVERY STAGE MUST FOLLOW. All blocks in an MPMD launch share one
MPI_COMM_WORLD. So each stage splits it on a color unique to that stage and
passes the SPLIT communicator to ADIOS2 -- never MPI.COMM_WORLD:

    from mpi4py import MPI
    import adios2
    color = int(os.environ["ADIOS2_STAGE_COLOR"])     # set per block by this server
    app = MPI.COMM_WORLD.Split(color, MPI.COMM_WORLD.Get_rank())
    with adios2.Stream("gs.bp", "w", comm=app) as s:
        ...

Two stages that share a color merge into one communicator, and the writer's
rank count then silently includes the reader's ranks -- a wrong global shape, or
a hang at Open. The server assigns the colors and exports them, so no stage has
to pick one.

Environment
-----------
In addition to the variables in src/servers/server.py:

    MAW_MPI_LAUNCHER   mpirun/mpiexec/srun. Default: "mpirun".

The MPI module itself is NOT loaded here. Which MPI a run needs is a property
of the domain's tools, stated in its skill file, and the agent writes that
`module load` into the task's own command string. A module loaded in this
server would reach every task it ever launches.
"""

import glob
import os
import shlex

if __package__:
    from .server import Server
else:
    from server import Server


class AdiosMpmdServer(Server):

    ENGINE = "adios"
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

        return self.launch_and_record(run_id, command, self.work_dir, timeout, {
            "launch_mode": f"mpmd:{sum(b['ranks'] for b in blocks)}:{len(blocks)}blocks",
            "blocks": blocks,
            "stage_colors": colors,
            "color_var": self.COLOR_VAR,
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


if __name__ == "__main__":
    AdiosMpmdServer.main()
