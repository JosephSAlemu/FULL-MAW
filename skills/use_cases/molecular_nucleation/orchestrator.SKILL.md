---
name: use_cases/molecular_nucleation/orchestrator
description: >
  Molecular nucleation routing rules for the orchestrator. Covers the venv setup flow,
  the one-workflow-file rule, LAMMPS error pattern recognition, and requirements.txt
  approval behavior.
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

## One Workflow File — Verify This

Setup, LAMMPS, OVITO analysis, rendering, GIF assembly, and the timeseries plot are
written into **one** generated workflow file and executed by the engine. If the
explorer invokes the CLI itself, opens a subprocess from its own tools, writes a
`.sh`/`.pbs` launcher, calls `qsub`, or splits the pipeline across several
executions, that is a violation to flag and route back.

(The ADIOS2 engine is the one place `subprocess` is legitimate — inside a stage
function body of the generated file, never at module top level and never from the
explorer's own tools.)

---

## Executor Error Pattern Recognition

When the explorer reports failures, use these patterns to decide where to route:

| Error pattern | Route to | Feedback |
|---|---|---|
| `exit 143` running LAMMPS | explorer | "MPI init SIGTERM. Confirm get_resources was called first and in_pbs is true, and that PMI_SIZE, PMI_RANK and I_MPI_HYDRA_BOOTSTRAP are unset before mpirun in the generated workflow file." |
| `WorkerLost` + `MPI` / `ORTE` | explorer | "LAMMPS MPI init failure. Run the cluster module's lmp under mpirun from the workflow file with the Intel-MPI singleton vars unset, or fall back to the single-process Python API path if there is no launcher." |
| `elf` / segment-layout load error on `lmp` | explorer | "The pip lammps wheel's bundled lmp binary does not load on this cluster's kernel. Use `module load lammps/22Jul2025` (gcc 13.2.0 + OpenMPI 5.0.6) and run that lmp." |
| `module: command not found` | explorer | "`module` is a shell function — the command needs a login shell (`bash -lc \"...\"`) before `module load lammps/22Jul2025`." |
| `ModuleNotFoundError: No module named 'lammps'` | explorer | "LAMMPS not found. Use `from lammps import lammps` — the source build is at /usr/local/lib. Do not pip install lammps." |
| `ModuleNotFoundError: No module named 'PIL'` | explorer | "pillow is installed as 'pillow' not 'PIL'. Import with `from PIL import Image`." |
| LAMMPS `exit 11` reported as a failure, frames present | explorer | "Exit 11 is a SIGSEGV during LAMMPS cleanup. Trajectory frames were written, so the step succeeded — have the step check for frames/*.lammpstrj and continue instead of failing." |
| `frames/step.*.lammpstrj` not found / no frames | explorer | "LAMMPS did not produce dump files. Ensure the working directory is /app/work/run0 (os.chdir before lammps() on the Python API path, cd/working_dir on the MPI path) and that work_dir/frames/ exists." |
| Frame count unchanged / frames look stale | explorer | "Delete frames/*.lammpstrj at the start of the LAMMPS step so old output can't be mistaken for new." |
| `results.csv` missing after a successful run | explorer | "The OVITO analysis step did not write results.csv. Check the output_csv path and that the pipeline.compute() loop runs." |
| Workflow file rejected by `write_workflow` | explorer | "Read the returned `errors` and fix the file — do not route around the rejection with another approach." |
| Explorer calls the CLI/subprocess directly, or writes a `.sh`/`.pbs` script or calls `qsub` | explorer | "You never execute anything yourself. Every step belongs in the one generated workflow file that the engine runs." |
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

**Do NOT approve** requirements.txt that includes `lammps` as a pip package — LAMMPS must be source-built (serial, `BUILD_MPI=off`) locally, and on this cluster comes from the `lammps/22Jul2025` module. If `lammps` appears in the list, reject with feedback to remove it.

---

## Routing Examples

**LAMMPS succeeded, OVITO failed:** explorer_complete, frames exist but results.csv missing -> `next="explorer"`, `feedback="OVITO analysis failed. Verify ovito is installed and that frames exist in /app/work/run0/frames/ before retrying analysis."`

**LAMMPS failed exit 143:** explorer_complete, no frames -> `next="explorer"`, `feedback="LAMMPS returned exit 143 (MPI init SIGTERM). Check get_resources was called first and in_pbs is true, that the workflow loads the lammps/22Jul2025 module in a login shell, and that PMI_SIZE, PMI_RANK and I_MPI_HYDRA_BOOTSTRAP are unset before mpirun."`

**LAMMPS exit 11, frames present:** explorer_complete, reported as failure -> `next="explorer"`, `feedback="Exit 11 is a cleanup SIGSEGV, not a run failure. frames/*.lammpstrj were written — continue to the OVITO analysis instead of retrying the simulation."`

**Missing input file:** explorer_complete, copy failed -> `next="explorer"`, `feedback="Input file copy failed. Verify AW.tersoff, data.init, and in.watbox all exist in /app/data/."`

---

## Notes

- After a successful run (results.csv and animation.gif present), route to "end"
- Explorer revision trigger: any failure that is not a missing pip package
- Never route back to planner unless the task description itself was wrong (rare)
