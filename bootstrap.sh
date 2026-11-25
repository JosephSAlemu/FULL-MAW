#!/usr/bin/env bash
# =====================================================================
#  bootstrap.sh -- run ONCE, on a new machine.
#
#      ./bootstrap.sh
#      ./bootstrap.sh --spack-root ~/spack
#
#  EXECUTED, NOT SOURCED. `source bootstrap.sh` would put `set -euo pipefail`
#  into your interactive shell and let die()'s `exit 1` close it. There is a
#  guard at the top that refuses, but run it as ./bootstrap.sh and the question
#  does not arise. After it finishes you activate with
#
#      spack env activate <repo>
#
#  and nothing else. There is no activate script: `spack env activate` runs
#  spack_repo/maw/packages/env/package.py's setup_run_environment, which sets CC/CXX,
#  JAVA_HOME, COMPSS_HOME, MAW_PYTHON, LD_LIBRARY_PATH, MODULEPATH, CPPFLAGS,
#  the libstdc++ preload and the PMI scrub, and puts the venv on PATH. That is everything a 190-line activate.sh used to do.
#
#  WHY THIS SCRIPT EXISTS AT ALL
#  Three bootstrap problems, none of which Spack can solve for itself:
#
#    1. INSTALLING SPACK. `spack` is a shell function. Nothing inside Spack
#       can clone or source Spack. Chicken and egg.
#    2. THE VENV. It must be created from the environment's own python so
#       --system-site-packages sees the view, which means the environment
#       has to exist first. Spack has no recipe for "make a venv".
#    3. THE PyPI PACKAGES, from requirements.txt. Most have no Spack
#       package; adios2 must not be built by one:
#
#       adios2 above all. Spack HAS an adios2 package and building it would
#       be wrong -- the PyPI wheel is a serial build (nothing matching
#       libmpi in its DT_NEEDED) that still reports ADIOS2_HAVE_SST TRUE
#       and ships EVPath/FFS/atl/dill. That exact combination is what lets
#       a task load whatever MPI its domain names without putting a second
#       MPI in the process. Building from source would spend minutes
#       reproducing the same artifact and risk Spack resolving an MPI into
#       it.
#
#       They also need a WRITABLE site-packages: the install_package MCP
#       tool pip-installs during a run, and a Spack prefix is
#       content-addressed and must not be written to after install.
#
#  Everything else -- build staging, compiler choice, module generation,
#  every environment variable -- is spack.yaml and package.py.
# =====================================================================
# ---- Guard: must be EXECUTED, not sourced -------------------------------
# `set -euo pipefail` below and the `exit 1` in die() both apply to whatever
# shell runs this. Sourced, that shell is your interactive one: the first
# failing command or the first die() exits it, and the terminal closes with no
# message. Checked before `set -e` is in effect, so this branch cannot itself
# trip it.
#
# Nothing here needs to persist in your shell -- bootstrap only writes to disk.
# The thing that DOES need sourcing is `spack env activate`, which you run
# afterwards; see the closing message.
if [ -n "${BASH_SOURCE[0]:-}" ] && [ "${BASH_SOURCE[0]}" != "${0}" ]; then
    echo "ERROR: run this, do not source it:" >&2
    echo "           ./bootstrap.sh" >&2
    echo "       Sourced, its 'set -e' and 'exit' would close this shell." >&2
    return 1 2>/dev/null || exit 1
fi

set -euo pipefail

MAW_REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${MAW_REPO_ROOT}"

SPACK_ROOT_ARG=""
[ "${1:-}" = "--spack-root" ] && { SPACK_ROOT_ARG="${2:?--spack-root needs a path}"; }

die()  { echo "ERROR: $*" >&2; exit 1; }
note() { echo; echo "==> $*"; }

# ---- 1. Spack -----------------------------------------------------------
if ! command -v spack >/dev/null 2>&1; then
    for _c in "${SPACK_ROOT_ARG:+${SPACK_ROOT_ARG}/share/spack/setup-env.sh}" \
              "${MAW_SPACK_SETUP:-}" \
              "${SPACK_ROOT:-}/share/spack/setup-env.sh" \
              "${HOME}/spack/share/spack/setup-env.sh"; do
        [ -n "${_c}" ] && [ -f "${_c}" ] && { . "${_c}"; break; }
    done
