#!/bin/bash
# =====================================================================
#  MAW build/run environment
#
#  USAGE:  source setup_hpc.sh
#          MAW_ENGINE=adios source setup_hpc.sh
#
#  This script must be SOURCED, not executed.
#
#  Safe to source repeatedly, by TWO separate mechanisms -- both are
#  needed, and the second was missing:
#
#    1. The venv is activated BEFORE any PATH exports, because
#       venv/bin/activate calls `deactivate nondestructive`, which
#       restores PATH from _OLD_VIRTUAL_PATH and would otherwise wipe
#       everything this script had already added. Sections 4-7 then run
#       after activation and (re-)add their prefixes on top.
#
#    2. Any venv that is ALREADY active when this script starts is
#       deactivated first -- see the guard below. (1) only protects
#       against activate being called by THIS script. It does nothing
#       about `source venv3/bin/activate` run by hand afterwards: that
#       restores _OLD_VIRTUAL_PATH captured at the FIRST activation,
#       before gcc, java, libxml2 and MPI went on, and there is no later
#       pass to rebuild it. The symptom is a doubled "((venv3) )" prompt
#       plus an empty `command -v xml2-config`, while LIBXML2_PREFIX is
#       still populated from the earlier sourcing -- which makes it look
#       like a Spack or a libxml2 problem and is neither.
#
#  It is also sourced a second time on the Explorer's compute node, by
#  make_worker_init() under --env multi-node. That node never saw your
#  shell, so this file is the only thing that puts the library paths and
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

# Which engine's libraries to put on the path. Unset means all of them.
# Gating matters on a shared LD_LIBRARY_PATH: an unused install can shadow the
# MPI that the engine you ARE running was linked against.
MAW_ENGINE="${MAW_ENGINE:-all}"

# ---------------------------------------------------------------------
# 1. Spack environment and installed prefixes
#
#    Not fatal when absent -- a local run has no Spack, and every
#    consumer below degrades to a warning.
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
    OPENMPI_PREFIX=$(_loc openmpi)        # version pinned in spack.yaml
    # Unpinned on purpose. spack.yaml's python spec is free to move, and a
    # hardcoded version here resolves to nothing when it does, silently falls
    # through to `command -v python3`, and builds the venv from the system
    # interpreter -- which cannot see the Spack py-mpi4py / py-numpy / adios2
    # bindings at all. (The env currently concretizes python@3.12.13; do not
    # write that number down here.)
    PYTHON_PREFIX=$(_loc python)
    LIBXML2_PREFIX=$(_loc libxml2)
    PAPI_PREFIX=$(_loc papi)
    ADIOS2_PREFIX=$(_loc adios2)

    # Every prefix coming back empty means `spack location -i` found nothing,
    # which has exactly two causes and they need different fixes. Saying so
    # here beats letting it surface later as "xml2-config: *** MISSING ***"
    # and an empty CPPFLAGS, which look like path bugs and are not.
    if [ -z "${BOOST_PREFIX}${LIBXML2_PREFIX}${PAPI_PREFIX}${OPENMPI_PREFIX}" ]; then
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
    JAVA_PREFIX=""; BOOST_PREFIX=""; OPENMPI_PREFIX=""
    PYTHON_PREFIX=""; LIBXML2_PREFIX=""; PAPI_PREFIX=""; ADIOS2_PREFIX=""
fi

# GCC 13.2.0 is a site external, so `spack location -i` may not resolve it.
GCC_PREFIX="${MAW_GCC_PREFIX:-/gpfs/fs1/soft/improv/software/spack-built/linux-rhel8-x86_64/gcc-8.5.0/gcc-13.2.0-iyqxotb}"

# Fallbacks for the site-provided externals.
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

