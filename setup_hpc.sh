#!/bin/bash
# =====================================================================
#  MAW build/run environment
#
#  USAGE:  source setup_hpc.sh
#          MAW_ENGINE=pycompss source setup_hpc.sh
#
#  This script must be SOURCED, not executed.
#
#  THIS SCRIPT PINS A COMPILER. IT PINS NO MPI AND SETS NO LD_LIBRARY_PATH.
#
#  The compiler is pinned to match spack.yaml, because the view's compiled
#  extensions link the libstdc++ of whatever built them and libstdc++ is only
#  backward compatible -- an unpinned compiler moves the view's ABI floor
#  silently (section 4). The MPI is the opposite: a per-task decision taken
#  from the domain skill file inside that task's own command string, so this
#  script names none (section 7). And rather than exporting LD_LIBRARY_PATH,
#  which every child process would inherit, it publishes MAW_LD_BASE: every
#  library directory the environment provides, with no MPI in it, for a task
#  to compose its own MPI onto (section 8c).
#
#  A tool whose module brings its own compiler or MPI -- the LAMMPS module
#  here brings both -- is loaded inside the task's command string, in its own
#  process, not in the shell that sources this. A module loaded in this shell
#  AFTER sourcing wins over the pin and says nothing about it.
#
#  Safe to source repeatedly, by TWO separate mechanisms -- both are
#  needed, and the second was missing:
#
#    1. The venv is activated BEFORE any PATH exports, because
#       venv/bin/activate calls `deactivate nondestructive`, which
#       restores PATH from _OLD_VIRTUAL_PATH and would otherwise wipe
#       everything this script had already added. Sections 4-6 then run
#       after activation and (re-)add their prefixes on top.
#
#    2. Any venv that is ALREADY active when this script starts is
#       deactivated first -- see the guard below. (1) only protects
#       against activate being called by THIS script. It does nothing
#       about `source venv3/bin/activate` run by hand afterwards: that
#       restores _OLD_VIRTUAL_PATH captured at the FIRST activation,
#       before gcc, java and libxml2 went on, and there is no later
#       pass to rebuild it. The symptom is a doubled "((venv3) )" prompt
#       plus an empty `command -v xml2-config`, while LIBXML2_PREFIX is
#       still populated from the earlier sourcing -- which makes it look
#       like a Spack or a libxml2 problem and is neither.
#
#  It is also sourced a second time on the Explorer's compute node, by
#  make_worker_init() under --env multi-node. That node never saw your
#  shell, so this file is the only thing that puts MAW_LD_BASE and
#  MAW_PYTHON there. Two consequences:
#
#    * It must set NO SHELL OPTIONS that survive. No `set -e`, no
#      `set -u`, no `set -o pipefail`. make_worker_init runs
#      `export PYTHONPATH=...:$PYTHONPATH` two commands after sourcing
#      this, and a leftover `set -u` aborts the entire PBS submit script
#      on that unset variable. `set -a`/`set +a` around the .env block is
#      fine because it is turned off again immediately.
#
#    * It must TOLERATE a missing Spack, a missing venv and a missing
#      .env rather than returning early. On a login node the whole thing
#      is there; inside a submit script a `return 1` takes the job with
#      it. Everything below warns and carries on.
# =====================================================================

# ---- Guard: refuse to run as a subprocess -----------------------------
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
    echo "ERROR: this script must be sourced, not executed."
    echo "       Run:  source ${0}"
    exit 1
fi

# ---- Guard: leave any already-active venv cleanly ---------------------
#
# See note (2) in the header. Cheap, idempotent, and a no-op inside
# make_worker_init, where VIRTUAL_ENV is unset and the test
# short-circuits before `declare -F` is reached.
if [ -n "${VIRTUAL_ENV:-}" ] && declare -F deactivate >/dev/null 2>&1; then
    echo "==> Deactivating already-active virtualenv (${VIRTUAL_ENV})..."
    deactivate
fi

# Progress notes. Sections 8a/8b called this before it existed anywhere in
# the file, so every sourcing emitted "_maw_note: command not found" on
# stderr -- harmless with no `set -e`, but it is noise in every PBS job log
# and the lines those calls were meant to print never appeared.
_maw_note() { echo "==> $*"; }

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"

# Which engine's libraries to put in MAW_LD_BASE. Unset means all of them.
MAW_ENGINE="${MAW_ENGINE:-all}"

# ---------------------------------------------------------------------
# 1. Spack environment and installed prefixes
#
#    Not fatal when absent -- a local run has no Spack, and every
#    consumer below degrades to a warning.
#
#    No MPI prefix is looked up. spack.yaml declares every MPI provider
#    buildable: false with no externals, so nothing in this environment is
#    MPI-linked and there is nothing here to find. See section 7.
# ---------------------------------------------------------------------
_loc() { spack location -i "$1" 2>/dev/null || echo ""; }

if command -v spack >/dev/null 2>&1; then
    if [ -f "${SCRIPT_DIR}/spack.yaml" ] || [ -d "${SCRIPT_DIR}/.spack-env" ]; then
        echo "==> Activating Spack environment at ${SCRIPT_DIR}..."

        # `cd` in THIS shell, not in a subshell. An earlier version wrapped this
        # as `( cd "${SCRIPT_DIR}" && spack env activate . )`, which activates
        # the environment inside the subshell and then throws it away when the
        # subshell exits. It printed the same "Activating..." line and returned
        # 0, so nothing looked wrong -- but every `spack location -i` after it
        # ran with no environment active and returned "", which is why
        # JAVA_PREFIX fell through to its hardcoded fallback and CPPFLAGS came
        # out empty.
        _MAW_OLDPWD="${PWD}"
        cd "${SCRIPT_DIR}" || echo "WARNING: cannot cd to ${SCRIPT_DIR}"

        # Two ways to activate, because `spack` is only a shell function in a
        # shell that has sourced share/spack/setup-env.sh. Interactively it
        # usually has (via ~/.bashrc). The PBS submit script that
        # make_worker_init builds has NOT, and there `spack env activate`
        # resolves to the BINARY, which merely PRINTS the shell commands to
        # stdout instead of applying them -- activation silently does nothing
        # on exactly the node where it is hardest to notice.
        if [ "$(type -t spack)" = "function" ]; then
            spack env activate . || \
                echo "WARNING: spack env activate failed; using what is on PATH"
        else
            eval "$(spack env activate --sh .)" 2>/dev/null || \
                echo "WARNING: spack env activate --sh failed; using what is on PATH"
        fi

        cd "${_MAW_OLDPWD}" || true
    fi
    echo "==> Locating package installation paths..."
    JAVA_PREFIX=$(_loc openjdk@21)
    BOOST_PREFIX=$(_loc boost)
    # Unpinned on purpose. spack.yaml's python spec is free to move, and a
    # hardcoded version here resolves to nothing when it does, silently falls
    # through to `command -v python3`, and builds the venv from the system
    # interpreter -- which cannot see the Spack python extensions at all.
    # (The env currently concretizes python@3.12.13; do not write that
    # number down here.)
    PYTHON_PREFIX=$(_loc python)
    LIBXML2_PREFIX=$(_loc libxml2)
    PAPI_PREFIX=$(_loc papi)

    # Every prefix coming back empty means `spack location -i` found nothing,
    # which has exactly two causes and they need different fixes. Saying so
    # here beats letting it surface later as "xml2-config: *** MISSING ***"
    # and an empty CPPFLAGS, which look like path bugs and are not.
    if [ -z "${BOOST_PREFIX}${LIBXML2_PREFIX}${PAPI_PREFIX}${PYTHON_PREFIX}" ]; then
        echo "WARNING: the Spack environment resolved NO package prefixes."
        echo "         Everything below falls back to a hardcoded path or to PATH."
        echo "         Check which of the two it is:"
        echo "           spack env status        # is this env active at all?"
        echo "           spack find              # are its specs actually installed?"
        echo "         An env that is active but never concretized and installed"
        echo "         resolves nothing -- run: spack concretize -f && spack install"
    fi
