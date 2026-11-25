"""Project constants.

Paths are absolute. Once agents run on other nodes this is not optional:
worker_init puts each worker in tmp/run_N, so a relative
"inputs/literature/foo.pdf" has nothing to resolve against and the Planner's
PdfReader call fails with FileNotFoundError.

PROJECT_DIR is the root everything else follows from. It resolves to this
checkout; set MAW_PROJECT_DIR to point it somewhere else.
"""

import os

# __ Paths ______________________________________________________________________

# The repo root, derived from this file's own location (src/common/consts.py
# -> two directories above src/common/). Absolute, so the workers' cwd cannot
# affect it.
#
# Not a hardcoded literal: every other module already resolves the repo from
# __file__ -- _SKILLS_ROOT in mcp_explorer.py, repo_root in
# REPO_ROOT in src/servers/*.py. A literal here only agrees with those on the one
# machine it names, and disagreeing silently is worse than either: the agents
# read skills and write requirements under one root while the MCP servers
# resolve /app/ to another.
#
# MAW_PROJECT_DIR overrides it for the case the literal was there to serve --
# running the code from somewhere other than the checkout.
# THREE levels up, not two. This file lives in src/common/, so "the directory
# above common/" is src/ -- which would make SRC_DIR src/src, INPUTS_PATH
# src/inputs, WORK_DIR src/work, and SPACK_ENV_PATH src/ (where there is no
# spack.yaml). Every one of those is a directory that does not exist, and none
# of them errors on read: a missing work/ is created on first write, a missing
# inputs/ just lists empty. The symptom would be a run whose outputs and inputs
# are in the wrong place, succeeding.
PROJECT_DIR = os.environ.get(
    "MAW_PROJECT_DIR",
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
)

SRC_DIR = f"{PROJECT_DIR}/src"

# The three input directories live under one parent. They were data/,
# Literature/ and images/ at the repo root -- three siblings of the code, with
# nothing marking them as inputs rather than outputs, and an unused inputs/
# alongside them. Consolidating also means the agents' /app/ aliases name one
# tree: /app/inputs/data, /app/inputs/literature, /app/inputs/images.
INPUTS_PATH = f"{PROJECT_DIR}/inputs"
WORK_DIR = f"{PROJECT_DIR}/work"
LITERATURE_PATH = f"{INPUTS_PATH}/literature"
SKILLS_PATH = f"{PROJECT_DIR}/skills"
DATA_PATH = f"{INPUTS_PATH}/data"
RUN_PATH = f"{PROJECT_DIR}/runs"
IMAGE_PATH = f"{INPUTS_PATH}/images"
# Under work/, not at the repo root, and therefore GENERATED: the installer
# writes requirements.txt here from stack_decision on every run, and always
# regenerates it -- a file left from a previous run is how a stale package
# reaches the venv. Nothing reads it between runs, so there is nothing to
# keep, and work/ is already gitignored.
#
# MAW_WORK_DIR wins, so a run with its work directory elsewhere puts its
# builds there too rather than back in the checkout.
BUILDS_PATH = f"{WORK_DIR}/builds"
# The Spack environment the Explorer's compute node activates. spack.yaml and
# repo.yaml live at the repo root, so the repo root is the environment. There is no
# setup script any more: `spack env activate` runs maw.env's
# setup_run_environment -- the compiler, JAVA_HOME, COMPSS_HOME, MAW_PYTHON,
# LD_LIBRARY_PATH, MODULEPATH, the PMI scrub -- which is the whole of what an
# activate script would have done. bootstrap.sh runs once per machine and
# only does what cannot bootstrap itself: install Spack, and make the venv.
SPACK_ENV_PATH = PROJECT_DIR

# Sourced before activating, on a worker, because `spack` is a shell function
# and the `module purge` in the worker prelude may have removed whatever
# defined it. Nothing inside Spack can source Spack -- that is the one thing
# that genuinely cannot be expressed as Spack configuration.
#
# Empty means "already on PATH"; set MAW_SPACK_SETUP in .env if the worker
# needs an explicit path to share/spack/setup-env.sh.
SPACK_SETUP_PATH = os.environ.get("MAW_SPACK_SETUP", "")

# __ Engine environment: deliberately absent ____________________________________
# Nothing about PyCOMPSs, ADIOS2, MPI, Java, libxml2 or headless rendering is
# written down here any more.
#
# All of it used to be: COMPSS_HOME, JAVA_HOME, LIBXML2_LIB, ADIOS2_HOME,
# GCC14_LIB, OPENMPI_BIN, MPI_LIB_PATHS, GUI_OFFSCREEN_ENV and
# SINGLE_RANK_MPI_SHIMS, each with a hardcoded Improv/Swing path as its default.
# A default path is a claim about a machine, and every one of those claims was
# true on exactly one cluster -- while being wrong silently, because a
# nonexistent directory on LD_LIBRARY_PATH is not an error, it is just a lookup
# that finds nothing.
#
# The servers in src/servers/ now read what they need from the environment and
# resolve nothing: see the header of src/servers/server.py for the list. Export them
# from the setup script sourced before launch, which is the one place that knows
# which machine this is. Anything that script does not export is a path the
# servers will not have, and that is the intended failure mode -- loud, and at
# the right layer.


# No VENV_DIR here on purpose: the virtualenv is not a constant. The
# workers activate whichever one the launching process is running in,
# which academy_agents.spack_env_dir() reads from SPACK_ENV.


# __ ADIOS2 variant _____________________________________________________________

ADIOS_DEFAULT_VARIANT = "serial"
ADIOS_VARIANTS = ("serial", "mpi")


