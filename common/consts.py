"""Project constants.

Paths are absolute. Once agents run on other nodes this is not optional:
worker_init puts each worker in tmp/run_N, so a relative
"Literature/foo.pdf" has nothing to resolve against and the Planner's
PdfReader call fails with FileNotFoundError.

PROJECT_DIR is the root everything else follows from. It resolves to this
checkout; set MAW_PROJECT_DIR to point it somewhere else.
"""

import os

# __ Paths ______________________________________________________________________

# The repo root, derived from this file's own location (common/consts.py ->
# the directory above common/). Absolute, so the workers' cwd cannot affect it.
#
# Not a hardcoded literal: every other module already resolves the repo from
# __file__ -- _SKILLS_ROOT in mcp_explorer.py, _REPO_ROOT in run_archiver.py,
# REPO_ROOT in servers/*.py. A literal here only agrees with those on the one
# machine it names, and disagreeing silently is worse than either: the agents
# read skills and write requirements under one root while the MCP servers
# resolve /app/ to another.
#
# MAW_PROJECT_DIR overrides it for the case the literal was there to serve --
# running the code from somewhere other than the checkout.
PROJECT_DIR = os.environ.get(
    "MAW_PROJECT_DIR",
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
)

SRC_DIR = f"{PROJECT_DIR}/src"
WORK_DIR = f"{PROJECT_DIR}/work"
LITERATURE_PATH = f"{PROJECT_DIR}/Literature"
SKILLS_PATH = f"{PROJECT_DIR}/skills"
DATA_PATH = f"{PROJECT_DIR}/data"
RUN_PATH = f"{PROJECT_DIR}/runs"
IMAGE_PATH = f"{PROJECT_DIR}/images"
BUILDS_PATH = f"{PROJECT_DIR}/builds"
SETUP_SCRIPT_PATH = f"{PROJECT_DIR}/setup_hpc.sh"

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
# The servers in servers/ now read what they need from the environment and
# resolve nothing: see the header of servers/server.py for the list. Export them
# from the setup script sourced before launch, which is the one place that knows
# which machine this is. Anything that script does not export is a path the
# servers will not have, and that is the intended failure mode -- loud, and at
# the right layer.


# No VENV_DIR here on purpose: the virtualenv is not a constant. The
# workers activate whichever one the launching process is running in,
# which academy_agents.venv_dir() reads from sys.prefix.


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
# for cosmology's per-agent skills quietly got you new_comsology/domain instead.
# That existed because the folder was chosen by matching a --domain label rather
# than being named outright. It is now named outright: `--use-case new_comsology`
# loads that folder, and `--use-case cosmology` loads the other one, with no
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

# Modules the workers load before activating the venv, passed verbatim to
# `module load`. Site-specific: these names are Improv's.
HPC_MODULES = "gcc/13.2.0 openmpi/4.1.7"

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
# and nothing should size an allocation from it. The Explorer's block requests
# `select=<nodes>:mpiprocs=<ranks>` with no ncpus, so PBS applies its own
# per-chunk default and rank placement is decided by mpiprocs alone. This number
# is here to answer "how much is on a node", not to be passed to anything.
CORES_PER_NODE = 128