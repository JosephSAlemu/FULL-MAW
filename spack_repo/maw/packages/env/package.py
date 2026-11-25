import os

# BundlePackage, NOT from `spack.package import *`.
#
# spack/package.py carries a per-API-version table of the names a recipe gets,
# and BundlePackage is in NONE of them -- checked across v0.21 through develop.
# The star import has never provided it, so `class Env(BundlePackage)` raises
#
#     cannot load package 'env' from the 'maw' repository:
#     name 'BundlePackage' is not defined
#
# at repo-load time, before anything is built. Under Package API v2 it lives in
# the builtin repo's build systems, which Spack puts on sys.path itself.
from spack_repo.builtin.build_systems.bundle import BundlePackage # type: ignore

from spack.package import * # type: ignore
from spack.util.executable import Executable # type: ignore


class Env(BundlePackage):
    """Environment aggregator for MAW.

    Carries no code. Exists so that setup_run_environment runs on
    `spack env activate` and can compute what cannot be written as a
    literal in env_vars: the compiler runtime directory, the libstdc++
    preload, lib-vs-lib64 per prefix, LD_LIBRARY_PATH, MODULEPATH, the PMI
    scrub, and the repo paths the MCP servers read.

    This is setup_hpc.sh sections 4 through 9, minus sections 7 and 8,
    which exported nothing on purpose and still do. There is no activate
    script: `spack env activate` runs this hook, which is the whole of what
    one would have done. bootstrap.sh exists only for what cannot
    bootstrap itself -- installing Spack, and creating the venv.
    """

    homepage = "https://github.com/maw"
    version("1.0") # type: ignore

    depends_on("gcc", type=("build", "run")) # type: ignore
    depends_on("boost", type=("build", "run")) # type: ignore
    depends_on("libxml2", type=("build", "run")) # type: ignore
    depends_on("papi", type=("build", "run")) # type: ignore
    depends_on("python", type=("build", "run")) # type: ignore
    depends_on("openjdk", type=("build", "run")) # type: ignore

    # Directories owned by the distribution. An external gcc's runtime lives
    # here, and in that case there is nothing to prepend: prepending them
    # would put the system libxml2/papi/python ahead of the Spack view.
    SYSTEM_DIR_ROOTS = ("/lib", "/lib64", "/usr/lib", "/usr/lib64", "/usr/bin", "/bin")

    # ---- helpers ----------------------------------------------------

    def _is_system_dir(self, d):
        rd = os.path.realpath(d)
        return any(rd == p or rd.startswith(p + os.sep) for p in self.SYSTEM_DIR_ROOTS)

    def _libdir(self, prefix):
        for cand in ("lib64", "lib"):
            d = os.path.join(str(prefix), cand)
            if os.path.isdir(d):
                return d
        return None

    def _gcc_compilers(self):
        """Return (cc, cxx) for the gcc in the DAG, external or not.

        An external gcc may have prefix /usr with binaries named gcc-14/g++-14;
        only packages.yaml's extra_attributes knows that. Joining
        prefix/bin/gcc there silently yields the system gcc, so
        extra_attributes and the package's own cc/cxx come first and the
        prefix layout is only the last resort.
        """
        gcc = self.spec["gcc"]

        try:
            cc, cxx = gcc.package.cc, gcc.package.cxx
            if cc and cxx:
                return cc, cxx
        except Exception:
            pass

        compilers = (gcc.extra_attributes or {}).get("compilers", {})
        cc, cxx = compilers.get("c"), compilers.get("cxx")
        if cc and cxx:
            return cc, cxx

        bindir = os.path.join(str(gcc.prefix), "bin")
        return os.path.join(bindir, "gcc"), os.path.join(bindir, "g++")

    def _gcc_libdir(self, cxx=None):
        override = os.environ.get("MAW_GCC_LIBDIR")
        if override and os.path.isdir(override):
            return override
        gxx = cxx or self._gcc_compilers()[1]
        if not os.access(gxx, os.X_OK):
            return None
        try:
            so = Executable(gxx)(
                "-print-file-name=libstdc++.so.6", output=str, error=str
            ).strip()
        except Exception:
            return None
        if os.path.isabs(so) and os.path.exists(so):
            # gcc answers with ../.. components; normalise so the dir can be
            # compared against SYSTEM_DIR_ROOTS and deduped in ldpath.
            return os.path.realpath(os.path.dirname(so))
        return None

    def _repo_root(self):
        """The MAW checkout, not the Spack environment.

        MAW_REPO_ROOT wins. Otherwise the active environment's path is
        the repo root itself (spack.yaml lives there) -- checked
        against a file that only the checkout has, so a relocated env
        reports nothing instead of a wrong root.

        NOTE: do not add a __file__-relative guess ahead of this. It was
        tried, on the theory that ev.active_environment() returns None during
        activation, and it broke MAW_PYTHON: Spack may load this recipe from
        its CACHED COPY in the install prefix, so __file__ points into the
        store, walking up finds no academy_agents.py, and a wrong-but-
        plausible path can be returned before this branch ever runs. The
        environment route is the one that works -- JAVA_HOME being set proves
        this hook runs, and everything below depends on this returning a real
        path.
        """
        root = os.environ.get("MAW_REPO_ROOT")
        if root:
            return root
        try:
            import spack.environment as ev # type: ignore

            active = ev.active_environment()
            if active:
                guess = os.path.abspath(active.path)
                if os.path.isfile(os.path.join(guess, "academy_agents.py")):
                    return guess
                return active.path
        except Exception:
            pass
        return None

    # ---- the hook ---------------------------------------------------

    #: Set by PBS and by MPI launchers. Meaningful only to a process the
    #: launcher started; inherited by everything else in the job, where an
    #: MPI_Init that trusts them hangs rather than erroring. setup_hpc.sh
    #: stripped these for one tool's command; unsetting them at activation
    #: covers every task not launched through mpirun, and a task that IS
    #: gets them back from its launcher -- the only place they mean
    #: anything.
    PMI_VARS = ("PMI_RANK", "PMI_SIZE", "PMI_FD", "PMI_JOBID",
                "I_MPI_HYDRA_BOOTSTRAP")

    def setup_run_environment(self, env):
        spec = self.spec
        ldpath = []

        for _pmi in self.PMI_VARS:
            env.unset(_pmi)

        def add_lib(d):
            if d and d not in ldpath:
                ldpath.insert(0, d)

        # --- compiler: absolute, so sub-makes cannot re-resolve `cc` ---
        cc, cxx = self._gcc_compilers()
        if os.access(cc, os.X_OK) and os.access(cxx, os.X_OK):
            env.set("CC", cc)
            env.set("CXX", cxx)
            bindir = os.path.dirname(cc)
            if not self._is_system_dir(bindir):
                env.prepend_path("PATH", bindir)
        else:
            tty.warn("maw.env: gcc reports %s and %s, neither executable" % (cc, cxx)) # type: ignore

        # --- JDK -------------------------------------------------------
        # NOTE ON JAVA_HOME AND THE VIEW. With `view: true`, Spack substitutes
        # the view root for each spec's prefix when it computes environment
        # modifications, so this comes out as <env>/.spack-env/view. COMPSs
        # then fails, because it looks for the JVM with
        #     find "${JAVA_HOME}"/lib/ -name libjvm.so
        # and find does not descend into a symlinked directory -- the view is
        # a symlink farm, so `find -L` locates libjvm.so and plain `find`
        # reports nothing. Verified on Improv; COMPSs dies 1.4 s in.
        #
        # Three fixes were tried and none worked from inside this file:
        # excluding openjdk from the view, reading the prefix through
        # spack.store's path_for_spec, and publishing the value under a
        # different variable name. The substitution applies to any value
        # matching a store prefix, whatever it is called.
        #
        # What DOES work, from outside: `spack load openjdk` after activating,
        # which computes modifications per spec with no view. Or `view: false`,
        # which removes the substitution along with the merged tree. The
        # worker prelude in make_worker_init should carry the `spack load`.
        java_home = str(spec["openjdk"].prefix)
        env.set("JAVA_HOME", java_home)
        if not os.path.exists(os.path.join(java_home, "include", "jni.h")):
            tty.warn("maw.env: no jni.h under %s/include -- JRE, not JDK" % java_home) # type: ignore
        # BOTH lib/server and lib. lib/server has libjvm.so; lib has libjli.so
        # and the rest of the launcher libraries. setup_hpc.sh used plain
        # $JAVA_HOME/lib, and COMPSs's C++ bindings dlopen the JVM, so they
        # want the pair -- an earlier version of this package added only
        # lib/server and would have broken that.
        for rel in ("lib/server", "jre/lib/amd64/server", "lib", "jre/lib/amd64"):
            d = os.path.join(java_home, rel)
            if os.path.isdir(d):
                add_lib(d)

        # --- headers, libs, rpaths -------------------------------------
        cppflags, ldflags = [], []
        deps = (
            (spec["boost"], "include"),
            (spec["libxml2"], os.path.join("include", "libxml2")),
            (spec["papi"], "include"),
        )
        for dep, inc_rel in deps:
            inc = os.path.join(str(dep.prefix), inc_rel)
            if os.path.isdir(inc):
                cppflags.append("-I%s" % inc)
            libdir = self._libdir(dep.prefix)
            if libdir:
                ldflags.append("-L%s -Wl,-rpath,%s" % (libdir, libdir))
                add_lib(libdir)
                pc = os.path.join(libdir, "pkgconfig")
                if os.path.isdir(pc):
                    env.prepend_path("PKG_CONFIG_PATH", pc)

        # The view consolidates every package AND its dependencies into one
        # tree, which is what resolves libiconv for libxml2's own probes.
        # Taken from the ACTIVE ENVIRONMENT, not the repo root: with
        # spack.yaml at the repo root, these are the same directory --
        # but taken from the environment anyway, so a relocated env is right.
        #
        # Skipped entirely under `view: false`, since there is then no merged
        # tree to point at. Anything that relied on it resolving a transitive
        # library must find it by rpath instead.
        try:
            import spack.environment as ev # type: ignore

            active = ev.active_environment()
            env_path = active.path if active else None
        except Exception:
            env_path = None
        if env_path:
            view = os.path.join(env_path, ".spack-env", "view")
            vlib = self._libdir(view)
            if vlib:
                ldflags.append("-L%s -Wl,-rpath,%s" % (vlib, vlib))
                add_lib(vlib)

        env.set("BOOST_HOME", str(spec["boost"].prefix))
        env.set("BOOST_CPPFLAGS", "-I%s" % os.path.join(str(spec["boost"].prefix), "include"))

        # xml2-config: named absolutely, because AC_PATH_PROG honours a
        # preset value and skips the PATH search that a sub-build can lose.
        xml2 = os.path.join(str(spec["libxml2"].prefix), "bin", "xml2-config")
        if os.access(xml2, os.X_OK):
            env.set("XML2_CONFIG", xml2)
            env.prepend_path("PATH", os.path.dirname(xml2))
        else:
            tty.warn("maw.env: no executable bin/xml2-config under %s" % spec["libxml2"].prefix) # type: ignore

        # --- compiler runtime: added LAST so it sorts FIRST -------------
        # With an external gcc this is a distribution directory. The loader
        # finds it anyway, and hoisting it to the front of LD_LIBRARY_PATH would
        # put the system libxml2/papi ahead of the Spack ones, so in that
        # case the rpath, the preload and the reorder are all skipped.
        gcc_libdir = self._gcc_libdir(cxx)
        if gcc_libdir and self._is_system_dir(gcc_libdir):
            tty.debug( # type: ignore
                "maw.env: gcc runtime is in %s (system); not added to "
                "LD_LIBRARY_PATH or rpath" % gcc_libdir
            )
        elif gcc_libdir:
            ldflags.append("-Wl,-rpath,%s" % gcc_libdir)
            if gcc_libdir in ldpath:
                ldpath.remove(gcc_libdir)
            ldpath.insert(0, gcc_libdir)
            preload = os.path.join(gcc_libdir, "libstdc++.so.6")
            if os.path.exists(preload) and os.environ.get("MAW_NO_LIBSTDCXX_PRELOAD") != "1":
                env.prepend_path("LD_PRELOAD", preload)
        else:
            tty.warn("maw.env: could not locate libstdc++ for the active gcc") # type: ignore

        if cppflags:
            env.set("CPPFLAGS", " ".join(cppflags))
        if ldflags:
            env.set("LDFLAGS", " ".join(ldflags))

        # --- LD_LIBRARY_PATH: the real variable, MPI-free ---------------
        # setup_hpc.sh section 8c published these under MAW_LD_BASE instead,
        # a name the dynamic loader ignores, so that each task could assemble
        # LD_LIBRARY_PATH=$MAW_LD_BASE:<its own MPI libs> for itself. That
        # staging variable is gone, and nothing was lost:
        #
        #   * `module load <mpi>` PREPENDS to LD_LIBRARY_PATH. A task that
        #     does `module purge && module load <its mpi>` therefore puts its
        #     MPI in front of these, with no assembly on its side.
        #   * The collision 8c defended against -- an MPI path here that a
        #     task's own MPI then fights with -- cannot arise, because
        #     spack.yaml declares every MPI provider `buildable: false`, so
        #     nothing MPI-linked can concretize into this environment. That
        #     is a stronger guarantee than a renamed variable.
        #
        # So these are set directly, which is also what COMPSs's C++ bindings
        # need: bindings-common/lib has to be somewhere the loader looks, and
        # under MAW_LD_BASE it never was.
        if not ldpath:
            tty.warn( # type: ignore
                "maw.env: no dependency provided a library directory. "
                "Something is wrong with this environment's view; check "
                "`spack find -e . --loaded`."
            )

        # The check that actually matters, kept from 8c and now run against
        # the real variable: if anything here ships a libmpi, a task's own
        # MPI can end up alongside it, and two implementations in one
        # process hang rather than erroring.
        for d in ldpath:
            if any(
                f.startswith("libmpi.so.")
                for f in (os.listdir(d) if os.path.isdir(d) else [])
            ):
                tty.warn( # type: ignore
                    "maw.env: %s provides libmpi, so LD_LIBRARY_PATH is not "
                    "MPI-free. A task loading its domain's MPI would have two "
                    "in one process. Find what pulled it in: spack find -d" % d
                )
                break

        # An MPI module loaded in the ACTIVATING shell is the one case the
        # buildable: false guard cannot cover, since its paths come from
        # outside Spack entirely. Prepending below leaves them in front,
        # which is wrong for every task whose domain names a different MPI.
        inherited = os.environ.get("LD_LIBRARY_PATH", "")
        if inherited:
            for d in inherited.split(":"):
                if d and os.path.isdir(d) and any(
                    f.startswith("libmpi.so.") for f in os.listdir(d)
                ):
                    tty.warn( # type: ignore
                        "maw.env: an MPI is already on LD_LIBRARY_PATH (%s). "
                        "It will stay in front of this environment's paths "
                        "and be inherited by every task. Activate from a "
                        "shell with `module purge` done first." % d
                    )
                    break

        for d in ldpath:
            env.prepend_path("LD_LIBRARY_PATH", d)

        # --- repo paths and venv ----------------------------------------
        root = self._repo_root()
        if root:
            env.set("MAW_REPO_ROOT", root)
            env.set("MAW_PROJECT_DIR", root)   # src/common/consts.py reads this one
            env.set("MAW_WORK_DIR", os.path.join(root, "work", "run0"))
            # inputs/data, not data/. The three input directories were
            # consolidated under inputs/, and this export OVERRIDES the
            # defaults in src/common/consts.py (DATA_PATH) and
            # src/servers/server.py -- both of which already say
            # inputs/data. Left as "data" this silently wins over both,
            # and every task resolves /app/inputs/data correctly while
            # the servers' own data_dir points at a directory that no
            # longer exists.
            env.set("MAW_DATA_DIR", os.path.join(root, "inputs", "data"))
            env.prepend_path("PYTHONPATH", root)
            env.prepend_path("PYTHONPATH", os.path.join(root, "src"))

            # The venv. maw.env activates it rather than a shell script
            # doing so: it sets VIRTUAL_ENV and puts the venv's bin FIRST
            # on PATH, so `python` is the venv's and `pip install` at
            # runtime lands somewhere writable.
            #
            # A venv and not a Spack package for the PyPI-only wheels:
            # a Spack prefix is content-addressed and must not be written
            # to after install, which would break the install_package MCP
            # tool; Spack would also need network from the build
            # environment, and any wheel update would change the
            # environment's hash. See bootstrap.sh.
            venv = os.environ.get("MAW_VENV_DIR") or os.path.join(root, "venv3")
            vpy = os.path.join(venv, "bin", "python3")
            if os.access(vpy, os.X_OK):
                env.set("VIRTUAL_ENV", venv)
                env.prepend_path("PATH", os.path.join(venv, "bin"))
                env.set("MAW_PYTHON", vpy)
                env.set("VENV_PYTHON", vpy)
            else:
                tty.warn( # type: ignore
                    "maw.env: no venv at %s -- run bootstrap.sh. Without "
                    "it MAW_PYTHON is unset and the servers cannot start."
                    % venv
                )
        else:
            # LOUD, because everything above this point still worked and the
            # run fails much later with "MAW_PYTHON is unset". If this fires,
            # ev.active_environment() returned None or its path holds no
            # academy_agents.py -- activate from the repo root.
            tty.warn( # type: ignore
                "maw.env: could not determine the MAW checkout, so "
                "MAW_REPO_ROOT, MAW_PYTHON, MAW_WORK_DIR, MAW_DATA_DIR and "
                "PYTHONPATH are all UNSET. Activate from the repo root "
                "(`spack env activate .`), or set MAW_REPO_ROOT."
            )

        if root:
            # Nothing is exported about any engine's MPI, and nothing here
            # loads one -- setup_hpc.sh section 8's point, now true of three
            # engines out of four rather than a rule about one. The task
            # writes its own `module load`, per knowledge/lcrc.SKILL.md.
            #
            # MODULEPATH is a different matter: it is a path, and
            # prepend_path is exactly what this hook is for. The wilkins
            # module that `spack module refresh` generated lives under
            # spack_mpi/, not on the site MODULEPATH, so without this a
            # task's `module load wilkins/<version>` finds nothing.
            # Extending MODULEPATH loads nothing, so LD_LIBRARY_PATH stays
            # MPI-free and the warning above stays unreachable.
            _mpi_modules = os.path.join(root, "spack_mpi", "modules")
            if os.path.isdir(_mpi_modules):
                env.prepend_path("MODULEPATH", _mpi_modules)