else
    echo "==> No spack on PATH -- skipping prefix discovery (fine for a local run)"
    JAVA_PREFIX=""; BOOST_PREFIX=""
    PYTHON_PREFIX=""; LIBXML2_PREFIX=""; PAPI_PREFIX=""
fi

# ---- The compiler: pinned, matching spack.yaml --------------------------
#
# GCC 13.2.0 is a site external, so `spack location -i` may not resolve it;
# the path is written down here and declared in spack.yaml's `packages: gcc:`
# block, and the two must agree. This is the compiler the view's extensions
# were built by, and libstdc++ is only backward compatible, so sourcing under
# an older one breaks them.
#
# Override with MAW_GCC_PREFIX for a different build.
#
# NOTE ON ORDERING. Section 4 prepends this gcc's bin to PATH, so a module
# loaded BEFORE sourcing loses to this pin, and a module loaded AFTER wins
# silently -- including one loaded as a dependency of something else. The
# LAMMPS module on this machine says outright that it unloads any other gcc
# and openmpi. Load such a module inside the task's own command string
# (`module purge && module load ... && <command>`), in its own process,
# rather than in the shell that sourced this.
GCC_PREFIX="${MAW_GCC_PREFIX:-/gpfs/fs1/soft/improv/software/spack-built/linux-rhel8-x86_64/gcc-8.5.0/gcc-13.2.0-iyqxotb}"

