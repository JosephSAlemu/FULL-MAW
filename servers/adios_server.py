"""
ADIOS2 workflow MCP server.

Everything the explorer calls is inherited from Server. What is specific to
ADIOS2 is:

  - the launch command. ADIOS2 is an I/O library, not a scheduler: there is no
    runtime to start and nothing to submit to. The workflow file is a staged
    pipeline that moves data between its stages through ADIOS2, and running it
    means running the interpreter on it -- under `mpirun -n <ranks>` when the
    pipeline asks for ranks, so producer and consumer stages can stream to each
    other concurrently.
  - clearing stale SST rendezvous files before a run. SST leaves a .sst file
    next to the stream; one left behind by a killed run makes the next open
    block forever waiting on a peer that no longer exists, so the run dies on
    its timeout instead of failing fast.

Environment
-----------
In addition to the variables in servers/server.py:

    MAW_MPI_LAUNCHER   used when run_workflow is called with ranks > 0.
                       Default: "mpirun".

The ADIOS2 bindings themselves come from the environment the operator built;
nothing here looks for them. check_package("adios2") is how the agent confirms
they are importable, and check_package("mpi4py") whether ranks are usable.
"""

import glob
import os

if __package__:
    from .server import Server
else:
    from server import Server


class AdiosServer(Server):

    ENGINE = "adios"
    DESCRIPTION = (
        "MCP server for ADIOS2 workflows. Write one staged pipeline file whose "
        "stages pass data through ADIOS2, then run it."
    )

    LAUNCH_NOTE = (
        "The file is run as `<python> workflow.py`, or as "
        "`mpirun -n <ranks> <python> workflow.py` when ranks > 0. With ranks, "
        "every rank runs the same file, so it must split MPI.COMM_WORLD by role "
        "and pass the split communicator to ADIOS2."
    )

    WORKFLOW_CHECKS = (
        (
            ("import adios2", "from adios2"),
            "adios2 is never imported, so no data moves through ADIOS2. The "
            "point of this pipeline is that stages exchange arrays through it.",
        ),
        (
            (".write(", ".put("),
            "Nothing is written to an ADIOS2 stream. A producing stage should "
            "open a stream in write mode and write the array.",
        ),
        (
            (".read(", ".get("),
            "Nothing is read back from an ADIOS2 stream. A consuming stage has "
            "to read the array back rather than reuse the in-memory copy.",
        ),
        (
            ("def main(", "--stage"),
            "No main() and no --stage dispatch. Give the pipeline an entry "
            "point that calls the stages in order.",
        ),
    )

    def launch_command(self, workflow_path: str, args: list[str], ranks: int) -> list[str]:
        """`<python> file`, or `mpirun -n <ranks> <python> file` when asked.

        One launcher, one level. Nothing wraps the launcher and the launcher
        starts the interpreter directly.
        """
        if ranks and ranks > 0:
            return [self.mpi_launcher, "-n", str(ranks),
                    self.python, workflow_path, *args]
        return [self.python, workflow_path, *args]

    # __ Engine-only tool _____________________________________________________

    def register_tools(self) -> None:
        """Add run_mpmd, which only makes sense for this engine."""
        self.mcp.tool(self.run_mpmd)

    def run_mpmd(self, blocks: list, filename: str = "workflow.py",
                 timeout: int = 7200) -> str:
        """Run the pipeline as ONE mpirun with a rank block per program.

        Use this, instead of run_workflow, when a compiled MPI binary and the
        Python stages must be alive AT THE SAME TIME so SST can stream between
        them live. run_workflow with `ranks` launches N copies of one program;
        this launches several different programs as rank blocks of a single job,
        which is the only way an external binary can be one of the roles.

        Each block is a dict with `ranks` plus either:
          - `exe`: a program to launch, with optional `args`
          - `stage`: the name of a stage function in the workflow file, which is
            re-entered as `<python> workflow.py --stage <name>`

        So this:

            [{"ranks": 8, "exe": "/path/sim", "args": ["in.params"]},
             {"ranks": 4, "stage": "analyze"},
             {"ranks": 1, "stage": "render"}]

        launches:

            mpirun -n 8 /path/sim in.params \
                 : -n 4 <python> workflow.py --stage analyze \
                 : -n 1 <python> workflow.py --stage render

        Because the file is re-entered once per stage block, it must read
        --stage and dispatch to the named function. Select the SST engine, or
        the consumer blocks wait on a BP file the producer has not closed.

        Args:
            blocks: rank blocks, in launch order. At least two.
            filename: workflow file the `stage` blocks re-enter
                      (default: workflow.py)
            timeout: seconds before the run is killed (default: 7200)

        Returns:
            JSON with run_id, status, exit_code, the exact command, stdout, stderr
        """
        run_id = self.new_run_id()
        path = self._resolve_write_path(filename, must_be_relative=False)

        if not os.path.isfile(path):
            return self._json({
                "run_id": run_id,
                "status": "failed",
                "error": f"No workflow file at {path}.",
                "hint": f"Call write_file(path='{filename}', content=...) first.",
            })

        problems = self._check_blocks(blocks)
        if problems:
            return self._json({
                "run_id": run_id,
                "status": "rejected",
                "errors": problems,
                "hint": (
                    "Each block needs an integer `ranks` of at least 1 and "
                    "either `exe` or `stage`. Two blocks minimum -- with one, "
                    "nothing streams to anything, so use run_workflow instead."
                ),
            })

        extra = self.prepare_run(path)
        command = [self.mpi_launcher]
        for i, block in enumerate(blocks):
            if i:
                command.append(":")
            command += ["-n", str(block["ranks"])]
            if block.get("exe"):
                command.append(str(block["exe"]))
            else:
                command += [self.python, path, "--stage", str(block["stage"])]
            command += [str(a) for a in block.get("args", [])]

        return self.launch_and_record(run_id, command, path, timeout, {
            "launch_mode": f"mpmd:{sum(b['ranks'] for b in blocks)}:{len(blocks)}blocks",
            "blocks": blocks,
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
            if not (block.get("exe") or block.get("stage")):
                problems.append(f"Block {i}: needs either `exe` or `stage`.")
        return problems

    # __ Engine specifics ______________________________________________________

    def prepare_run(self, workflow_path: str) -> dict:
        """Remove stale SST rendezvous files left by a previous run."""
        cleared = []
        for stale in glob.glob(os.path.join(self.work_dir, "**", "*.sst"),
                               recursive=True):
            try:
                os.remove(stale)
                cleared.append(os.path.relpath(stale, self.work_dir))
            except OSError:
                continue
        return {"stale_sst_cleared": cleared} if cleared else {}

    def resource_info(self) -> dict:
        """Say plainly that ranks come from run_workflow here, not from the file."""
        return {
            "note": (
                "ADIOS2 does not schedule anything. For Python stages that must "
                "run concurrently, pass ranks to run_workflow and split "
                "MPI.COMM_WORLD by role inside the file. When an external "
                "compiled binary has to be one of those concurrent roles, use "
                "run_mpmd instead. Confirm mpi4py and adios2 import with "
                "check_package first."
            ),
        }


if __name__ == "__main__":
    AdiosServer.main()