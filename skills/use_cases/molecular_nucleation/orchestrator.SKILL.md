---
name: use_cases/molecular_nucleation/orchestrator
description: >
  Molecular nucleation routing rules for the orchestrator. Covers the venv setup flow,
  LAMMPS executor error pattern recognition, and requirements.txt approval behavior.
---

# Molecular Nucleation — Orchestrator Skill

Routing rules specific to the water crystallization / LAMMPS workflow.

---

## When to Use This Skill

Load when orchestrating a molecular nucleation workflow. Overrides generic routing heuristics with LAMMPS-specific error pattern recognition.

---

## Flow for This Project

```
planner → installer → explorer → end
```

The installer sets up the local venv (pip install from requirements.txt). Route to installer after planner completes.

---

## Executor Error Pattern Recognition

When the explorer reports tool call failures, use these patterns to decide where to route:

| Error pattern | Route to | Feedback |
|---|---|---|
| `exit 143` on run_lammps | explorer | "MPI init failure — run_lammps should handle this automatically via server TASK_ENV. Check that get_resources was called first and in_pbs is true." |
| `WorkerLost` + `MPI` / `ORTE` | explorer | "LAMMPS MPI init failure. The run_lammps tool handles HPC/local selection — do not use submit_task with Python API for LAMMPS." |
| `ModuleNotFoundError: No module named 'lammps'` | explorer | "LAMMPS not found. Use `from lammps import lammps` — the source build is at /usr/local/lib. Do not pip install lammps." |
| `ModuleNotFoundError: No module named 'PIL'` | explorer | "pillow is installed as 'pillow' not 'PIL'. Import with `from PIL import Image`." |
| `frames/step.*.lammpstrj` not found / no frames | explorer | "LAMMPS did not produce dump files. Ensure os.chdir(work_dir) is called BEFORE lammps() and that work_dir/frames/ exists." |
| `results.csv` missing after exit 0 | explorer | "analyze_with_ovito did not write results.csv. Check the output_csv path and that pipeline.compute() loop runs." |
| All other failures | explorer | Full stderr content |

---

## Requirements Approval

When the installer presents requirements.txt for approval, verify it contains:
- `ovito`
- `numpy`
- `matplotlib`
- `pillow`
- The engine-specific package matching `--engine`:
  - `parsl`: `parsl>=2024.0.0` should be present
  - `pycompss`: `pycompss` should NOT be present (never pip-installed — see `systems/pycompss` skill)
  - `adios`: `adios2` may or may not appear — approve either way (optional, has numpy fallback)

**Do NOT approve** requirements.txt that includes `lammps` as a pip package — LAMMPS must be source-built (serial, `BUILD_MPI=off`). If `lammps` appears in the list, reject with feedback to remove it.

---

## Routing Examples

**LAMMPS succeeded, OVITO failed:** explorer_complete, frames exist but results.csv missing -> `next="explorer"`, `feedback="OVITO analysis failed. Verify ovito is installed and that frames exist in /app/work/run0/frames/ before retrying analysis."`

**LAMMPS failed exit 143:** explorer_complete, no frames -> `next="explorer"`, `feedback="run_lammps returned exit 143 (MPI SIGTERM). Check that get_resources was called first and in_pbs is true. Do not use submit_task for LAMMPS."`

**Missing input file:** explorer_complete, cp failed -> `next="explorer"`, `feedback="Input file copy failed. Verify AW.tersoff, data.init, and in.watbox all exist in /app/data/."`

---

## Notes

- After a successful run (exit 0, results.csv and animation.gif present), route to "end"
- Explorer revision trigger: any tool call failure that is not a missing pip package
- Never route back to planner unless the task description itself was wrong (rare)