# Ask the compiler where its runtime is rather than guessing lib vs lib64.
# gcc answers with the absolute path it would itself link against, which is
# correct for any prefix layout -- including lib/gcc/<triple>/<version>,
# which a two-candidate loop does not cover.
_maw_gcc_libdir() {
    [ -n "${MAW_GCC_LIBDIR:-}" ] && { echo "${MAW_GCC_LIBDIR}"; return 0; }
    _gxx="${GCC_PREFIX}/bin/g++"
    [ -x "${_gxx}" ] || _gxx="$(command -v g++ || command -v gcc || echo "")"
    [ -n "${_gxx}" ] || return 1
    _so="$("${_gxx}" -print-file-name=libstdc++.so.6 2>/dev/null)"
    # A compiler that cannot find it echoes the bare filename back.
    case "${_so}" in
        /*) [ -e "${_so}" ] && { dirname "${_so}"; return 0; } ;;
    esac
    return 1
}
GCC_LIBDIR="$(_maw_gcc_libdir || echo "")"
if [ -z "${GCC_LIBDIR}" ]; then
    echo "WARNING: could not locate libstdc++ for ${GCC_PREFIX}."
    echo "         Compiled Python extensions may fail to import with a"
    echo "         GLIBCXX version error. Check that the prefix above exists,"
    echo "         or set MAW_GCC_LIBDIR to its runtime directory."
fi

# Fallback for when the environment is not active and `spack location -i` has
# nothing to say: an already-set JAVA_HOME first (a module-provided JDK should
# win over a written-down path), then the openjdk external declared in
# spack.yaml's `packages:` block.
#
# An existing JAVA_HOME is only trusted if it actually CONTAINS a JDK. Plain
# `${JAVA_HOME:-default}` keeps whatever is in the variable, which makes a bad
# value permanent for the life of the shell: a stale JAVA_HOME exported by an
# earlier version of this script was re-adopted on every later sourcing, so the
# warning kept naming a path the file no longer mentions.
_MAW_JDK_FALLBACK="/gpfs/fs1/soft/improv/software/spack-built/linux-rhel8-zen3/gcc-12.3.0/openjdk-21.0.0_35-23zksi2"
if [ -z "$JAVA_PREFIX" ]; then
    if [ -n "${JAVA_HOME:-}" ] && [ -x "${JAVA_HOME}/bin/java" ]; then
        JAVA_PREFIX="${JAVA_HOME}"
    else
        [ -n "${JAVA_HOME:-}" ] && [ ! -x "${JAVA_HOME}/bin/java" ] && \
            echo "WARNING: ignoring JAVA_HOME=${JAVA_HOME} (no bin/java there)."
        JAVA_PREFIX="${_MAW_JDK_FALLBACK}"
    fi
fi

# ---------------------------------------------------------------------
# 2. Virtualenv  -- MUST come before any PATH exports (see header note)
#
#    Built from the Spack python when there is one, else from whatever
#    python3 is on PATH, so a local checkout works unchanged.
# ---------------------------------------------------------------------
VENV_DIR="${MAW_VENV_DIR:-${SCRIPT_DIR}/venv3}"

# The VIEW's python, not the python package's own prefix. Declared here, before
# the branch below, because BOTH halves need it -- the create path to build from
# it, and the reuse path to check an existing venv against it. It was originally
# set inside the create branch only, so the reuse branch compared against an
# empty string, silently found no mismatch, and MAW_VENV_REBUILD then rebuilt
# from whatever `command -v python3` happened to be.
#
# Why the view and not ${PYTHON_PREFIX}: a Spack python extension declares
# `extends("python")`, so Spack installs it into its own prefix and LINKS it
# into the view -- it is not in the python package's site-packages. Same
# interpreter either way; different set of visible packages.
_VIEW_PYTHON="${SCRIPT_DIR}/.spack-env/view/bin/python3"

if [ ! -x "${VENV_DIR}/bin/python3" ]; then
    # lib-dynload is what distinguishes a real install from a venv, and the
    # glob avoids naming a minor version that the manifest is free to change.
    _DYNLOAD=""
    if [ -n "$PYTHON_PREFIX" ]; then
        for _cand in "${PYTHON_PREFIX}"/lib/python3.*/lib-dynload; do
            [ -d "${_cand}" ] && _DYNLOAD="${_cand}" && break
        done
    fi

    # Falling back to the system python is right on a laptop and wrong on a
    # cluster. A spack.yaml in the repo is the signal that this project expects
    # a Spack-provided interpreter, so when one exists and we still could not
    # find it, say so loudly -- otherwise another user on another machine gets
    # whatever `python3` their site provides while spack.yaml pins a different
    # version, and the only symptom is a version skew nobody looks for.
    #
    # MAW_REQUIRE_SPACK_PYTHON=1 turns that warning into a refusal: no venv is
    # built from the system interpreter at all.
    if [ -x "${_VIEW_PYTHON}" ]; then
        BASE_PYTHON="${_VIEW_PYTHON}"
    elif [ -n "${_DYNLOAD}" ]; then
        echo "WARNING: no python in ${SCRIPT_DIR}/.spack-env/view/bin -- building the"
        echo "         venv from ${PYTHON_PREFIX} instead. Spack python EXTENSIONS"
        echo "         live in the view, not there, so they will not be importable."
        echo "         Check the summary's 'venv sees view' line below."
        BASE_PYTHON="${PYTHON_PREFIX}/bin/python3"
    else
        if [ -n "$PYTHON_PREFIX" ]; then
            echo "WARNING: ${PYTHON_PREFIX} has no lib/python3.*/lib-dynload -- it is"
            echo "         probably a venv, not a real install."
        fi
        BASE_PYTHON="$(command -v python3)"
        if [ -f "${SCRIPT_DIR}/spack.yaml" ]; then
            echo "WARNING: falling back to the SYSTEM python (${BASE_PYTHON})"
            echo "         $(${BASE_PYTHON} --version 2>&1)"
            echo "         but ${SCRIPT_DIR}/spack.yaml exists, so this project"
            echo "         expects the Spack interpreter. The version and the"
            echo "         available packages will not match what spack.yaml pins."
            echo "         Fix: spack env activate . && spack install"
            if [ "${MAW_REQUIRE_SPACK_PYTHON:-0}" = "1" ]; then
                echo "ERROR: MAW_REQUIRE_SPACK_PYTHON=1 -- refusing to build a venv"
                echo "       from the system python. No venv created."
                BASE_PYTHON=""
            fi
        fi
    fi
    if [ -n "${BASE_PYTHON}" ]; then
        # --system-site-packages lets the venv see the view's own
        # site-packages, which is where Spack puts every python extension it
        # builds. A sealed venv cannot import any of them.
        #
        # Nothing MPI-linked is in the view any more, so this no longer has
        # the MPI consequences it once did -- but it still decides whether a
        # Spack-built extension or a pip wheel of the same name wins, and the
        # view is the one that was built against this compiler.
        #
        # Set MAW_VENV_SEALED=1 to opt out.
        _VENV_OPTS=""
        [ "${MAW_VENV_SEALED:-0}" = "1" ] || _VENV_OPTS="--system-site-packages"
        echo "==> Creating virtualenv at ${VENV_DIR} from ${BASE_PYTHON} ${_VENV_OPTS}..."
        # shellcheck disable=SC2086
        "${BASE_PYTHON}" -m venv ${_VENV_OPTS} "${VENV_DIR}" || \
            echo "ERROR: venv creation failed -- MAW_PYTHON will be unset"
    else
        echo "ERROR: no python3 found; cannot create a virtualenv"
    fi
else
    # The venv already exists, so it is NOT rebuilt -- but it may have been
    # built from a different interpreter than the one this environment now
    # offers, and that is invisible otherwise. pyvenv.cfg records the base
    # python under `home`, so compare it with what we would pick today.
    BASE_PYTHON="$(readlink -f "${VENV_DIR}/bin/python3" 2>/dev/null)"
    _WANT_PYTHON="${_VIEW_PYTHON}"
    [ -x "${_WANT_PYTHON}" ] || _WANT_PYTHON="${PYTHON_PREFIX}/bin/python3"
    if [ -x "${_WANT_PYTHON}" ]; then
        _HAVE_HOME="$(sed -n 's/^home *= *//p' "${VENV_DIR}/pyvenv.cfg" 2>/dev/null)"
        _WANT_HOME="$(dirname "$(readlink -f "${_WANT_PYTHON}")")"
        if [ -n "${_HAVE_HOME}" ] && [ "$(readlink -f "${_HAVE_HOME}")" != "${_WANT_HOME}" ]; then
            echo "WARNING: ${VENV_DIR} was built from a DIFFERENT python."
            echo "           venv base: ${_HAVE_HOME}"
            echo "           would use: ${_WANT_HOME}"
            echo "         It is being reused as-is, so Spack python extensions"
            echo "         may not import."
            echo "         Rebuild it with:  MAW_VENV_REBUILD=1 source setup_hpc.sh"
        fi
    fi
fi

# Explicit rebuild. The venv is disposable -- everything in it came from pip and
# a requirements file; the view it layers on is the part that took an hour.
if [ "${MAW_VENV_REBUILD:-0}" = "1" ] && [ -d "${VENV_DIR}" ]; then
    echo "==> MAW_VENV_REBUILD=1: removing ${VENV_DIR}..."
    # Leave any active venv first, or `deactivate` later restores paths into a
    # directory that no longer exists. The guard at the top of this file has
    # usually done this already; this covers a venv activated by the create
    # branch above in the same pass.
    if [ -n "${VIRTUAL_ENV:-}" ] && declare -F deactivate >/dev/null 2>&1; then
        deactivate
    fi
    rm -rf "${VENV_DIR}"
    _MAW_RERUN_VENV=1
fi

# Re-run creation when MAW_VENV_REBUILD just deleted it.
if [ "${_MAW_RERUN_VENV:-0}" = "1" ] && [ ! -x "${VENV_DIR}/bin/python3" ]; then
    _BUILD_PYTHON="${_VIEW_PYTHON}"
    [ -x "${_BUILD_PYTHON}" ] || _BUILD_PYTHON="${PYTHON_PREFIX}/bin/python3"
    [ -x "${_BUILD_PYTHON}" ] || _BUILD_PYTHON="$(command -v python3)"
    BASE_PYTHON="${_BUILD_PYTHON}"
    _VENV_OPTS=""
    [ "${MAW_VENV_SEALED:-0}" = "1" ] || _VENV_OPTS="--system-site-packages"
    echo "==> Recreating virtualenv at ${VENV_DIR} from ${BASE_PYTHON} ${_VENV_OPTS}..."
    # shellcheck disable=SC2086
    "${BASE_PYTHON}" -m venv ${_VENV_OPTS} "${VENV_DIR}" || \
        echo "ERROR: venv creation failed -- MAW_PYTHON will be unset"
fi

if [ -f "${VENV_DIR}/bin/activate" ]; then
    echo "==> Activating virtualenv..."
    # shellcheck disable=SC1091
    source "${VENV_DIR}/bin/activate" || echo "ERROR: venv activation failed"
    if ! python3 -m pip --version >/dev/null 2>&1; then
        echo "==> Bootstrapping pip into the venv..."
        python3 -m ensurepip --upgrade >/dev/null 2>&1 || \
            echo "WARNING: ensurepip failed"
    fi
fi

# The one interpreter that runs workflows, pip and the import probes, so
# check_package answers about the environment the workflow actually sees.
# VENV_PYTHON is the same value under the older name the servers also accept.
#
# Exported only when it has a value: the servers read
#   os.environ.get("MAW_PYTHON", os.environ.get("VENV_PYTHON", sys.executable))
# and an exported empty string is *present*, so .get() returns "" and the
# fallback never happens -- every launch then fails on an empty argv[0].
if [ -x "${VENV_DIR}/bin/python3" ]; then
    export MAW_PYTHON="${VENV_DIR}/bin/python3"
    export VENV_PYTHON="${MAW_PYTHON}"
fi

# ---------------------------------------------------------------------
# 3. MAW paths
#
#    Already-set wins: make_worker_init() exports MAW_REPO_ROOT,
#    MAW_WORK_DIR and MAW_DATA_DIR on the Explorer's node AFTER sourcing
#    this, because work/run0 is a per-run decision this file cannot know.
# ---------------------------------------------------------------------
export MAW_REPO_ROOT="${MAW_REPO_ROOT:-${SCRIPT_DIR}}"
export MAW_WORK_DIR="${MAW_WORK_DIR:-${MAW_REPO_ROOT}/work/run0}"
export MAW_DATA_DIR="${MAW_DATA_DIR:-${MAW_REPO_ROOT}/data}"
# Prepend only what is not already there. A plain prepend grows PYTHONPATH on
# every source, and this script is sourced more than once in a shell as a matter
# of course -- three sourcings produced
# "FULL-MAW:FULL-MAW/src:FULL-MAW:FULL-MAW/src:FULL-MAW:FULL-MAW/src:".
_maw_prepend_pythonpath() {
    case ":${PYTHONPATH:-}:" in
        *":$1:"*) : ;;                                   # already present
        *) export PYTHONPATH="$1${PYTHONPATH:+:$PYTHONPATH}" ;;
    esac
}
_maw_prepend_pythonpath "${MAW_REPO_ROOT}/src"
_maw_prepend_pythonpath "${MAW_REPO_ROOT}"

