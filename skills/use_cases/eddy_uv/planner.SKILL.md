---
name: use_cases/eddy_uv/planner
description: >
  Eddy_uv (Nek5000 CFD) environment facts for the planner -- where the case
  lives, running the producer via submit_mpi_task inside the existing allocation
  (no qsub), and what package reads Nek5000 field files. Does not prescribe the
  CFD/stream-function math -- that's left to the planner's own reasoning from the
  paper and the .usr/.rea files.
---

# Eddy_uv (Nek5000 CFD) — Planner Skill

Cluster/environment facts for the Nek5000 `eddy_uv` workflow (Walsh (1992)
decaying vortex array). This skill only covers what the planner can't get from
the paper or from general CFD knowledge -- where things live on this cluster
and how this case is actually launched here.

---

## When to Use This Skill

Load when planning a Nek5000 CFD workflow (paper or goal mentions Nek5000,
Walsh (1992), eddy_uv, or paths under `/lcrc/project/PEDAL/jalemu/Nek5000/`).

---

## Where the Case Lives

```
/lcrc/project/PEDAL/jalemu/Nek5000/NekExamples-master/eddy_uv/
```
Input/config files: `eddy_uv.rea`, `eddy_uv.usr`, `eddy_uv.map`, `SIZE`,
`SESSION.NAME`. Existing job script: `subeddy.pbs`, in the same directory --
this is a reference for paths/env only, never something to submit (see below).
The `nek5000` executable in this directory is **already compiled** (see
`build.log`) -- never write a task that rebuilds it or asks the explorer to
regenerate it.

---

## No `qsub` Exception — Producer Runs in the Existing Allocation

Like every other use case, this one follows the general LCRC rule: never
submit a new PBS job -- run inside the existing interactive allocation
instead. The Nek5000 producer runs via `submit_mpi_task`, which launches
`./nek5000` with `mpirun`/`mpiexec` inside the same allocation the whole run
is already using, so the producer and the field-file analysis/visualization
stages that follow are always one job, never two separate submissions. State
this explicitly in the relevant task, and carry forward the `NRANKS` value
(default `8` unless the paper or user's goal specifies otherwise) for the
explorer to pass as `num_ranks`.

---

## Stack Decision

```
numpy
matplotlib
pymech        # the package that reads Nek5000 .f##### field files -- pip
              # installable. Do not hand-parse the binary format.
```

Beyond that, let the rest of `stack_decision` follow from what the actual
analysis/visualization code ends up needing -- don't pre-load it with packages
from other use cases (`scipy`, `mpi4py`, `ovito`, `pillow`, `lammps`,
`adios2`) unless the task genuinely calls for them.

---

## Producer / Consumer Split

- **Producer** — run the existing `nek5000` executable via `submit_mpi_task`
  (using `subeddy.pbs` only as a path/env reference), inside the existing
  allocation, and wait for it to complete.
- **Consumer** — a single-process stage that reads the resulting field files
  with `pymech`, computes the requested derived quantity (e.g. the stream
  function), and **must render it as a visualization saved to PNG** -- a
  task list that stops at "compute X" without a rendering/plotting task is
  incomplete. Both stages are executed by the one explorer agent;
  "producer"/"consumer" describes the two task groups, not separate agents.

How the consumer actually reconstructs and renders the requested quantity is
a CFD/numerics question for the planner and explorer to work out from the
case files and the paper -- this skill deliberately does not hand them the
algorithm, so it stays usable for other Nek5000 cases that need a different
derived quantity or plot.

---

## Workflow Shape: Two Stages, and Where Real Parallelism Lives

1. **Producer** — run `./nek5000` via `submit_mpi_task` (num_ranks=8,
   work_dir=eddy_uv/) inside the existing allocation; this blocks until done,
   no polling needed. Never wrapped in Parsl/PyCOMPSs `@task` code, regardless
   of which engine the run selected -- the only real parallelism here (8 MPI
   ranks) already happens entirely inside the pre-built `nek5000` executable
   via `mpiexec`, not anything the agent drives from Python.
2. **Consumer** — the field-file series (`eddy_uv0.f00001` .. `f00011`) is the
   *only* place independent units of work actually exist in this workflow:
   each file's (read -> compute -> render) is fully independent of every
   other file.

### How to actually get that parallelism: one `submit_task` call per file

`submit_task` already runs whatever `python_code` you give it through the
selected engine's task machinery automatically (see `systems/parsl` /
`systems/pycompss` -- the server wraps it in `@python_app`/`@task` for you,
on its own persistent worker pool). **Never write `import parsl`,
`Config`, `parsl.load(...)`, `compss_start()`, `@task`, or `@python_app`
yourself inside the code you submit.** That nests a second runtime inside
one the server already started. Confirmed in a real run of this exact
workflow: doing that let Parsl auto-detect the node's full core count and
spin up **128 worker processes** to run one synchronous function call.

The correct way to parallelize across the 11 field files: call `submit_task`
**11 separate times**, once per file, each with plain Python code (read this
one file, compute, render, save). The server's own worker pool already runs
those concurrently -- that's the whole mechanism, no Parsl/PyCOMPSs code
needed in the task body at all, regardless of which engine the run selected.
If the engine isn't task-parallel (`adios`, or none requested), the same
plain-Python-per-file code just runs through the fallback path instead --
nothing about the consumer's code needs to change either way.

---

## Key Rules

- Source data paths are real LCRC paths under
  `/lcrc/project/PEDAL/jalemu/Nek5000/NekExamples-master/eddy_uv/` — external
  to the repo, reference by actual absolute path, not `/app/data/`.
- Never write a task asking the explorer to modify `eddy_uv.rea`,
  `eddy_uv.usr`, `eddy_uv.map`, `SIZE`, `SESSION.NAME`, or `subeddy.pbs`.
  `subeddy.pbs` itself is read-only reference material now; nothing gets
  submitted from it.
- The task list must always end with a rendering/visualization task that
  produces PNG image output -- this use case's deliverable is a figure, not
  just numeric results.
