import os

# Package, NOT from `spack.package import *`. spack/package.py carries a
# per-API-version table of the names a recipe gets, and `Package` is listed
# under v2.2 while this repo declares api: v2.0 -- so the star import does not
# provide it here. Everything else below (Executable, mkdirp, join_path,
# install_tree, InstallError, tty, run_after, conflicts, variant, version) is a
# v2.0 name and does come from the star import.
#
# Importing it explicitly also keeps this file working if repo.yaml's api
# version changes in either direction.
from spack_repo.builtin.build_systems.generic import Package # type: ignore

from spack.package import * # type: ignore


class Compss(Package):
    """COMP Superscalar (COMPSs), installed from the release tarball.

    COMPSs is not pip-installable: it is Java plus C++ middleware with its
    own ./install script, and its Python binding lands under
    Bindings/python/3 rather than in site-packages. The previous setup put
    it in ~/.local/COMPSs by hand and reached it through PYTHONPATH and
    LD_LIBRARY_PATH; this package does the same install into a Spack prefix
    so that activation, not a shell script, wires it up.

    Named maw.compss rather than compss so it cannot be confused with a
    builtin package of that name, and so `spack install maw.compss` is
    unambiguous about which recipe ran.
    """

    homepage = "https://compss.bsc.es/"
    url = "https://compss.bsc.es/repo/sc/stable/COMPSs_3.3.3.tar.gz"

    version("3.3.3", sha256="7b0e52e275b3890aa59f5b5ae40e13b49701269c8d1b2ff76c0aed27d30d6239") # type: ignore

    variant("pycompss", default=True, description="Build the Python binding") # type: ignore
    variant("c_binding", default=False, description="Build the C/C++ binding") # type: ignore
    variant("tracing", default=False, description="Build Extrae tracing support") # type: ignore

    # Build-time only: the install script shells out to these.
    depends_on("openjdk@11:", type=("build", "run")) # type: ignore
    depends_on("boost", type=("build", "run")) # type: ignore
    depends_on("libxml2", type=("build", "run")) # type: ignore
    # libiconv explicitly, because setup_build_environment reads
    # spec["libiconv"].prefix.lib. That resolves today only because libxml2
    # happens to pull it in, and spec[...] searches the whole DAG -- so a
    # libxml2 built without iconv turns that line into KeyError: libiconv at
    # build time, which reads like a Spack bug rather than a missing
    # depends_on.
    depends_on("libiconv", type=("build", "run")) # type: ignore
    depends_on("papi", type=("build", "run")) # type: ignore
    depends_on("graphviz", type="run") # type: ignore
    depends_on("python@3.6:", type=("build", "run"), when="+pycompss") # type: ignore
    depends_on("py-pip", type="build", when="+pycompss") # type: ignore
    depends_on("py-setuptools", type="build", when="+pycompss") # type: ignore
    depends_on("autoconf", type="build") # type: ignore
    depends_on("automake", type="build") # type: ignore
    depends_on("libtool", type="build") # type: ignore
    depends_on("pkgconf", type="build") # type: ignore

    # Deliberately no MPI dependency. COMPSs can be built with an MPI adaptor
    # and is not here: src/servers/pycompss_server.py runs `python -m pycompss`
    # with no MPI, and adding one would put libmpi in the view and break the
    # MPI-free invariant in spack.yaml at the repo root.
    conflicts("^mpi", msg="maw.compss must stay MPI-free; see spack.yaml at the repo root") # type: ignore

    def setup_build_environment(self, env):
        spec = self.spec

        env.set("JAVA_HOME", spec["openjdk"].prefix)
        env.set("BOOST_HOME", spec["boost"].prefix)

        xml2_config = os.path.join(str(spec["libxml2"].prefix), "bin", "xml2-config")
        env.set("XML2_CONFIG", xml2_config)

        env.prepend_path("LD_LIBRARY_PATH", spec["libxml2"].prefix.lib)
        env.prepend_path("LD_LIBRARY_PATH", spec["libiconv"].prefix.lib)

    def install(self, spec, prefix):
        """Install into the stage, then copy into the prefix.

        NOT straight into prefix. Spack writes <prefix>/.spack/spec.json
        BEFORE install() runs (directory_layout.create_install_directory) and
        checks for it afterwards (ensure_installed). COMPSs's installer takes
        ownership of its target directory and removes subtrees inside it, which
        takes .spack with them -- producing

            Install prefix exists but contains no spec.json

        after an install that otherwise reported success. install_tree MERGES
        into the prefix rather than replacing it, so the metadata survives.
        """
        staged = join_path(self.stage.path, "compss-install") # type: ignore
        mkdirp(staged) # type: ignore

        args = [staged]
        if "+pycompss" not in spec:
            args.append("--no-python")
        if "+c_binding" in spec:
            args.append("--c-binding")
        if "+tracing" in spec:
            args.append("--tracing")

        # COMPSs's nested makes must not inherit Spack's GNU Make jobserver:
        # the file descriptors it names do not exist in a sub-make started this
        # way, and make reports that as a warning before serialising or
        # failing. Scoped to this call and restored in finally, so a failed
        # install does not leave the build environment altered for the hooks
        # that run afterwards.
        make_env = {k: os.environ.pop(k, None) for k in ("MAKEFLAGS", "MFLAGS")}
        try:
            Executable("./install")(*args) # type: ignore
        finally:
            for k, v in make_env.items():
                if v is not None:
                    os.environ[k] = v

        # Checked against the staged copy, before anything reaches the prefix.
        # COMPSs's install script exits 0 on several partial failures, so a
        # zero return code is not evidence that it produced a runtime.
        if not os.path.isdir(join_path(staged, "Runtime")): # type: ignore
            raise InstallError( # type: ignore
                "COMPSs produced no Runtime/ under %s -- read the install log; "
                "its script exits 0 on several partial failures" % staged
            )

        install_tree(staged, prefix) # type: ignore

    # At CLASS level. An earlier version had this nested inside install(),
    # indented under install_tree, where it was a local function defined and
    # thrown away on every install and @run_after did nothing -- so the check
    # silently never ran.
    @run_after("install") # type: ignore
    def check_binding(self):
        if "+pycompss" in self.spec:
            binding = os.path.join(self.prefix, "Bindings", "python", "3")
            if not os.path.isdir(binding):
                tty.warn( # type: ignore
                    "maw.compss: +pycompss but no Bindings/python/3 -- the "
                    "install script skipped the Python binding, usually a "
                    "missing python3-dev or a JAVA_HOME pointing at a JRE"
                )

    def setup_run_environment(self, env):
        env.set("COMPSS_HOME", str(self.prefix))

        # COMPSs's install only ever creates a major-version directory ("3")
        # no matter the actual python 3.x minor version; that is its naming
        # convention, not a bug, and compssenv does not add it to PYTHONPATH.
        binding = os.path.join(str(self.prefix), "Bindings", "python", "3")
        if os.path.isdir(binding):
            env.prepend_path("PYTHONPATH", binding)

        # The C++ bindings-common library, on LD_LIBRARY_PATH -- the real
        # variable, which is where COMPSs's C++ bindings actually look.
        bc_lib = os.path.join(str(self.prefix), "Bindings", "bindings-common", "lib")
        if os.path.isdir(bc_lib):
            env.prepend_path("LD_LIBRARY_PATH", bc_lib)

        # Runtime/scripts/{user,utils} hold runcompss and friends, which never
        # land on PATH by default. Bindings/c/bin is the C binding's own
        # wrappers, which the old setup_hpc.sh environment also had on PATH.
        for rel in (("Runtime", "scripts", "user"),
                    ("Runtime", "scripts", "utils"),
                    ("Bindings", "c", "bin")):
            d = os.path.join(str(self.prefix), *rel)
            if os.path.isdir(d):
                env.prepend_path("PATH", d)

        jar = os.path.join(str(self.prefix), "Runtime", "compss-engine.jar")
        if os.path.exists(jar):
            env.prepend_path("CLASSPATH", jar)