# ---------------------------------------------------------------------
#  Library-path accumulator
#
#  Sections 4-8b build up _MAW_LDPATH, a PLAIN SHELL VARIABLE. They do not
#  touch LD_LIBRARY_PATH, which this script never exports -- see section 8c.
#  Prepending here means the LAST writer ends up FIRST in the final value,
#  which is what section 8b relies on.
# ---------------------------------------------------------------------
_MAW_LDPATH=""
_maw_prepend_ldpath() {
    [ -n "$1" ] || return 0
    case ":${_MAW_LDPATH}:" in
        *":$1:"*) : ;;                                   # already present
        *) _MAW_LDPATH="$1${_MAW_LDPATH:+:$_MAW_LDPATH}" ;;
    esac
}

# ---------------------------------------------------------------------
# 4. Compiler selection
#
#    A plain `pip install` does NOT honour Spack's language-virtual
#    requirements; it uses whatever gcc/g++ is first on PATH. CC and CXX are
#    set to absolute paths because a pip build spawns sub-makes and configure
#    scripts that each re-resolve `gcc`, and an absolute path cannot be lost
#    to a PATH edit the way a search can.
#
#    This prepend is also what makes the pin beat a module loaded BEFORE
#    sourcing. It does nothing about one loaded after -- see the note at
#    GCC_PREFIX above.
# ---------------------------------------------------------------------
if [ -x "${GCC_PREFIX}/bin/gcc" ]; then
    export CC="${GCC_PREFIX}/bin/gcc"
    export CXX="${GCC_PREFIX}/bin/g++"
    export PATH="${GCC_PREFIX}/bin:${PATH}"
else
    echo "WARNING: GCC not found at ${GCC_PREFIX}; using whatever is on PATH."
    echo "         spack.yaml declares that prefix as an external, so if it is"
    echo "         wrong here it is wrong there too and the view will not match."
fi
# The compiler's runtime goes in MAW_LD_BASE. Section 8b re-adds it last so
# it ends up first; this entry is here so it is present regardless.
[ -n "${GCC_LIBDIR}" ] && _maw_prepend_ldpath "${GCC_LIBDIR}"

# ---------------------------------------------------------------------
# 5. Java -- PyCOMPSs
#
#    PyCOMPSs is a pip install in the venv, launched as
#    `python -m pycompss`, which takes the same flags as the runcompss
#    script. It still needs a JVM: libjvm lives in lib/server, and a JRE
#    is not enough if anything rebuilds the C++ bindings.
# ---------------------------------------------------------------------
if [ "$MAW_ENGINE" = "all" ] || [ "$MAW_ENGINE" = "pycompss" ]; then
    export JAVA_HOME="${JAVA_PREFIX}"
    if [ -d "${JAVA_HOME}" ]; then
        export PATH="${JAVA_HOME}/bin:${PATH}"
        if [ -d "${JAVA_HOME}/lib/server" ]; then
            _maw_prepend_ldpath "${JAVA_HOME}/lib/server"
        elif [ -d "${JAVA_HOME}/jre/lib/amd64/server" ]; then
            _maw_prepend_ldpath "${JAVA_HOME}/jre/lib/amd64/server"
        fi
        [ -f "${JAVA_HOME}/include/jni.h" ] || \
            echo "WARNING: jni.h not found under ${JAVA_HOME}/include -- JRE, not JDK."
    else
        echo "WARNING: JAVA_HOME does not exist: ${JAVA_HOME}"
    fi
    command -v java >/dev/null 2>&1 || \
        echo "WARNING: 'java' not on PATH -- check JAVA_HOME=${JAVA_HOME}"

    # The JVM is loaded by a process that has no LD_LIBRARY_PATH from this
    # script, so libjvm must be found some other way. PyCOMPSs locates it from
    # JAVA_HOME, which is exported above. If a COMPSs launch ever fails on
    # libjvm.so, compose the path in that launch's own command:
    #     export LD_LIBRARY_PATH=${MAW_LD_BASE}
    # which already contains the lib/server directory.

    # Extra flags appended to `python -m pycompss`. The server already passes
    # --project/--resources for XML files that exist, and
    # --python_propagate_virtual_environment=true unless it is set here.
    # export MAW_COMPSS_FLAGS="--graph --log_level=debug"
fi

# ---------------------------------------------------------------------
# 6. Header / library paths for the C and Python bindings
#
#    IMPORTANT: every -L is paired with -Wl,-rpath. Extrae's configure uses
#    AM_PATH_XML2, which COMPILES AND RUNS a probe program to read the libxml2
#    version. A bare -L satisfies the linker but not the run-time loader, and
#    the COMPSs extrae installer rewrites the environment for its sub-build,
#    so the probe binary cannot find libxml2.so and configure reports the
#    misleading "Cannot find xml2-config of libXML 2.5.0" error. Baking the
#    path into the binary with -rpath makes the probe run regardless.
#
#    The -rpath pairing matters MORE now than it did: this script no longer
#    exports LD_LIBRARY_PATH at all, so a bare -L is the only thing such a
#    probe would have, and it is not enough. If a pip build that used to work
#    starts failing to find a library at RUN time, the fix is an rpath here or
#    a composed path in that build's own command -- not re-exporting a global.
#
#    Note that the SAME error text appears for a completely different cause:
#    AM_PATH_XML2 starts with AC_PATH_PROG(XML2_CONFIG, xml2-config), so a
#    PATH that simply does not contain libxml2's bin reports it too, without
#    ever reaching the probe. Check the summary's xml2-config line to tell the
#    two apart before changing anything here.
#
#    _libdir picks lib or lib64 per prefix instead of assuming lib.
# ---------------------------------------------------------------------
_CPPFLAGS=""
_LDFLAGS=""

