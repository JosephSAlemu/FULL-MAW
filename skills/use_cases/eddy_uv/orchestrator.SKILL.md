---
name: use_cases/eddy_uv/orchestrator
description: >
  Eddy_uv (Nek5000 CFD) routing facts for the orchestrator -- running the producer
  under MPI inside the existing allocation (no qsub exception anymore),
  requirements approval, and cluster-specific error patterns (rank-count confusion,
  field-file naming, the err-prefixed diagnostic file).
---

# Eddy_uv (Nek5000 CFD) — Orchestrator Skill

Routing rules specific to the Nek5000 `eddy_uv` workflow.

---

## When to Use This Skill

Load when orchestrating a Nek5000 CFD workflow (goal mentions Nek5000, Walsh
(1992), eddy_uv, or paths under `/lcrc/project/PEDAL/jalemu/Nek5000/`).

---

## Flow for This Project

```
planner -> installer -> explorer -> end
```
Same shape as other use cases.

---

## No `qsub` Exception — Producer Runs in the Existing Allocation

There used to be an explicit exception here letting the explorer submit a brand-new
PBS batch job for the Nek5000 producer. That's gone: the producer now runs under MPI
inside the same interactive allocation as everything else in the run (including the
field-file analysis/visualization stages), exactly like the general LCRC rule every
other use case already follows. If the explorer calls `qsub` for this project, or
writes a `.pbs`/`.sh` script to launch the producer, that's a violation to flag --
route back with feedback to let the workflow engine launch it instead.

---

## Requirements Approval

When the installer presents `requirements.txt`, confirm `pymech` is present
alongside `numpy`/`matplotlib` -- it's the package that reads Nek5000 field
files and is easy to forget since it's not a household name.

**Do NOT approve** anything that tries to pip-install or rebuild `nek5000`
itself -- it's a pre-built cluster binary, not a Python package.

**Do NOT approve** a stale requirements.txt carrying over packages from other
use cases (`scipy`, `mpi4py`, `ovito`, `pillow`, `lammps`, `adios2`) unless the
current task list actually calls for them.

---

## Error Pattern Recognition

| Error pattern | Route to | Feedback |
|---|---|---|
| `ModuleNotFoundError: No module named 'pymech'` | installer | "Add `pymech` to requirements.txt -- it's pip-installable and not yet present." |
| Explorer calls `qsub`/`qstat`, or writes a `.pbs`/`.sh` launcher for this project | explorer | "Do not submit a new PBS job or write a launcher script -- have the workflow engine run `./nek5000` with 8 MPI ranks in eddy_uv/ inside the existing allocation, same as every other use case." |
| Explorer assumes the producer needs 32 MPI ranks | explorer | "That was a headroom quirk in the original subeddy.pbs (select=1:mpiprocs=32 vs. mpiexec -np 8). Use 8 ranks -- 8 unless the task says otherwise." |
| The producer fails with a launcher/env error | explorer | "Source bashrc.improv.cpu before ./nek5000 in the same command, and confirm the working directory is eddy_uv/." |
| Explorer can't find field files / globs the wrong pattern | explorer | "Field files are 5-digit zero-padded (eddy_uv0.f00001..f00011) -- check eddy_uv.nek5000's filetemplate rather than assuming 4 digits." |
| Explorer's per-timestep loop picks up an extra file | explorer | "erreddy_uv0.f00001 is a separate Walsh-exact-solution error diagnostic, not part of the 11-file series -- glob eddy_uv0.f* specifically." |
| Explorer reports results with no PNG produced | explorer | "This use case's deliverable is a rendered visualization, not just computed arrays -- add a rendering/plotting task and re-run it." |
| All other failures | explorer | Full stderr content |

---

## Notes

- After a successful run (producer completed, visualization PNG(s) produced,
  summary written), route to "end".
- Never route back to planner unless the task list itself is structurally
  wrong (e.g. missing a rendering step) -- config-value or path mistakes
  should go back to explorer with feedback to re-check the case files, not to
  planner.