def adios_variant(value: str | None = None) -> str:
    """MAW_ADIOS as exactly "serial" (the default) or "mpi".

    The one expression both sides of the MCP boundary use: the client binds the
    engine-only tools from it and the server picks its class from it, so they
    cannot disagree. Unrecognised falls back to serial, which runs everywhere.
    """
    raw = os.environ.get("MAW_ADIOS", "") if value is None else value
    normalised = (raw or "").strip().lower()
    return normalised if normalised in ADIOS_VARIANTS else ADIOS_DEFAULT_VARIANT


# __ Skills _____________________________________________________________________

ENV_KNOWLEDGE = {
    "local":      "knowledge/local",
    "hpc":        "knowledge/lcrc",
    # multi-node is LCRC under PBS like hpc, but the allocation is not the one
    # you hold -- this code submits it, and the Explorer's is one node wider
    # than its MPI application may use. Agents sizing from get_resources alone
    # get that wrong. NOTE: knowledge/multi_node_lcrc is not written yet; this
    # key is currently never looked up because make_state passes env="hpc" for
    # multi-node, so it is inert until both land together.
    "multi-node": "knowledge/multi_node_lcrc",
}

# __ Use-case skill redirects ___________________________________________________
# Empty, and kept only so that `from common.consts import USE_CASE_SKILL_REDIRECTS`
# keeps working. Nothing is redirected any more.
#
# It used to map use_cases/<key>/<agent> to a consolidated file, so that asking
# for cosmology's per-agent skills quietly got you new_cosmology/domain instead.
# That existed because the folder was chosen by matching a --domain label rather
# than being named outright. It is now named outright: `--domain new_cosmology`
# loads that folder, and `--domain cosmology` loads the other one, with no
# table in between deciding which you actually meant.
#
# Add an entry only to make one folder's skill requests resolve to a different
# file on purpose -- and prefer passing the folder you want.
USE_CASE_SKILL_REDIRECTS: dict[str, str] = {}


# __ Condition-A substitute for env knowledge ___________________________________
# B/C load the real knowledge/local|lcrc skill file, unchanged. A doesn't get that
# file, just these few bare facts about the machine, so it's not permanently stuck.
ENV_NOTES = {
    "local": (
        "Environment: a single local machine, no job scheduler. No PBS/LSF, no multi-node "
        "MPI. /app/ paths are resolved to the repo root at runtime."
    ),
    "hpc": (
        "Environment: an HPC cluster, presumably inside a job allocation. /app/ paths are "
        "resolved to the repo root at runtime; never hardcode cluster-specific absolute paths."
    ),
    "multi-node": (
        "Environment: an HPC cluster under PBS. The Explorer's allocation was submitted by "
        "the agent code itself, and the allocation is exactly as wide as what its MPI application "
        "may use, so size mpiexec from the MPI capacity stated in the task context, not from "
        "get_resources. /app/ paths are resolved to the repo root at runtime; never hardcode "
        "cluster-specific absolute paths."
    ),
}


# __ PBS Pro constants __________________________________________________________

# Modules the Explorer's worker loads before activating the venv, passed
# verbatim to `module load`. EMPTY BY DESIGN -- the load line is skipped
# entirely when this is blank.
#
# It used to be "gcc/13.2.0 openmpi/4.1.7". CORRECTION to what this comment
# previously claimed: openmpi/4.1.7 DOES exist on Improv -- it is in
# custom-modules, alongside 4.1.8, visible from Core with no compiler loaded.
# The original reason given here was false.
#
# The real problem is worse than a bad name. openmpi/4.1.7 in custom-modules
# is a DIFFERENT install from openmpi/4.1.6-gcc-13.2.0 in
# spack-modules/gcc/13.2.0. Loading either one in the worker would put an
# Open MPI into an environment that is meant to hold none.
#
# And loading any MPI here contradicts how MPI is now provided: a task loads
# the module its own tool needs, inside its own command string, and a module
# already loaded in the worker is inherited by every task -- including the
# ones that want a different one, or none at all. Two Open MPI majors in one
# process hang rather than error, and both ship libmpi.so.40, so nothing
# catches it at load time.
#
# The worker itself compiles nothing and launches nothing directly: it runs
# the MCP server, which runs the generated workflow file, whose tasks load
# what they need. If a future engine genuinely needs a module in the worker
# environment, name it here -- but not an MPI.
HPC_MODULES = ""

# Removed: DEFAULT_NCPUS. It reserved cores for a Planner/Installer placement
# mode that no longer exists -- both run in the launching process, on the login
# node. Nothing requests a core count anywhere now; the Explorer's allocation
# asks for nodes and mpiprocs only.


# PBS walltime for every block. One value, used by both.
#
# This bounds the entire run: when it expires the agents die mid-workflow,
# and in distributed placement that includes the Planner and Installer
# waiting on the Explorer. The 8-rank reference run spent about 6.5
# minutes inside the explorer session, so an hour leaves room for LLM
# latency and a revision loop or two -- but not for a scaled-up HACC.
DEFAULT_WALLTIME = "01:00:00"

# Cores per compute node on this machine. Reference only -- nothing imports it,
# and nothing should size an allocation from it. This number is here to answer
# "how much is on a node", not to be passed to anything.
#
# (This comment used to claim the block requests no ncpus. It does: make_config
# passes cpus_per_node=<ranks>, which the provider renders as ncpus. Two other
# places said the same wrong thing and have been corrected -- if you change the
# provider call, change all three.)
CORES_PER_NODE = 128