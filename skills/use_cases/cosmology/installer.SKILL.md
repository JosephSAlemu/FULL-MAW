---
name: use_cases/cosmology/installer
description: >
  Installer behavior for the HACC cosmology project. Covers the minimal
  pip-installable package set, why HACC/GenericIO/pygio must never be
  pip-installed or built, and the optional adios2 dependency.
---

# Cosmology (HACC) — Installer Skill

Sets up the local venv with the minimal packages needed for the HACC cosmology workflow.

---

## Current Installer Behavior

Same two-phase flow as the base installer skill (`agents/installer`):
1. **Phase 1:** Read or generate `builds/requirements.txt` from `stack_decision`.
2. **Orchestrator:** Reviews and approves.
3. **Phase 2:** Skip packages already installed; `pip install` the rest.

---

## Package Requirements

```
numpy
matplotlib
mpi4py        (only if environment knowledge confirms MPI is available)
```

That's the full requirement for the workflow to run end-to-end (snapshot/halo reading
goes through CLI binaries via `subprocess`, not Python bindings).

**Optional:** `adios2` — only relevant if `--engine adios` is selected. If it's in
`stack_decision`, try installing it (`pip install adios2`), but a failure here is not
fatal: the workflow has a numpy-I/O fallback (see `systems/adios` skill), so do not
block or fail the run if `adios2` can't be installed. There's no system-level ADIOS2
build assumed on this cluster for this use case yet.

**Do NOT add:** `ovito`, `pillow`, `lammps`, `parsl` — those are for
`molecular_nucleation`, not this project. If `builds/requirements.txt` already has
them from a prior run with a different engine, that's stale — it should be regenerated
from the current `stack_decision`, not reused as-is.

---

## Never Pip-Install or Build These

- **`pygio`** — in-tree at `HACC_go/submodules/genericio/python/pygio`, missing its
  compiled extension (`ModuleNotFoundError: No module named 'pygio._version'`).
  Building it means compiling a C++ extension against the GenericIO libs — a real
  installer risk. The workflow deliberately avoids it; reading happens via the
  pre-built `GenericIOPrint` CLI binary instead.
- **HACC itself** (`hacc_tpm`, `hacc_slice`, etc.) — pre-built cluster binaries under
  `HACC_go/improv.cpu/{mpi,frontend}/bin/`. Never attempt to build or install these;
  they are not Python packages and are treated as an opaque black box.

---

## Notes

- No source build step is needed for this use case (unlike `molecular_nucleation`'s
  LAMMPS build) — everything HACC-related is already built on the cluster.
- Install is fast (3-4 small packages); no `LD_LIBRARY_PATH` or headless-rendering env
  vars are required beyond what's already set globally for the project.