fi

if ! command -v spack >/dev/null 2>&1; then
    _target="${SPACK_ROOT_ARG:-${HOME}/spack}"
    note "Spack not found. Cloning into ${_target}"
    echo "    (pass --spack-root <path> to put it elsewhere; Ctrl-C to abort)"
    sleep 3
    git clone -c feature.manyFiles=true --depth=2 \
        https://github.com/spack/spack.git "${_target}"
    . "${_target}/share/spack/setup-env.sh"
    echo
    echo "    Add this to your ~/.bashrc:"
    echo "        source ${_target}/share/spack/setup-env.sh"
fi
command -v spack >/dev/null 2>&1 || die "spack still not on PATH after bootstrap"
note "spack: $(command -v spack)"

# ---- 2. Compilers -------------------------------------------------------
# No compiler is named in any of this repo's Spack files. If the site's compilers have
# never been registered, nothing will concretize.
if ! spack compiler list 2>/dev/null | grep -q .; then
    note "no compilers registered; running spack compiler find"
    spack compiler find
fi
spack compiler list 2>/dev/null | grep -q . || die "no compilers found. Load a compiler module, then: spack compiler find"

# ---- 3. The core environment -------------------------------------------
# spack.yaml and repo.yaml are at the repo root, so the repo root IS the
# environment and `-e .` works from here.
note "building the core environment (MPI-free)"
spack -e . concretize -f
spack -e . install --fail-fast

# ---- 4. The venv --------------------------------------------------------
# From the ENVIRONMENT's python, not whatever python is on PATH: that is the
# interpreter --system-site-packages must point at for the view to be
# visible, and getting it from elsewhere is the most common way to end up
# with a venv that cannot import anything.
if [ ! -x venv3/bin/python3 ]; then
    _py="$(spack -e . location -i python)/bin/python3"
    [ -x "${_py}" ] || die "cannot find the environment's python at ${_py}"
    note "creating venv3 from ${_py}"

    # ensurepip, checked before use. `python -m venv` relies on it to put pip
    # in the new venv, and nothing in spack.yaml provides pip -- deliberately,
    # since the venv's own pip shadows anything in the view. A python built
    # without ensurepip would otherwise fail several lines later with a message
    # about pip rather than about the interpreter.
    if ! "${_py}" -c "import ensurepip" >/dev/null 2>&1; then
        die "${_py} has no ensurepip, so it cannot create a venv with pip.
       Either rebuild python with it, or add py-pip to spack.yaml's specs and
       create the venv with --without-pip, then bootstrap pip by hand."
    fi

    "${_py}" -m venv --system-site-packages "${MAW_REPO_ROOT}/venv3"
    venv3/bin/python3 -m pip install -q --upgrade pip setuptools wheel
else
    note "venv3 already exists"
fi

# ---- 5. The PyPI packages ----------------------------------------------
# From requirements.txt, not a list hardcoded here: this script should not be
# a second place the dependencies are written down. See that file for why
# mpi4py and h5py are deliberately absent from it.
[ -f requirements.txt ] || die "requirements.txt not found in ${MAW_REPO_ROOT}"
note "installing from requirements.txt"
venv3/bin/python3 -m pip install -r requirements.txt

