from fastmcp import FastMCP
import glob as _glob
import sysconfig, os, sys, importlib.util


class server():
    def __init__(self, engine, instruct):
        self.mcp = FastMCP(
            f"{engine} Workflow Engine",
            instructions=instruct,
        )

    def _find_lammps_pkg_dir(self) -> str:
        """Locate the installed `lammps` package dir (which ships the bundled `lmp`
        binary) using Python's own import machinery. Returns "" if not found, so
        PATH construction stays harmless.
        """
        try:
            spec = importlib.util.find_spec("lammps")
        except (ImportError, ValueError):
            return ""

        if spec is None or not spec.submodule_search_locations:
            return ""

        path = spec.submodule_search_locations[0]
        return path if os.path.isdir(path) else ""

    