# Echo the first of <prefix>/lib64, <prefix>/lib that actually exists.
_libdir() {
    for _c in lib64 lib; do
        if [ -d "${1}/${_c}" ]; then echo "${1}/${_c}"; return 0; fi
    done
    return 1
}

# Add include + link + rpath + MAW_LD_BASE entry for one prefix.
_add_dep() {
    _p="$1"; _inc="$2"
    [ -z "${_p}" ] && return 0
    [ -n "${_inc}" ] && [ -d "${_inc}" ] && _CPPFLAGS="${_CPPFLAGS} -I${_inc}"
    if _l=$(_libdir "${_p}"); then
        _LDFLAGS="${_LDFLAGS} -L${_l} -Wl,-rpath,${_l}"
        _maw_prepend_ldpath "${_l}"
        [ -d "${_l}/pkgconfig" ] && \
            export PKG_CONFIG_PATH="${_l}/pkgconfig:${PKG_CONFIG_PATH:-}"
    fi
}

if [ -n "$BOOST_PREFIX" ]; then
    export BOOST_HOME="${BOOST_PREFIX}"
    export BOOST_CPPFLAGS="-I${BOOST_PREFIX}/include"
fi

_add_dep "${BOOST_PREFIX}"   "${BOOST_PREFIX}/include"
_add_dep "${LIBXML2_PREFIX}" "${LIBXML2_PREFIX}/include/libxml2"
_add_dep "${PAPI_PREFIX}"    "${PAPI_PREFIX}/include"

# Transitive dependencies. Spack's libxml2 links against Spack's libiconv (and
# zlib/xz/icu), and those did NOT resolve via libxml2.so's own RPATH here --
# Extrae's configure probe died on "libiconv.so.2: cannot open shared object
# file". The environment view links every package AND its dependencies into one
# tree, so adding it covers all of them without naming each prefix.
_VIEW="${SCRIPT_DIR}/.spack-env/view"
[ -d "${_VIEW}" ] && _add_dep "${_VIEW}" ""

# Belt and braces: libiconv explicitly, in case the view is absent or partial.
_add_dep "$(_loc libiconv)" ""

# libxml2's bin holds xml2-config, which Extrae's configure looks for on PATH.
#
# XML2_CONFIG is also exported, because AC_PATH_PROG honours a preset value for
# its variable and skips the PATH search entirely. That matters for the same
# reason the -rpath pairing above does: the COMPSs extrae installer rewrites
# the environment for its sub-build, and a named absolute path cannot be lost
# to a PATH edit the way a search can.
if [ -n "${LIBXML2_PREFIX}" ] && [ -x "${LIBXML2_PREFIX}/bin/xml2-config" ]; then
    export PATH="${LIBXML2_PREFIX}/bin:${PATH}"
    export XML2_CONFIG="${LIBXML2_PREFIX}/bin/xml2-config"
elif [ -n "${LIBXML2_PREFIX}" ]; then
    # The prefix resolved but the script is not in it. Worth naming explicitly:
    # the old guard tested only `-d bin`, so this case added a useless
    # directory to PATH and said nothing, and the failure surfaced an hour
    # later as Extrae's misleading version error.
    echo "WARNING: no executable bin/xml2-config under ${LIBXML2_PREFIX}."
    echo "         Extrae's configure will fail with"
    echo "         'Cannot find xml2-config of libXML 2.5.0 (or above)'."
fi

# The active compiler's runtime needs an rpath too, for the same reason.
[ -n "${GCC_LIBDIR}" ] && _LDFLAGS="${_LDFLAGS} -Wl,-rpath,${GCC_LIBDIR}"

export CPPFLAGS="${_CPPFLAGS# }"
export LDFLAGS="${_LDFLAGS# }"

# ---------------------------------------------------------------------
# 7. MPI -- DELIBERATELY NOT HERE
#
#     This script names no MPI, exports no MPI_HOME, no COMPSS_MPIRUN and no
#     MAW_MPI_LAUNCHER, and puts no libmpi anywhere. That is the point: which
#     MPI a run uses is stated in the domain skill file and applied by the
#     generated task at launch, so a version pinned here would either be
#     ignored or silently override the domain's choice.
#
#     It cannot be a default either. An MPI is not a preference a task can
#     deviate from cheaply -- a pre-built binary resolves exactly one
#     implementation, named in its own RUNPATH, and no process can hold two.
#     Observed on this machine:
#
#       * hacc_tpm has a direct DT_NEEDED on libmpi_cxx.so.40 and calls
#         MPI::Win::Free, MPI::Comm::Comm and MPI::Datatype::Free. Open MPI 5
#         removed the C++ bindings, so it runs on 4.1.x and nothing else.
#       * The LAMMPS python wheel wants libmpi.so.12, the MPICH-family
#         soname, which Open MPI never provides at any version.
#
#     Open MPI 4 and 5 BOTH ship libmpi.so.40, so a mismatch between them is
#     invisible to any soname check. The authority on what a pre-built binary
#     needs is the binary:
#
#         readelf -d <binary> | grep RUNPATH
#         ldd <binary> | grep -iE 'mpi|open-rte|open-pal'
#
#     Two libopen-pal majors (.40 and .80) in one ldd output means two MPIs
#     are already loaded in that process. That is the failure this section
#     exists to prevent, and it hangs rather than erroring.
#
#     What a task does instead, in its own command string:
#
#       module purge && module load <the domain's mpi module> && \
#         export LD_LIBRARY_PATH=${MAW_LD_BASE}:<the domain's mpi libdirs> && \
#         <the domain's launcher> -n <ranks> <program>
#
#     Three parts, all required: the module or library directories, the
#     launcher (never a bare `mpirun`, which is whatever the environment
#     happens to offer), and -- if the program threads -- OMP_NUM_THREADS,
#     since ranks x threads oversubscribes a pinned core set otherwise.
#     Getting the libraries right and the launcher wrong produces N
#     independent one-rank jobs that overwrite each other and exit 0.
#
#     Composing on MAW_LD_BASE, with an ASSIGNMENT rather than an append, is
#     what keeps the domain's MPI the only one present.
# ---------------------------------------------------------------------