# ---- 5a. Which ADIOS2 ---------------------------------------------------
# MAW_ADIOS picks ONE of two, and actively removes the other. Having both
# installed is the failure this exists to prevent: two packages named adios2,
# and which one `import adios2` finds is decided by PYTHONPATH order at that
# moment. The serial one raises on comm=, the MPI one does collective writes,
# and nothing in either case announces which answered.
#
#   serial  the PyPI wheel (installed above). SST over TCP, no MPI. The adios
#           engine works for every domain whatever MPI it names, but cannot do
#           MPMD or a collective parallel write.
#   mpi     built +mpi in the spack_mpi/ engines environment. MPMD and collective
#           writes, but bound to that one MPI, so adios becomes gated.
#
# src/servers/adios_sst.py and src/servers/adios_mpmd.py are the two servers;
# mcp_explorer.py reads MAW_ADIOS to decide which one `--engine adios` starts.
case "${MAW_ADIOS:-serial}" in
    serial)
        note "MAW_ADIOS=serial -- adios2 from PyPI (SST, no MPI)"
        ;;
    mpi)
        note "MAW_ADIOS=mpi -- tasks load the +mpi adios2 module"
        # The wheel STAYS. Both builds coexist: PYTHONPATH precedes the venv's
        # site-packages in sys.path, so `module load adios2/<ver>-<mpi>`
        # shadows the wheel and `module unload` restores it. Nothing is
        # installed or removed to switch, which is what makes the choice a
        # per-task one rather than a per-machine one.
        echo "    the PyPI wheel stays; the module shadows it when loaded"
        echo "    build the +mpi one, if you have not already:"
        echo "        module load <the MPI your domains use>"
        echo "        cd spack_mpi && cp spack.yaml.in spack.yaml"
        echo "        # uncomment: - adios2 +mpi +python +sst ~fortran ~hdf5"
        echo "        spack -e . external find openmpi mpich intel-oneapi-mpi"
        echo "        spack -e . install && spack -e . module lmod refresh -y"
        ;;
    *)
        die "MAW_ADIOS must be 'serial' or 'mpi', got '${MAW_ADIOS}'"
        ;;
esac

# A binding that links MPI must not arrive through requirements.txt: it would
# be compiled against whatever MPI happened to be loaded now and bind the venv
# to it. Checked rather than trusted, because the file is edited by hand.
_mpi_pkgs="$(grep -ioE '^[[:space:]]*(mpi4py|h5py)' requirements.txt | tr -d ' ' | sort -u || true)"
if [ -n "${_mpi_pkgs}" ]; then
    echo >&2
    echo "WARNING: requirements.txt lists MPI-linked package(s):" >&2
    echo "${_mpi_pkgs}" | sed 's/^/           /' >&2
    echo "         These bind the venv to whichever MPI was loaded during" >&2
    echo "         bootstrap. Remove them and install after the domain's" >&2
    echo "         module load, per knowledge/lcrc.SKILL.md." >&2
fi

# The assumption the adios engine rests on, checked rather than trusted. A
# future wheel built against an MPI would put two implementations in one
# process once a task loads its domain's, and that hangs rather than erroring.
# Checked whatever MAW_ADIOS says: the wheel is installed either way now, and
# MAW_ADIOS=serial depends on it being serial.
_lib="$(venv3/bin/python3 -c 'import adios2,os;print(os.path.dirname(adios2.__file__))' 2>/dev/null || true)"
if [ -n "${_lib}" ] && ls "${_lib}"/libadios2_core.so.* >/dev/null 2>&1; then
    if readelf -d "${_lib}"/libadios2_core.so.* 2>/dev/null | grep -q 'NEEDED.*libmpi'; then
        echo
        echo "WARNING: this adios2 wheel LINKS MPI." >&2
        echo "         The adios engine is documented as needing none, which" >&2
        echo "         would no longer be true: a task loading its domain's" >&2
        echo "         MPI would then have two in one process. Pin an older" >&2
        echo "         adios2 in this script." >&2
    else
        note "adios2 confirmed serial (no libmpi in DT_NEEDED)"
    fi
fi

# ---- Done ---------------------------------------------------------------
cat <<DONE

==> bootstrap complete.

    Every session:
        spack env activate .

    Then check:
        echo "\$LD_LIBRARY_PATH"    # the non-MPI paths; no libmpi among them
        echo "\$JAVA_HOME"          # the environment's openjdk
        echo "\$COMPSS_HOME"        # the maw.compss prefix
        echo "\$MAW_PYTHON"         # the venv's python

    Optional, for the wilkins engine or adios2 mpmd -- see INSTALL.md:
        module load <the MPI your domains use>
        cd spack_mpi && cp spack.yaml.in spack.yaml
        spack -e . external find openmpi mpich intel-oneapi-mpi mvapich2
        spack -e . install && spack -e . module lmod refresh -y
DONE