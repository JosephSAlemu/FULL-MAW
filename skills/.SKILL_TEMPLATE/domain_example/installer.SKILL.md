---
name: uses_cases/<your domain name>/installer
description: >
  Installer behavior for the (your domain name) project. Covers the minimal pip-installable package set, the one non-obvious package, why any pre-built tool/binary (Ex: HACC, nek5000, LAMMPS) must never be pip-installed or rebuilt, and any optional engine dependency (Ex: adios2).
---
<!-- Remove all comments in your copy. -->
<!-- Note: don't use bullet points in section headers; the Notes and package lists may use them (see examples). -->
# (your domain name) [Optional: a tool name] — Installer Skill
**Purpose**: One sentence stating that this sets up the local venv with the packages needed for this workflow.

**Template**:
> Sets up the local venv with the minimal packages needed for the (your domain name) workflow.

**For example**:
> Sets up the local venv with the minimal packages needed for the HACC cosmology workflow.

---

## Current Installer Behavior
**Purpose**: Restate the standard two-phase installer flow so this file is self-contained. This is almost identical across all use cases — copy it and adjust only the approval note.

**Template**:
> Same two-phase flow as the base installer skill (`agents/installer`):
> 1. **Phase 1:** Read or generate `builds/requirements.txt` from `stack_decision`.
> 2. **Orchestrator:** Reviews and approves.
> 3. **Phase 2:** Skip packages already installed; `pip install` the rest.

**For example**:
> File: `use_cases\molecular_nucleation\installer`
>
> Section: "Current Installer Behavior"

    "1. Phase 1: Read or generate builds/requirements.txt... Do NOT call an LLM to generate a new one if the file already exists. 2. Orchestrator: Always approves immediately... 3. Phase 2: Check if all packages are already installed. If yes, skip pip install and return. If no, run pip install -r builds/requirements.txt."

---

## Package Requirements
**Purpose**: List the exact pip-installable packages this workflow needs, plus any engine-specific and optional packages. Call out the one non-obvious package an installer wouldn't otherwise know to add.

**Template**:
> ```
> <standard packages, e.g. numpy, matplotlib>
> <the one non-obvious package>   # what it does and why it's needed
> mpi4py        (only if environment knowledge confirms MPI is available)
> ```
>
> The engine-specific package matching `--engine`:
>- `parsl`: `parsl>=2024.0.0` should be present
>- `pycompss`: `pycompss` should NOT be present (never pip-installed — see `systems/pycompss` skill)
>- `adios`: `adios2` is optional — try installing it, but a failure is not fatal (numpy-I/O fallback exists, see `systems/adios` skill)
>
> **Do NOT add:** \<packages that belong to other use cases>. If `builds/requirements.txt` already has them from a prior run with a different engine, that's stale — regenerate from the current `stack_decision`.

**Example**:
> File: `use_cases\eddy_uv\installer`
>
> Section: "The One Non-Obvious Package"

    "pymech   # reads Nek5000 .f##### field files; pip-installable. numpy/matplotlib are standard... pymech is the one package an installer wouldn't otherwise know to add."
> File: `use_cases\molecular_nucleation\installer`
>
> Section: "Package Requirements"

    "ovito, numpy, matplotlib, pillow. Engine-specific: --engine parsl add parsl>=2024.0.0; --engine pycompss do NOT add pycompss; --engine adios adios2 optional. Do NOT add: scipy, ase, mdanalysis, h5py..."

---

## Never Pip-Install or Build These
**Purpose**: Name the pre-built cluster binaries / in-tree extensions that must never be pip-installed or rebuilt as part of a task, and why (building them is a real installer risk). This is the most important use-case-specific installer rule.

**Template**:
>- **\<tool/binary name>** — \<where it lives> is a pre-built cluster binary, not a Python package. Never attempt to build or install it; treat it as an opaque black box.
>- **\<in-tree extension, if any>** — \<why building it is risky / fails>. The workflow deliberately avoids it; \<what it uses instead>.

**Example**:
> File: `use_cases\cosmology\installer`
>
> Section: "Never Pip-Install or Build These"

    "pygio — in-tree at HACC_go/submodules/genericio/python/pygio, missing its compiled extension... Building it means compiling a C++ extension against the GenericIO libs — a real installer risk. HACC itself (hacc_tpm, hacc_slice, etc.) — pre-built cluster binaries... Never attempt to build or install these."

---

## [Optional: Source Build Steps]
**Purpose**: Only if your workflow's tool must be source-built (like LAMMPS on local/WSL). Document the system deps, build flags, install location, Python bindings, and required environment variables. Most use cases DELETE this section — everything is already built on the cluster.

**Template**:

Content varies heavily. I reccomend going to the skill file in the example.

**For Example**:
> File: `use_cases\molecular_nucleation\installer`
>
> Sections: "LAMMPS — Local Build (Linux/WSL)", "LAMMPS — HPC Build (LCRC/Swing)"

    "Build from source with BUILD_MPI=off. The pip lammps wheel... calls MPI_Init on import in serial contexts, crashing the worker process... Serial build avoids this entirely." (Includes apt deps, cmake flags, Python bindings, and LD_LIBRARY_PATH/OVITO env vars.)

---

<!-- This section can have bullet points -->
## Notes
**Purpose**: End-of-file notes: whether a source build is needed (contrast with other use cases), install speed, any required env vars, and skip-detection details.

**Template**:
>- \<No / a> source build step is needed for this use case (contrast with the LAMMPS build in `molecular_nucleation` / everything is already built on the cluster).
>- \<install speed and any LD_LIBRARY_PATH / headless-rendering env vars required, or a note that none are>.

**Example**:
> File: `use_cases\cosmology\installer`
>
> Section: "Notes"

    "No source build step is needed for this use case (unlike molecular_nucleation's LAMMPS build) — everything HACC-related is already built on the cluster. Install is fast (3-4 small packages); no LD_LIBRARY_PATH or headless-rendering env vars are required beyond what's already set globally."