# ---------------------------------------------------------------------
# 8. ADIOS2 -- DELIBERATELY NOT HERE
#
#     ADIOS2 is not in spack.yaml and this script configures nothing for it.
#     An MPI-enabled ADIOS2 cannot be MPI-agnostic: libadios2_c_mpi.so
#     records a DT_NEEDED on whichever libmpi it was compiled against, and
#     Open MPI (libmpi.so.40) and the MPICH family (libmpi.so.12) do not
#     share an ABI, so one build cannot serve both.
#
#     A domain that needs ADIOS2 with MPI -- for MPMD coupling, or to pass a
#     communicator to Adios(comm) -- builds it against that domain's MPI and
#     states the install prefix in its skill file, exactly as it states the
#     MPI itself. A domain that only needs file I/O can use a serial ADIOS2
#     from pip, which still reads and writes BP5 and still speaks SST, since
#     SST's transport is its own rather than MPI's; what a serial build
#     cannot do is accept a communicator.
#
#     The PyPI adios2 wheel is SERIAL (bindings.is_built_with_mpi is False),
#     so it is the right choice for the second case and the wrong one for the
#     first. Check which is installed with:
#         python -c 'from adios2 import bindings; print(bindings.is_built_with_mpi)'
# ---------------------------------------------------------------------

# ---------------------------------------------------------------------
# 8a. libstdc++ for the compiled Python extensions -- AUTOMATIC, every engine
#
#     numpy and the other compiled extensions in this environment were built
#     by GCC_PREFIX's compiler and need its libstdc++ at run time. The Spack
#     view can carry an OLDER gcc-runtime alongside them -- with
#     `concretizer: unify: when_possible` nothing prevents that -- and if the
#     loader reaches the old one first the import dies:
#
#       ImportError: .../gcc-runtime-8.5.0-.../lib/libstdc++.so.6:
#         version `GLIBCXX_3.4.29' not found
#           (required by .../numpy/_core/_multiarray_umath...)
#
#     This preload is now the PRIMARY fix rather than a backstop. Section 8b
#     puts the right libdir first in MAW_LD_BASE, but MAW_LD_BASE is not
#     exported as LD_LIBRARY_PATH any more, so ordering alone no longer
#     protects this process -- only the preload and the packages' own RPATHs
#     do. Spack bakes RPATHs into everything it builds, which covers most of
#     it; this covers a library whose RPATH names a stale runtime.
#
#     Preloading the NEWEST libstdc++ is safe rather than risky -- libstdc++
#     is backward compatible, so a binary built against an older one keeps
#     working against a newer one. The reverse is what breaks, and is the
#     error above.
#
#     MAW_NO_LIBSTDCXX_PRELOAD=1 suppresses it. Note that LD_PRELOAD is
#     inherited by every child process, pip and its compilers included, so it
#     is the first thing to turn off when diagnosing a build that works
#     elsewhere and not here.
# ---------------------------------------------------------------------
if [ "${MAW_NO_LIBSTDCXX_PRELOAD:-0}" != "1" ] && [ -n "${GCC_LIBDIR}" ]; then
    if [ -e "${GCC_LIBDIR}/libstdc++.so.6" ]; then
        case ":${LD_PRELOAD:-}:" in
            *":${GCC_LIBDIR}/libstdc++.so.6:"*) : ;;    # already there
            *) export LD_PRELOAD="${GCC_LIBDIR}/libstdc++.so.6${LD_PRELOAD:+:$LD_PRELOAD}" ;;
        esac
    else
        _maw_note "no libstdc++.so.6 under ${GCC_LIBDIR}; nothing to preload"
    fi
fi

# ---------------------------------------------------------------------
# 8b. The compiler's own runtime goes LAST, so it ends up FIRST
#
#     Every prepend above puts its directory at the FRONT of _MAW_LDPATH, so
#     the last one to run wins. Section 4 already put GCC_PREFIX's libdir on,
#     but section 6 then prepended the Spack view and the per-package
#     prefixes in front of it -- and the view links whatever gcc-runtime the
#     concretizer chose, which with `unify: when_possible` can be an OLD one.
#
#     Observed on Improv: the view carried gcc-runtime-8.5.0, so numpy's
#     C extension loaded that libstdc++ and died with
#
#       version `GLIBCXX_3.4.29' not found
#         (required by .../numpy/_core/_multiarray_umath...)
#
#     Re-prepending here is ordering rather than override: the gcc that
#     compiled these extensions supplies their runtime, and nothing later can
#     push a stale one in front of it. This now governs MAW_LD_BASE's order
#     for whichever task composes on it, rather than this process's own
#     resolution -- section 8a's preload covers that.
# ---------------------------------------------------------------------
if [ -n "${GCC_LIBDIR}" ]; then
    # _maw_prepend_ldpath is a no-op for a value already present, so remove
    # the section 4 entry first -- otherwise the compiler runtime keeps
    # whatever position it got there and the view can still precede it.
    _MAW_LDPATH="$(printf '%s' "${_MAW_LDPATH}" | tr ':' '\n' \
        | grep -vxF "${GCC_LIBDIR}" | paste -sd: -)"
    _maw_prepend_ldpath "${GCC_LIBDIR}"
    _maw_note "compiler runtime first in MAW_LD_BASE: ${GCC_LIBDIR}"
fi

# ---------------------------------------------------------------------
# 8c. MAW_LD_BASE -- published; LD_LIBRARY_PATH -- not
#
#     Sections 4-8b accumulated every library directory this environment
#     provides into _MAW_LDPATH: the compiler runtime, the Spack view, boost,
#     libxml2, papi, libiconv, the JVM. None of it is MPI, because nothing in
#     this environment is MPI-linked.
#
#     That value is published as MAW_LD_BASE and NOT exported as
#     LD_LIBRARY_PATH. An exported LD_LIBRARY_PATH is inherited by every
#     child process, which is exactly the wrong shape for a tool whose MPI is
#     chosen per task: a task setting its own would be appending to, or
#     fighting with, whatever this script left behind, and which libmpi a
#     given .so bound to would depend on who wrote first. Publishing a value
#     the task ASSIGNS from makes the task's own command the only thing that
#     decides.
#
#       MAW_LD_BASE        every library directory EXCEPT MPI. The prefix a
#                          task composes on; never consumed as-is.
#       LD_LIBRARY_PATH    unset by this script. A task sets it:
#                            export LD_LIBRARY_PATH=${MAW_LD_BASE}:<mpi libdirs>
#
#     Note the ASSIGNMENT. Appending would leave two MPIs loadable in one
#     process, which hangs rather than erroring -- see section 7.
#
#     This process itself runs without LD_LIBRARY_PATH, relying on Spack's
#     baked-in RPATHs and section 8a's LD_PRELOAD. That is how Spack
#     installations are designed to work. If something here fails to find a
#     library at run time, the fix is an rpath or a composed path in that
#     thing's own command, NOT re-exporting a global.
# ---------------------------------------------------------------------
#     Empty entries are stripped first. An empty entry in a library path means
#     THE CURRENT DIRECTORY -- harmless until a task composes on it and gets
#     "...:/base::/some/mpi/lib", at which point cwd is on the loader's search
#     path for every process in that task.
MAW_LD_BASE="$(printf '%s' "${_MAW_LDPATH:-}" \
    | tr ':' '\n' | grep -v '^$' | paste -sd: -)"
