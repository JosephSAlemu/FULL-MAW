---
name: use_cases/eddy_uv/explorer
description: >
  Eddy_uv (Nek5000 CFD) execution facts for the explorer -- running the producer
  under MPI inside the existing allocation (no qsub), cluster-specific quirks from
  subeddy.pbs (rank count, sourced env), field-file naming, and which files to
  ignore. Does not prescribe the CFD/visualization math -- that's left to the
  explorer's own reasoning.
---

# Eddy_uv (Nek5000 CFD) — Explorer Skill

Cluster/environment facts for executing the Nek5000 `eddy_uv` workflow. This
skill covers only what isn't derivable by reading the case files in isolation
or from general CFD/Nek5000 knowledge -- the actual mechanics of running this
specific case on this specific cluster.

It describes **the work**, not the dispatch mechanism. Every step below belongs
in the single workflow file you generate with `write_workflow` and execute with
`run_workflow`; how that file expresses a step (`@bash_app`, `@binary`/`@mpi`
over `@task`, or a stage function) is defined by the engine reference in your
context, which is authoritative.

---

## When to Use This Skill

Load whenever the explorer is executing a workflow that runs/reads a
Nek5000 `eddy_uv` run (paths under
`/lcrc/project/PEDAL/jalemu/Nek5000/NekExamples-master/eddy_uv/`).

---

## Stage 1: Run the Producer (MPI, inside the existing allocation)

**No new PBS job here, and no `qsub`.** The producer runs inside the same
allocation this whole run is already using — the same allocation the field-file
analysis/visualization stages run in — so producer and consumer are always one
job, never two separate submissions.

`subeddy.pbs` still exists on disk as the vendor sample script — read it first, but
only as a reference for the env file and executable name, not something to submit.

The step the workflow file must express:

```
command:  bash -c 'source /lcrc/project/PEDAL/jalemu/HACC/HACC_go/env/bashrc.improv.cpu && export OMP_NUM_THREADS=1 && ./nek5000'
ranks:    <NRANKS>
work dir: /lcrc/project/PEDAL/jalemu/Nek5000/NekExamples-master/eddy_uv
```

- Before building the file, call `get_resources()` and confirm `in_pbs` is true
  AND `ntasks`/`cpus_per_task` >= `NRANKS`.
- `NRANKS` comes from the task (the user may override it); default to `8` — the
  value confirmed working for this producer. Set it explicitly in the workflow
  file rather than letting the engine default to whatever rank count the
  interactive allocation happens to have.
- If `get_resources` reports fewer than `NRANKS` ranks available, stop and tell the
  user to restart their interactive PBS job with enough resources — do not fall
  back to a smaller rank count silently, and do not work around it by submitting a
  separate batch job.
- The working directory must be the `eddy_uv/` directory so `./nek5000` (relative)
  is found and field-file output lands alongside the case files.
- Sourcing `bashrc.improv.cpu` inside the `bash -c` wrapper is required before the
  binary can find its shared libraries — the same shared cluster env the
  `cosmology` use case's HACC build also uses; not a sign of misconfiguration.
- `nek5000` in this directory is **already built** (`build.log` exists) --
  never try to rebuild it.
- The producer completes within the run of the workflow file — there's no separate
  job to poll with `qstat`.

### Quirks in the original `subeddy.pbs` (for reference only — this file is never submitted)
- `#PBS -l select=1:mpiprocs=32` requests a node with headroom for 32 procs,
  but the script's `mpiexec -np ${NTOTRANKS}` only launches **8** ranks
  (`NRANKS=8` is set explicitly in the script body) -- 8 is the actual rank
  count for this producer, not 32. Use 8 ranks directly; there's no headroom
  concept to replicate.
- The script sources
  `/lcrc/project/PEDAL/jalemu/HACC/HACC_go/env/bashrc.improv.cpu` to set up
  the Polaris/Improv module environment. That's the shared cluster bashrc
  reused from the `cosmology` use case's HACC build -- it is not a sign the
  script is misconfigured or belongs to a different project; keep sourcing it
  before `./nek5000`.

---

## Stage 1b: Explore Before Assuming Anything

List the directory before assuming any filenames or formats:
```
ls -la /lcrc/project/PEDAL/jalemu/Nek5000/NekExamples-master/eddy_uv/
```
Config values live in `eddy_uv.rea` (params) and `eddy_uv.usr` (case-specific
Fortran) -- read them rather than assuming values from a paper or description.