# Site MPI fallback, used only when the Spack environment resolves nothing.
#
# A Spack external is just a declared path: the previous openmpi@4.1.7 entry
# pointed at a directory that did not exist on the compute nodes. Checking that
# bin/mpirun is really there turns that class of mistake into a warning here,
# rather than "bash: mpirun: command not found" inside a job log an hour later.
#
# Override with MAW_MPI_PREFIX rather than editing this line. Any pre-built MPI
# binary a workflow launches must be linked against the same MPI, but which
# binary that is, and where it lives, is domain knowledge -- it belongs in the
# domain skill file, not here.
_SITE_MPI="${MAW_MPI_PREFIX:-/gpfs/fs1/soft/improv/software/spack-built/linux-rhel8-zen3/gcc-13.2.0/openmpi-4.1.6-knzkvb2}"
if [ -z "$OPENMPI_PREFIX" ] || [ ! -x "${OPENMPI_PREFIX}/bin/mpirun" ]; then
    if [ -x "${_SITE_MPI}/bin/mpirun" ]; then
        OPENMPI_PREFIX="${_SITE_MPI}"
    else
        OPENMPI_PREFIX=""
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
# Why the view and not ${PYTHON_PREFIX}: adios2 +python declares
# `extends("python")`, so Spack installs the bindings into ADIOS2's own prefix
# and LINKS them into the view -- they are not in the python package's
# site-packages. py-numpy and py-mpi4py work the same way. Same interpreter
# either way; different set of visible packages.
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
        echo "         (adios2 bindings, py-numpy, py-mpi4py) live in the view, not"
        echo "         there, so they will not be importable. Check the summary's"
        echo "         'venv sees view' line below."
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
        # --system-site-packages is load-bearing, not a convenience. The Spack
        # env builds adios2 +python, py-mpi4py and py-numpy into the view's own
        # site-packages, and a sealed venv cannot see any of them -- so
        # `import adios2` fails, check_package("adios2") reports False, and the
        # obvious next move (pip install adios2) installs the SERIAL wheel over
        # the top, which is the exact thing +mpi exists to avoid.
        #
        # This is what spack_maw.yaml means by "a venv layered on the view":
        # Spack supplies anything MPI-linked, pip supplies the rest into this
        # venv on top. Set MAW_VENV_SEALED=1 to opt out -- which is also how you
        # reproduce the known-good activate_pycompss_env.sh venv exactly, if a
        # view-provided package ever turns out to interfere with a pip build.
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
            echo "         (adios2 bindings, py-mpi4py, py-numpy) may not import."
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
# 4. Compiler selection
#
#    A plain `pip install` does NOT honour Spack's `require: %gcc@13.2.0`;
#    it uses whatever gcc/g++ is first on PATH.
# ---------------------------------------------------------------------
if [ -x "${GCC_PREFIX}/bin/gcc" ]; then
    export CC="${GCC_PREFIX}/bin/gcc"
    export CXX="${GCC_PREFIX}/bin/g++"
    export PATH="${GCC_PREFIX}/bin:${PATH}"
    for _d in lib64 lib; do
        [ -d "${GCC_PREFIX}/${_d}" ] && \
            export LD_LIBRARY_PATH="${GCC_PREFIX}/${_d}:${LD_LIBRARY_PATH:-}"
    done
else
    echo "WARNING: GCC not found at ${GCC_PREFIX}; using system gcc."
fi

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
            export LD_LIBRARY_PATH="${JAVA_HOME}/lib/server:${LD_LIBRARY_PATH:-}"
        elif [ -d "${JAVA_HOME}/jre/lib/amd64/server" ]; then
            export LD_LIBRARY_PATH="${JAVA_HOME}/jre/lib/amd64/server:${LD_LIBRARY_PATH:-}"
        fi
        [ -f "${JAVA_HOME}/include/jni.h" ] || \
            echo "WARNING: jni.h not found under ${JAVA_HOME}/include -- JRE, not JDK."
    else
        echo "WARNING: JAVA_HOME does not exist: ${JAVA_HOME}"
    fi
    command -v java >/dev/null 2>&1 || \
        echo "WARNING: 'java' not on PATH -- check JAVA_HOME=${JAVA_HOME}"

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
#    the COMPSs extrae installer rewrites LD_LIBRARY_PATH for its sub-build,
#    so the probe binary cannot find libxml2.so and configure reports the
#    misleading "Cannot find xml2-config of libXML 2.5.0" error. Baking the
#    path into the binary with -rpath makes the probe run regardless.
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