export MAW_LD_BASE

# MAW_LD_BASE is a PROMISE, and worth checking rather than asserting. A task
# composing its own MPI onto it relies on nothing in it providing libmpi; if a
# view entry or a package prefix ever carries one, that task silently gets two
# implementations in one process and hangs instead of erroring.
for _d in $(printf '%s' "${MAW_LD_BASE}" | tr ':' ' '); do
    if ls "${_d}"/libmpi.so.* >/dev/null 2>&1; then
        echo "WARNING: MAW_LD_BASE is NOT MPI-free -- ${_d} provides libmpi."
        echo "         Something in the Spack environment is MPI-linked, which"
        echo "         spack.yaml's buildable: false guards are meant to"
        echo "         prevent. A task composing its own MPI onto this will"
        echo "         load two implementations in one process."
        echo "         Find what added the MPI dependency:  spack find -d"
        break
    fi
done

# Nothing inherits a library path from this script. Unset rather than left
# alone: a value from the login shell or a previously loaded module would
# otherwise survive into every task, which is the global this section exists
# to remove.
unset LD_LIBRARY_PATH

# ---------------------------------------------------------------------
# 9. Headless rendering
#
#    Not per-engine and not optional. Compute nodes have no display, so
#    anything that renders fails without these.
#
#    Only the generic graphics-stack variables belong here: the OpenGL
#    software path, the OSMesa backend, and matplotlib's non-interactive
#    backend. Those are properties of a display-less compute node, true for
#    every workflow regardless of science.
#
#    A variable that names ONE application is not one of them. OVITO_GUI_MODE
#    used to be set here; it configures a particular molecular-visualization
#    tool, which makes it domain knowledge and puts it in that domain's skill
#    file alongside the tool's own path.
# ---------------------------------------------------------------------
export LIBGL_ALWAYS_SOFTWARE=1
export PYOPENGL_PLATFORM=osmesa
export MPLBACKEND="${MPLBACKEND:-Agg}"

# ---------------------------------------------------------------------
# 10. Simulation binaries -- DELIBERATELY NOT HERE
#
#     No simulation executable, environment file or input deck is named in
#     this script, and none should be.
#
#     This script configures the workflow engines and the environment they
#     share. Which executable a given paper needs is not a property of Parsl
#     or PyCOMPSs -- it is domain knowledge, and it belongs in the domain
#     skill file under skills/use_cases/<domain>/, written by the researcher
#     who knows the science.
#
#     The convention is an ABSOLUTE PATH stated in that skill's Tools section
#     and carried into the generated workflow as-is, so nothing has to be on
#     PATH -- and an absolute path cannot be silently shadowed by a different
#     build earlier in the search order.
#
#     The MPI that binary needs, and the ADIOS2 build that matches it, are
#     part of the same domain knowledge: the module to load or the library
#     directories to compose, the launcher to use, and the install prefix of
#     any MPI-linked library the tasks import. Sections 7 and 8 say why none
#     of it can be defaulted here.
#
#     Anything else the executable needs at launch -- an environment file to
#     source first, a parameter file, a working directory -- is stated there
#     too, and the engine expresses it: a shell-command engine can source an
#     env file and launch in one invocation, so the whole sequence goes in the
#     command string it runs.
#
#     If a workflow fails with "command not found" or with a missing libmpi,
#     the fix is in the domain skill file, not here.
# ---------------------------------------------------------------------

# ---------------------------------------------------------------------
# 11. .env
#
#     Python does not read .env on its own. academy_agents.py and
#     mcp_explorer.py both call load_dotenv(), which covers the launching
#     process and the Explorer -- so this block is belt and braces. It
#     still matters for PBS_ACCOUNT, which argparse checks before any of
#     that runs, and on the compute node, where a value read in the
#     launching process would not have travelled.
#
#     `set -a` marks everything defined until `set +a` for export, which
#     is what turns plain KEY=value lines into exported variables. Quotes
#     in the file are handled by bash itself. It is turned off again
#     immediately -- see the header note on shell options.
#
#     load_dotenv() does not override variables already set, so sourcing
#     here and loading there agree.
#
#     This block runs AFTER every PATH export above, so a PATH= line in
#     .env silently replaces the whole search path and undoes sections
#     4-6. PATH is snapshotted and compared rather than parsed, which
#     catches `export PATH=`, a continuation line and an indirect edit
#     alike. LD_LIBRARY_PATH is checked too, since section 8c has just
#     unset it on purpose and a line in .env would undo that for every
#     task in the run.
# ---------------------------------------------------------------------
_ENV_FILE="${MAW_REPO_ROOT}/.env"
if [ -f "${_ENV_FILE}" ]; then
    _MAW_PATH_BEFORE_ENV="${PATH}"
    set -a
    # shellcheck disable=SC1090
    . "${_ENV_FILE}"
    set +a
    echo "==> Loaded ${_ENV_FILE}"
    if [ "${PATH}" != "${_MAW_PATH_BEFORE_ENV}" ]; then
        echo "WARNING: ${_ENV_FILE} changed PATH. Everything sections 4-6 added"
        echo "         (gcc, java, xml2-config) may be gone. Remove the PATH"
        echo "         assignment from .env -- it is for KEY=value settings."
        echo "         Restoring the PATH this script built."
        export PATH="${_MAW_PATH_BEFORE_ENV}"
    fi
    if [ -n "${LD_LIBRARY_PATH:-}" ]; then
        echo "WARNING: ${_ENV_FILE} set LD_LIBRARY_PATH. This script unsets it"
        echo "         deliberately (section 8c) so each task composes its own"
        echo "         from MAW_LD_BASE. Unsetting it again."
        unset LD_LIBRARY_PATH
    fi
else
    echo "WARNING: ${_ENV_FILE} not found -- OPENAI_* and PBS_ACCOUNT unset."
fi

for _v in PBS_ACCOUNT OPENAI_BASE_URL OPENAI_API_KEY MODEL_NAME; do
    if [ -z "$(eval "echo \"\${${_v}:-}\"")" ]; then
        echo "WARNING: ${_v} is not set; see .env"
    fi
done

# ---------------------------------------------------------------------
# 12. Optional run behaviour
# ---------------------------------------------------------------------
#  Move a non-empty work/run0 aside instead of writing over it. Currently
#  honoured under --env multi-node only.
# export MAW_ROTATE_RUN0=1
#  Clear all of work/ first. Blunt -- it removes anything renamed to keep
#  alongside run0.
# export MAW_WIPE_WORK=1

