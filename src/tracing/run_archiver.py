"""
Run Archiver -- snapshots a completed run's output and trace into a permanent,
self-describing folder outside the repo, then clears work/run0 for the next run.

work/run0/ is a single fixed working directory shared by every run (see
DEFAULT_WORK_DIR in src/servers/parsl_server.py and src/servers/pycompss_server.py) --
it gets overwritten by whatever the next run does. archive_run() is the one
place that captures a permanent copy before that happens.

Usage (called from academy_agents.py after tracer.save(trace_path), on both the
success and failure paths):

    from tracing.run_archiver import archive_run
    archive_path = archive_run(tracer.run_metadata, trace_path)
"""

import os
import re
import shutil
import sys

from .trace_schema import RunMetadata

# Lives outside the repo: committing MCP_Approach should never drag run output
# along, and the eventual Artifact ("single") approach will archive alongside
# it under a sibling folder so the two never collide.
# MAW_ARCHIVE_ROOT, not a literal. The old default named one project
# directory on one cluster; anywhere else it is a path makedirs happily creates
# in the wrong place, or cannot create at all -- and archive_run never raises,
# so the failure is one warning on stderr at the very end of a run.
#
# Unset means archiving is OFF, which is the right default for a machine that
# has no shared project directory: a run that is not archived is better than a
# run archived into someone's home directory by accident.
ARCHIVE_ROOT = os.environ.get("MAW_ARCHIVE_ROOT", "")

# PROJECT_DIR, not __file__. This module lives in src/tracing/ now, so
# __file__-relative would make _WORK_DIR src/tracing/work/run0 -- a directory
# that does not exist. shutil.copytree on a missing source raises, archive_run
# catches everything, and the run would be archived WITHOUT its outputs and
# with only a warning to say so.
try:
    from common.consts import PROJECT_DIR as _REPO_ROOT
except ImportError:
    _REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))
_WORK_DIR = os.environ.get("MAW_WORK_DIR",
                           os.path.join(_REPO_ROOT, "work", "run0"))

# Engine bookkeeping, not run output: Parsl's run_dir and block logs, the
# per-launch stdout/stderr temp files, COMPSs's generated project/resources
# XML. Kept out of the archive because it is most of the tree -- 90% of the
# files in the sample cosmology run -- and copying it to the archive root on
# GPFS is the last thing a run does before it finishes. Mirrors _SCRATCH_DIRS
# in mcp_explorer.py, which prunes the same names from the artifact manifest.
_SCRATCH_DIRS = (".parsl", "runinfo", "_task_scripts", "__pycache__", "_compss_config")


def _slugify(value: str) -> str:
    """Filesystem-safe segment for the archive folder name. Mirrors the
    _slugify helper in academy_agents.py -- duplicated rather than imported to
    avoid a circular import (academy_agents.py imports archive_run from here)."""
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "_", value or "").strip("_")
    return slug or "unknown"


def _folder_name(metadata: RunMetadata) -> str:
    date = metadata.run_id[:8] if len(metadata.run_id) >= 8 else metadata.run_id
    usecase = metadata.domain or metadata.paper_id
    return "__".join([
        _slugify(date),
        _slugify(usecase),
        _slugify(metadata.framework),
        _slugify(metadata.condition),
        _slugify(metadata.combination),
        f"trial{metadata.trial}",
    ])


def _unique_dest(root: str, name: str) -> str:
    """Windows-style collision handling: name, then 'name (2)', 'name (3)', ..."""
    dest = os.path.join(root, name)
    if not os.path.exists(dest):
        return dest
    n = 2
    while True:
        candidate = os.path.join(root, f"{name} ({n})")
        if not os.path.exists(candidate):
            return candidate
        n += 1


def archive_run(metadata: RunMetadata, trace_path: str) -> str:
    """Copy work/run0 + trace.json into ARCHIVE_ROOT/<slug>/.

    Called on both success and failure so a failed run's diagnostics are
    never silently dropped. Never raises -- a broken archiver should not
    take down a real run's exit status. Returns the archive folder path,
    or "" if archiving failed.

    Does NOT clear work/run0 afterward -- it used to (wipe + recreate, gated
    behind a successful copy), but since work/run0 is a single fixed directory
    shared by every run, the next run's own setup tasks (re-copying input
    files, the workflow's own pre-run frame cleanup, etc.) are what's actually
    responsible for not stepping on stale output, not a blanket wipe here.
    Leaving the directory alone after archiving means a run is never one
    archive-step bug away from losing both the copy and the original.
    """
    try:
        if not ARCHIVE_ROOT:
            print("[run_archiver] MAW_ARCHIVE_ROOT is unset -- not archiving. "
                  "Set it to a directory outside the repo to enable archiving.",
                  file=sys.stderr)
            return ""
        os.makedirs(ARCHIVE_ROOT, exist_ok=True)
        dest = _unique_dest(ARCHIVE_ROOT, _folder_name(metadata))
        os.makedirs(dest)

        if os.path.isdir(_WORK_DIR):
            shutil.copytree(_WORK_DIR, os.path.join(dest, "work"),
                            ignore=shutil.ignore_patterns(*_SCRATCH_DIRS))
        if os.path.isfile(trace_path):
            shutil.copy2(trace_path, os.path.join(dest, "trace.json"))

        # Post-run diagnosis output, written by the orchestrator when the user
        # reported the run as unsuccessful. Absent on a clean run.
        recs_path = os.path.join(
            os.path.dirname(trace_path),
            metadata.run_id + "_skill_recommendations.md",
        )
        if os.path.isfile(recs_path):
            shutil.copy2(recs_path, os.path.join(dest, "skill_recommendations.md"))

        return dest
    except Exception as e:
        print(f"[run_archiver] WARNING: failed to archive run {metadata.run_id}: {e}",
              file=sys.stderr)
        return ""