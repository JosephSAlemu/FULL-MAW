---
name: use_cases/eddy_uv/installer
description: >
  Installer environment facts for the eddy_uv (Nek5000 CFD) project -- the one
  package (pymech) that isn't obvious from general knowledge, and why
  nek5000 itself must never be rebuilt or pip-installed.
---

# Eddy_uv (Nek5000 CFD) — Installer Skill

Sets up the local venv for the eddy_uv workflow. Same two-phase flow as the
base installer skill (`agents/installer`): Phase 1 generates
`builds/requirements.txt`, orchestrator approves, Phase 2 installs.

---

## The One Non-Obvious Package

```
pymech        # reads Nek5000 .f##### field files; pip-installable
```
`numpy`/`matplotlib` are standard and will already be obvious from the task
list. `pymech` is the one package an installer wouldn't otherwise know to add
-- it's the only way to read Nek5000's binary field-file format without
hand-parsing it.

---

## Never Pip-Install or Build

- **`nek5000`** (the case executable) -- already compiled in the case
  directory (`build.log` confirms it). It's a pre-built cluster binary, not a
  Python package. Never attempt to rebuild it via `makenek` as part of a task.

---

## Notes

- No source build step is needed for this use case -- unlike
  `molecular_nucleation`'s LAMMPS build, everything Nek5000-related is already
  built on the cluster.
- Don't carry over packages from other use cases (`scipy`, `mpi4py`, `ovito`,
  `pillow`, `lammps`, `adios2`) unless the actual task list calls for them.