# ---------------------------------------------------------------------
# 13. Summary
# ---------------------------------------------------------------------
echo "======================================================="
echo " MAW environment ready (engine scope: ${MAW_ENGINE})"
echo " MAW_REPO_ROOT:    ${MAW_REPO_ROOT}"
echo " MAW_WORK_DIR:     ${MAW_WORK_DIR}"
echo " MAW_DATA_DIR:     ${MAW_DATA_DIR}"
echo " BASE_PYTHON:      ${BASE_PYTHON:-<unknown>}"
echo " python version:   $(python3 --version 2>&1 || echo '*** MISSING ***')"
echo " MAW_PYTHON:       ${MAW_PYTHON:-*** MISSING: servers fall back to their own ***}"
echo " python3 resolves: $(command -v python3 || echo '*** MISSING ***')"
echo " CC / CXX:         ${CC:-<none>} / ${CXX:-<none>}"
echo " gcc resolves:     $(command -v gcc || echo '*** MISSING ***')"
echo " gcc version:      $(gcc -dumpfullversion -dumpversion 2>/dev/null || echo '*** MISSING ***')"
echo " libstdc++ in use: ${GCC_LIBDIR:+${GCC_LIBDIR}/libstdc++.so.6}${GCC_LIBDIR:-<not located>}"
if [ -n "${GCC_LIBDIR}" ] && [ -d "${SCRIPT_DIR}/.spack-env/view/lib" ]; then
    _VIEW_CXX="$(ls "${SCRIPT_DIR}"/.spack-env/view/lib/libstdc++.so.6.* 2>/dev/null | head -1)"
    _HAVE_CXX="$(ls "${GCC_LIBDIR}"/libstdc++.so.6.* 2>/dev/null | head -1)"
    if [ -n "${_VIEW_CXX}" ] && [ -n "${_HAVE_CXX}" ]; then
        _VIEW_V="${_VIEW_CXX##*/libstdc++.so.}"
        _HAVE_V="${_HAVE_CXX##*/libstdc++.so.}"
        if [ "$(printf '%s\n%s\n' "${_VIEW_V}" "${_HAVE_V}" | sort -V | head -1)" != "${_VIEW_V}" ]; then
            echo " libstdc++ check:  *** the view's runtime (${_VIEW_V}) is NEWER than"
            echo "                       the loaded compiler's (${_HAVE_V}). Extensions"
            echo "                       built by that gcc may fail with a GLIBCXX"
            echo "                       error. Load the newer gcc. ***"
        fi
    fi
fi
echo " JAVA_HOME:        ${JAVA_HOME:-<unset: pycompss scope off>}"
echo " java on PATH:     $(command -v java || echo '*** MISSING ***')"
echo " xml2-config:      $(command -v xml2-config || echo '*** MISSING ***')"
echo " XML2_CONFIG:      ${XML2_CONFIG:-<unset>}"
echo " libiconv.so.2:    $(for _d in $(printf '%s' "${MAW_LD_BASE:-}" | tr ':' ' '); do [ -e "${_d}/libiconv.so.2" ] && { echo "${_d}/libiconv.so.2"; break; }; done || true)"
# MPI is intentionally absent from this summary -- there is nothing to report.
# A task's MPI comes from its domain skill file and exists only inside that
# task's own command string; see sections 7 and 8.
echo " MPI:              <none here, by design -- per-task, from the skill file>"
if [ -n "${MAW_LD_BASE:-}" ]; then
    echo " MAW_LD_BASE:      <set, ${#MAW_LD_BASE} chars, MPI-free>"
else
    echo " MAW_LD_BASE:      <empty>"
fi
echo " LD_LIBRARY_PATH:  ${LD_LIBRARY_PATH:-<unset, by design>}"
if [ -n "${MAW_PYTHON:-}" ]; then
    echo " venv sees view:   numpy=$("${MAW_PYTHON}" -c 'import numpy;print("yes")' 2>/dev/null || echo no)"
    # Whether an adios2 is importable at all, and whether it is the serial
    # wheel or an MPI build. Neither comes from this environment; a domain
    # that needs one installs it. Reported so a run expecting MPI coupling
    # finds out here rather than at Adios(comm).
    _A2="$("${MAW_PYTHON}" -c 'from adios2 import bindings;print("mpi" if bindings.is_built_with_mpi else "serial")' 2>/dev/null)"
    echo " adios2:           ${_A2:-<not installed -- per-domain, see section 8>}"
fi
echo " LD_PRELOAD:       ${LD_PRELOAD:-<none>}"
echo " CPPFLAGS:         ${CPPFLAGS:-<none>}"
echo " LDFLAGS:          ${LDFLAGS:-<none>}"
echo " PYTHONPATH:       ${PYTHONPATH:-<empty>}"
echo " PBS_ACCOUNT:      ${PBS_ACCOUNT:-*** MISSING ***}"
echo " MODEL_NAME:       ${MODEL_NAME:-*** MISSING ***}"
echo " CODER_MODEL_NAME: ${CODER_MODEL_NAME:-<falls back to MODEL_NAME>}"
echo " OPENAI_BASE_URL:  ${OPENAI_BASE_URL:-*** MISSING ***}"
# `${VAR:+<set>}${VAR:-*** MISSING ***}` prints BOTH when the variable is set:
# the first expansion yields <set> and the second yields the value, because
# `:-` only substitutes when unset. That printed the key itself into every
# terminal and every PBS job log. One expansion, chosen by a test instead.
if [ -n "${OPENAI_API_KEY:-}" ]; then
    echo " OPENAI_API_KEY:   <set>"
else
    echo " OPENAI_API_KEY:   *** MISSING ***"
fi
echo "======================================================="
echo
echo " No MPI is configured here. A task that needs one loads it in its own"
echo " command string, from the domain skill file, and composes its library"
echo " path as:  export LD_LIBRARY_PATH=\${MAW_LD_BASE}:<that MPI's libdirs>"
echo
echo " PyCOMPSs is pip-only here. Install it into this venv with:"
echo "   pip install --upgrade pip 'setuptools<70' wheel"
echo "   pip install --no-build-isolation -v pycompss 2>&1 | tee pip_build.log"
echo " Then confirm:  python -c 'import pycompss; print(pycompss.__file__)'"
echo
echo " Do NOT run 'source venv3/bin/activate' after this -- it restores the"
echo " pre-venv PATH and drops gcc, java and xml2-config. Source this script"
echo " again instead; it is safe to repeat."
echo "======================================================="

unset _CPPFLAGS _LDFLAGS _VIEW _ENV_FILE _l _c _d _p _inc _v _A2
unset _DYNLOAD _cand _VENV_OPTS _VIEW_PYTHON _MAW_OLDPWD _MAW_JDK_FALLBACK
unset _WANT_PYTHON _HAVE_HOME _WANT_HOME _MAW_RERUN_VENV _BUILD_PYTHON
unset _MAW_PATH_BEFORE_ENV _MAW_LDPATH _gxx _so _VIEW_CXX _HAVE_CXX _VIEW_V _HAVE_V
unset -f _maw_gcc_libdir
unset -f _maw_prepend_pythonpath _maw_prepend_ldpath
unset -f _loc _libdir _add_dep _maw_note