# Add include + link + rpath + LD_LIBRARY_PATH for one prefix.
_add_dep() {
    _p="$1"; _inc="$2"
    [ -z "${_p}" ] && return 0
    [ -n "${_inc}" ] && [ -d "${_inc}" ] && _CPPFLAGS="${_CPPFLAGS} -I${_inc}"
    if _l=$(_libdir "${_p}"); then
        _LDFLAGS="${_LDFLAGS} -L${_l} -Wl,-rpath,${_l}"
        export LD_LIBRARY_PATH="${_l}:${LD_LIBRARY_PATH:-}"
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

# GCC runtime libs also need an rpath, for the same reason.
if [ -x "${GCC_PREFIX}/bin/gcc" ]; then
    if _l=$(_libdir "${GCC_PREFIX}"); then
        _LDFLAGS="${_LDFLAGS} -Wl,-rpath,${_l}"
    fi
fi

export CPPFLAGS="${_CPPFLAGS# }"
export LDFLAGS="${_LDFLAGS# }"

# ---------------------------------------------------------------------
# 7. MPI
#
#    MAW_MPI_LAUNCHER is what servers/server.py hands to run_workflow for
#    an engine that launches ranks: the ADIOS server's `mpirun -n <ranks>`
#    and run_mpmd's colon-separated command line both come from it.
#
#    An ABSOLUTE path, not the bare name. A COMPSs worker or a Parsl
#    block has a thin PATH, and "mpirun: command not found" an hour into
#    a job is an expensive way to discover that.
# ---------------------------------------------------------------------
if [ -n "${OPENMPI_PREFIX}" ]; then
    if _l=$(_libdir "${OPENMPI_PREFIX}"); then
        export LD_LIBRARY_PATH="${_l}:${LD_LIBRARY_PATH:-}"
    fi
    [ -d "${OPENMPI_PREFIX}/bin" ] && export PATH="${OPENMPI_PREFIX}/bin:${PATH}"
    export MPI_HOME="${OPENMPI_PREFIX}"
    if [ -x "${OPENMPI_PREFIX}/bin/mpirun" ]; then
        _MAW_MPIRUN="${OPENMPI_PREFIX}/bin/mpirun"
        # COMPSs resolves @mpi(runner=...) off the worker's PATH, which has
        # none, so the bare name cannot work there either.
        export COMPSS_MPIRUN="${_MAW_MPIRUN}"
    fi
else
    echo "WARNING: no usable mpirun found under a Spack prefix; falling back to PATH."
    echo "         Multi-rank runs will fail if there is none there either."
fi

# Resolved fresh every time, NOT with `${MAW_MPI_LAUNCHER:-...}`.
#
# That default-if-unset form preserves the value THIS SCRIPT exported on a
# previous sourcing, which is how a bare "mpirun" got stuck: the first source
# ran before the OpenMPI prefix was on PATH, fell through to `echo mpirun`, and
# every later source then kept that useless value even once the real absolute
# path was available.
#
# An override is still honoured, but only if it names something executable --
# which a stale bare name does not.
if [ -z "${_MAW_MPIRUN:-}" ]; then
    if [ -n "${MAW_MPI_LAUNCHER:-}" ] && command -v "${MAW_MPI_LAUNCHER}" >/dev/null 2>&1; then
        _MAW_MPIRUN="$(command -v "${MAW_MPI_LAUNCHER}")"
    else
        _MAW_MPIRUN="$(command -v mpirun || echo mpirun)"
    fi
fi
export MAW_MPI_LAUNCHER="${_MAW_MPIRUN}"

# ---------------------------------------------------------------------
# 8. ADIOS2
#
#    Needs the MPI-enabled build. The PyPI adios2 wheel is SERIAL
#    (bindings.is_built_with_mpi is False), so Adios(comm) raises and SST
#    cannot stream between ranks.
# ---------------------------------------------------------------------
if [ "$MAW_ENGINE" = "all" ] || [ "$MAW_ENGINE" = "adios" ]; then
    export ADIOS2_HOME="${ADIOS2_HOME:-${ADIOS2_PREFIX:-$HOME/.local/adios2-mpi}}"
    if [ -d "${ADIOS2_HOME}" ]; then
        _add_dep "${ADIOS2_HOME}" ""
    else
        echo "WARNING: ADIOS2_HOME does not exist: ${ADIOS2_HOME}"
    fi
fi

# ---------------------------------------------------------------------
# 8a. libstdc++ for the compiled Python extensions -- AUTOMATIC, every engine
#
#     numpy, adios2 and the other compiled extensions in this environment were
#     built by GCC_PREFIX's compiler and need its libstdc++ at run time. The
#     Spack view can carry an OLDER gcc-runtime alongside them -- with
#     `concretizer: unify: when_possible` nothing prevents that -- and if the
#     loader reaches the old one first the import dies:
#
#       ImportError: .../gcc-runtime-8.5.0-.../lib/libstdc++.so.6:
#         version `GLIBCXX_3.4.29' not found
#           (required by .../numpy/_core/_multiarray_umath...)
#
#     Section 8b below puts the right libdir first on LD_LIBRARY_PATH, which
#     fixes it whenever the loader consults that path. This preload covers
#     what ordering cannot: a library whose own RPATH names a stale runtime.
#
#     Preloading the NEWEST libstdc++ is safe rather than risky -- libstdc++ is
#     backward compatible, so a binary built against an older one keeps working
#     against a newer one. The reverse is what breaks, and is the error above.
#
#     NOT gated on MAW_ENGINE: numpy is imported by analysis and visualization
#     stages whatever engine runs the workflow, so this is not ADIOS-specific.
#     It also is not opt-in. An earlier version put it behind
#     MAW_PRELOAD_LIBSTDCXX=1, on the argument that a unified Spack env could
#     not produce two runtimes -- untrue for `unify: when_possible`, and all
#     the flag achieved was moving a required step onto whoever ran the script.
#
#     MAW_NO_LIBSTDCXX_PRELOAD=1 suppresses it. Note that LD_PRELOAD is
#     inherited by every child process, pip and its compilers included, so it
#     is the first thing to turn off when diagnosing a build that works under
#     activate_pycompss_env.sh (which sets no LD_PRELOAD) and not here.
# ---------------------------------------------------------------------
if [ "${MAW_NO_LIBSTDCXX_PRELOAD:-0}" != "1" ]; then
    if _l=$(_libdir "${GCC_PREFIX}"); then
        if [ -e "${_l}/libstdc++.so.6" ]; then
            case ":${LD_PRELOAD:-}:" in
                *":${_l}/libstdc++.so.6:"*) : ;;        # already there
                *) export LD_PRELOAD="${_l}/libstdc++.so.6${LD_PRELOAD:+:$LD_PRELOAD}" ;;
            esac
        else
            _maw_note "no libstdc++.so.6 under ${_l}; relying on LD_LIBRARY_PATH order"
        fi
    fi
