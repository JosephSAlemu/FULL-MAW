LITERATURE_PATH = "Literature/"
SKILLS_PATH = "skills/"
DATA_PATH = "data/"
RUN_PATH = "runs/"
IMAGE_PATH = "images/"

ENV_KNOWLEDGE = {
    "local": "knowledge/local",
    "hpc":   "knowledge/lcrc",
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
