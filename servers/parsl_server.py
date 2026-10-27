"""
Parsl workflow MCP server.

Everything the explorer calls is inherited from Server. What is specific to
Parsl is one method -- how a workflow file is launched -- and a few structural
expectations about what that file should contain.

A Parsl workflow is self-contained: the file builds its own Config, calls
parsl.load(), and the DataFlowKernel inside it schedules every @bash_app and
@python_app. So launching it is launching the interpreter on it, and there is
nothing for this server to configure. That is why this file is short.

Environment: see servers/server.py. This engine reads nothing of its own --
MAW_PYTHON is the interpreter and parsl comes from whatever environment the
operator built around it.
"""

# Spawned as `python servers/parsl_server.py`, which puts servers/ on sys.path,
# but also importable as servers.parsl_server. Both are supported.
if __package__:
    from .server import Server
else:
    from server import Server


class ParslServer(Server):

    ENGINE = "parsl"
    DESCRIPTION = (
        "MCP server for the Parsl workflow engine. Write one self-contained "
        "Parsl driver file, then run it."
    )

    LAUNCH_NOTE = (
        "The file is run as `<python> workflow.py`. The Parsl runtime it loads "
        "schedules the apps, so `ranks` is not used here -- rank counts belong "
        "in the mpirun string returned by a @bash_app."
    )

    WORKFLOW_CHECKS = (
        (
            ("@bash_app", "@python_app", "@join_app"),
            "No Parsl app found. Every unit of work should be a decorated "
            "function: @bash_app for a command line (the body returns the "
            "command string), @python_app for Python work.",
        ),
        (
            ("parsl.load", "parsl.load(",),
            "The file never calls parsl.load(config). It has to build and load "
            "its own Config, or no app is ever scheduled.",
        ),
        (
            ("mpirun", "mpiexec", "srun", "@python_app"),
            "No launcher and no @python_app -- check the workflow actually "
            "does something.",
        ),
    )

    def launch_command(self, workflow_path: str, args: list[str], ranks: int) -> list[str]:
        """`<python> workflow.py [args...]`.

        `ranks` is deliberately ignored: Parsl's executors and the mpirun string
        inside a @bash_app decide how wide the work runs, and wrapping the
        driver itself in a launcher would start N copies of the scheduler.
        """
        return [self.python, workflow_path, *args]


if __name__ == "__main__":
    ParslServer.main()