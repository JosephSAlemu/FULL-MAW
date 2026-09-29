LITERATURE_PATH = "Literature/"
SKILLS_PATH = "skills/"
DATA_PATH = "data/"
RUN_PATH = "runs/"
IMAGE_PATH = "images/"

ENV_KNOWLEDGE = {
    "local": "knowledge/local",
    "hpc":   "knowledge/lcrc",
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
}