fi

# ---------------------------------------------------------------------
# 8b. The compiler's own runtime goes LAST, so it ends up FIRST
#
#     Every prepend above puts its directory at the FRONT of
#     LD_LIBRARY_PATH, so the last one to run wins. Section 4 already put
#     GCC_PREFIX's libdir on, but sections 6-8 then prepended the Spack
#     view and the per-package prefixes in front of it -- and the view
#     links whatever gcc-runtime the concretizer chose, which with
#     `unify: when_possible` can be an OLD one.
#
#     Observed on Improv: the view carried gcc-runtime-8.5.0, so numpy's
#     C extension loaded that libstdc++ and died with
#
#       version `GLIBCXX_3.4.29' not found
#         (required by .../numpy/_core/_multiarray_umath...)
#
#     even though a newer libstdc++ was present further down the path.
#     numpy had been built against the newer one; the loader simply found
#     the older one first.
#
#     Re-prepending here is the fix, and it is ordering rather than
#     override: the gcc that compiled these extensions supplies their
#     runtime, and nothing later can push a stale one in front of it.
# ---------------------------------------------------------------------
if [ -x "${GCC_PREFIX}/bin/gcc" ]; then
    if _l=$(_libdir "${GCC_PREFIX}"); then
        export LD_LIBRARY_PATH="${_l}:${LD_LIBRARY_PATH:-}"
        _maw_note "compiler runtime first on LD_LIBRARY_PATH: ${_l}"
    fi
fi

