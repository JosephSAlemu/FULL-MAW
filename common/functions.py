"""
File of common functions used by multiple file
"""
import os


def find_lammps_pkg_dir(venv_python: str, repo_root: str) -> str:
    """Locate the installed `lammps` package dir, which ships the bundled `lmp`.

    Tries the site-packages of `venv_python`, then this interpreter's own, then
    a glob under `repo_root`/venv*. Returns "" when not found, so the PATH
    built from it stays harmless. Avoids hardcoding a Python minor version.

    Shared by all three servers in servers/, which each used to carry a
    byte-identical private copy.
    """
    import glob
    import sysconfig

    candidates: list[str] = []
    venv_bin = os.path.dirname(venv_python)
    venv_root = os.path.dirname(venv_bin) if venv_bin else ""
    if venv_root:
        candidates += glob.glob(
            os.path.join(venv_root, "lib", "python*", "site-packages", "lammps")
        )
        candidates.append(os.path.join(venv_root, "Lib", "site-packages", "lammps"))
    try:
        purelib = sysconfig.get_paths().get("purelib", "")
        if purelib:
            candidates.append(os.path.join(purelib, "lammps"))
    except Exception:
        pass
    candidates += glob.glob(
        os.path.join(repo_root, "venv*", "lib", "python*", "site-packages", "lammps")
    )

    for path in candidates:
        if path and os.path.isdir(path):
            return path
    return ""