---

## Reading Field-File Output

- Output file naming is **5-digit zero-padded**: `eddy_uv0.f00001` ...
  `eddy_uv0.f00011` -- confirm the exact digit width against
  `eddy_uv.nek5000`'s `filetemplate` line rather than assuming 4 digits.
- `erreddy_uv0.f00001` is a **separate** file (a Walsh-exact-solution error
  diagnostic written once at the final step) -- do not include it when
  iterating over the per-timestep series. A glob like `eddy_uv0.f*` already
  excludes it correctly (the err file's name doesn't start with `eddy_uv0`).
- Read field files with `pymech` -- this is the pip-installable package that
  understands the Nek5000 binary field-file format; do not hand-parse it.
  (Multiple valid entry points exist across pymech versions, e.g.
  `pymech.neksuite.readnek` or `pymech.open_dataset` -- check what the
  installed version actually exposes rather than assuming one.)
- Beyond that -- how to turn the per-element data into whatever quantity and
  plot the task requires (e.g. a stream function, vorticity, velocity
  magnitude) is a CFD/numerics problem to solve from first principles using
  the case's physics, not something this skill prescribes.

---

## The Deliverable Includes a Rendered Image

Whatever derived quantity the task asks for, the consumer work is not done
until it has rendered and saved a visualization per field file (matplotlib,
`Agg` backend, saved as PNG) -- not just printed or pickled numeric arrays.
The consumer is single-process Python work per file; it is never an MPI step.

---

## Where Real Parallelism Lives: Per Field File

The producer is one indivisible unit of work: its only parallelism (8 MPI ranks)
happens inside the pre-built `nek5000` executable, so never split or fan it out.

The field-file series (`eddy_uv0.f00001` .. `f00011`) is the one place
independent units of work exist here -- each file's (read -> compute ->
render) is fully independent of every other file. Express those **11 per-file
steps as 11 separate invocations inside the single generated workflow file**,
using the engine's own construct:

- **parsl** — 11 `@python_app` invocations; collect the futures, then
  `.result()` them in the driver body.
- **pycompss** — 11 `@task` invocations, with the per-file paths declared
  through `FILE_IN`/`FILE_OUT` parameter types.
- **adios** — 11 calls to the per-file stage function from `main()`.

The engine is what runs them concurrently. **Do not build your own
`ThreadPoolExecutor`, `ProcessPoolExecutor`, `multiprocessing` pool, or
threads** — and do not collapse the series into one step that loops over all 11
files serially, which throws away the only real parallelism in this workflow.

---

## Common Pitfalls

| Pitfall | Solution |
|---|---|
| Assuming the producer needs 32 MPI ranks | That was the original `subeddy.pbs`'s headroom quirk (`mpiprocs=32` vs. `mpiexec -np` 8). Use 8 ranks unless the task says otherwise. |
| Explorer calls `qsub`/`qstat` for this project | Don't -- the workflow file runs `./nek5000` on 8 ranks with `eddy_uv/` as the working directory, inside the existing allocation. |
| The producer step fails with a launcher/env error | Wrap the command in `bash -c '...'`, sourcing `bashrc.improv.cpu` before `./nek5000`, and confirm the working directory is the `eddy_uv/` directory. |
| Globbing picks up `erreddy_uv0.f00001` alongside the main series | Glob `eddy_uv0.f*` specifically -- it won't match the `err`-prefixed file. |
| Assuming 4-digit field indices (`f0001`) | This case's files are 5-digit (`f00001`) -- check `eddy_uv.nek5000`'s `filetemplate`. |
| `ModuleNotFoundError: No module named 'pymech'` | `install_package("pymech")` -- it's a normal pip package, not a cluster binary. |
| Parallelizing the file series with your own thread/process pool | Don't build concurrency yourself -- emit 11 separate engine invocations in the workflow file and let the engine schedule them. |
| Splitting the workflow across several generated files or several runs | One file contains every step: producer plus all 11 per-file steps. |

---

## Output Files (in the work dir)

- Whatever intermediate arrays the explorer chooses to save (npz, csv, etc.)
- One or more rendered PNGs -- the visualization deliverable
- `summary.txt` -- files read, PNGs produced