# ---------------------------------------------------------------------
# 9. Headless rendering
#
#    Not per-engine and not optional. Compute nodes have no display, so
#    anything that renders fails without these. The MCP servers used to
#    inject them into every task environment from a constant in
#    common/consts.py; they no longer do, so this is the only place they
#    are set.
#
#    Only the generic graphics-stack variables belong here: the OpenGL
#    software path, the OSMesa backend, and matplotlib's non-interactive
#    backend. Those are properties of a display-less compute node, true for
#    every workflow regardless of science.
#
#    A variable that names ONE application is not one of them. OVITO_GUI_MODE
#    used to be set here; it configures a particular molecular-visualization
#    tool, which makes it domain knowledge and puts it in that domain's skill
#    file alongside the tool's own path. Same for any other per-application
#    rendering switch -- adding them here would mean every domain editing the
#    shared engine setup.
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
#     This script configures the three workflow engines and the environment
#     they share. Which executable a given paper needs is not a property of
#     Parsl, PyCOMPSs or ADIOS2 -- it is domain knowledge, and it belongs in
#     the domain skill file under skills/use_cases/<domain>/, written by the
#     researcher who knows the science. Putting it here would mean editing the
#     shared engine setup for every new paper, which is exactly the coupling
#     the servers/ refactor removed.
#
#     The convention is an ABSOLUTE PATH stated in that skill's Tools section
#     and carried into the generated workflow as-is, so nothing has to be on
#     PATH -- and an absolute path cannot be silently shadowed by a different
#     build earlier in the search order.
#
#     Anything else the executable needs at launch -- an environment file to
#     source first, a parameter file, a working directory -- is stated there
#     too, and the engine expresses it: a shell-command engine can source an
#     env file and launch in one invocation, so the whole sequence goes in the
#     command string it runs.
#
#     If a workflow fails with "command not found", the fix is in the domain
#     skill file, not here.
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
#     4-7. PATH is snapshotted and compared rather than parsed, which
#     catches `export PATH=`, a continuation line and an indirect edit
#     alike.
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
        echo "WARNING: ${_ENV_FILE} changed PATH. Everything sections 4-7 added"
        echo "         (gcc, java, xml2-config, mpirun) may be gone. Remove the"
        echo "         PATH assignment from .env -- it is for KEY=value settings."
        echo "         Restoring the PATH this script built."
        export PATH="${_MAW_PATH_BEFORE_ENV}"
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
echo " CC / CXX:         ${CC:-<system>} / ${CXX:-<system>}"
echo " gcc resolves:     $(command -v gcc || echo '*** MISSING ***')"
echo " JAVA_HOME:        ${JAVA_HOME:-<unset: pycompss scope off>}"
echo " java on PATH:     $(command -v java || echo '*** MISSING ***')"
echo " xml2-config:      $(command -v xml2-config || echo '*** MISSING ***')"
echo " XML2_CONFIG:      ${XML2_CONFIG:-<unset>}"
echo " libiconv.so.2:    $(for _d in $(echo "${LD_LIBRARY_PATH:-}" | tr ':' ' '); do [ -e "${_d}/libiconv.so.2" ] && { echo "${_d}/libiconv.so.2"; break; }; done || true)"
echo " MAW_MPI_LAUNCHER: ${MAW_MPI_LAUNCHER:-*** MISSING ***}"
echo " mpirun resolves:  $(command -v mpirun || echo '*** MISSING ***')"
echo " ADIOS2_HOME:      ${ADIOS2_HOME:-<unset: adios scope off>}"
if [ -n "${MAW_PYTHON:-}" ]; then
    echo " venv sees view:   adios2=$("${MAW_PYTHON}" -c 'import adios2,sys;print("yes")' 2>/dev/null || echo no)" \
         "mpi4py=$("${MAW_PYTHON}" -c 'import mpi4py;print("yes")' 2>/dev/null || echo no)" \
         "numpy=$("${MAW_PYTHON}" -c 'import numpy;print("yes")' 2>/dev/null || echo no)"
    echo " adios2 MPI build: $("${MAW_PYTHON}" -c 'from adios2 import bindings;print(bindings.is_built_with_mpi)' 2>/dev/null || echo '<adios2 not importable>')"
fi
echo " LD_PRELOAD:       ${LD_PRELOAD:-<none>}"
echo " libstdc++ in use: $(for _d in $(echo "${LD_LIBRARY_PATH:-}" | tr ':' ' '); do [ -e "${_d}/libstdc++.so.6" ] && { echo "${_d}/libstdc++.so.6"; break; }; done || true)"
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
echo " PyCOMPSs is pip-only here. Install it into this venv with:"
echo "   pip install --upgrade pip 'setuptools<70' wheel"
echo "   pip install --no-build-isolation -v pycompss 2>&1 | tee pip_build.log"
echo " Then confirm:  python -c 'import pycompss; print(pycompss.__file__)'"
echo
echo " Do NOT run 'source venv3/bin/activate' after this -- it restores the"
echo " pre-venv PATH and drops gcc, java, xml2-config and mpirun. Source this"
echo " script again instead; it is safe to repeat."
echo "======================================================="

unset _CPPFLAGS _LDFLAGS _VIEW _ENV_FILE _SITE_MPI _l _c _d _p _inc _v
unset _DYNLOAD _cand _VENV_OPTS _VIEW_PYTHON _MAW_OLDPWD _MAW_MPIRUN _MAW_JDK_FALLBACK
unset _WANT_PYTHON _HAVE_HOME _WANT_HOME _MAW_RERUN_VENV _BUILD_PYTHON
unset _MAW_PATH_BEFORE_ENV
unset -f _maw_prepend_pythonpath
unset -f _loc _libdir _add_dep _maw_note