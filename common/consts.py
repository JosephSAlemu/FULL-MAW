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

# No VENV_DIR here on purpose: the virtualenv is not a constant. The
# workers activate whichever one the launching process is running in,
# which academy_agents.venv_dir() reads from sys.prefix.


# __ Skills _____________________________________________________________________

ENV_KNOWLEDGE = {
    "local":      "knowledge/local",
    "hpc":        "knowledge/lcrc",
    # multi-node is LCRC under PBS like hpc, but the allocation is not the one
    # you hold -- this code submits it, in two separate blocks, and the
    # Explorer's is one node wider than its MPI application may use. Agents
    # sized from get_resources alone get that wrong, so multi-node gets its own
    # knowledge file rather than reusing knowledge/lcrc.
    "multi-node": "knowledge/multi_node_lcrc",
}

# __ Use-case skill redirects ___________________________________________________
# Side-by-side comparison hook: instead of the per-agent files under
# use_cases/cosmology/ (planner/orchestrator/explorer/installer), every agent
# loads the single consolidated use_cases/new_comsology/domain file for cosmology.
# Any skill request matching use_cases/<key>/<agent> is rewritten to the value.
USE_CASE_SKILL_REDIRECTS = {
    "cosmology":            "use_cases/new_comsology/domain",
    "molecular_nucleation": "use_cases/new_molecular_nucleation/domain",
}

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
        "Environment: an HPC cluster under PBS. This run holds two allocations submitted by "
        "the agent code itself -- one node for the orchestrator/planner/installer, and a "
        "separate, wider one for the explorer. The explorer's allocation carries one spare "
        "node beyond what its MPI application may use, so size mpiexec from the MPI capacity "
        "stated in the task context, not from get_resources. /app/ paths are resolved to the "
        "repo root at runtime; never hardcode cluster-specific absolute paths."
    ),
}


# __ PBS Pro constants __________________________________________________________

# Modules the workers load before activating the venv, passed verbatim to
# `module load`. Site-specific: these names are Improv's.
HPC_MODULES = "gcc/13.2.0 openmpi/4.1.7"

# CPUs per node for the Orchestrator, Planner and Installer block, used only
# under --env multi-node with --agent-placement distributed, and the default
# for --agent-ncpus. One core is enough for LLM-latency-bound work, but the
# Installer shells out to pip, which builds wheels single-threaded -- raise
# this if installs are slow or time out.
#
# Improv compute nodes report 128 cores and ~251 GB, and the site
# allocates whole nodes, so this does not change what the block costs. It
# only bounds what the agents may use.
DEFAULT_NCPUS = 4

# PBS walltime for every block. One value, used by both.
#
# This bounds the entire run: when it expires the agents die mid-workflow,
# and in distributed placement that includes the Planner and Installer
# waiting on the Explorer. The 8-rank reference run spent about 6.5
# minutes inside the explorer session, so an hour leaves room for LLM
# latency and a revision loop or two -- but not for a scaled-up HACC.
DEFAULT_WALLTIME = "01:00:00"

# Cores per compute node on this machine. Reference only -- nothing
# imports it. Useful when sizing --ncpus: the generated workflow runs
# `mpiexec --map-by ppr:<ranks>:node:PE=<threads>`, so
# ranks_per_node * threads_per_rank must fit inside the ncpus reserved.
# The 8-rank reference run needs 8 x 4 = 32.
CORES_PER_NODE = 128