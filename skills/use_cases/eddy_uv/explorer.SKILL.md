---
name: use_cases/eddy_uv/explorer
description: >
  Eddy_uv (Nek5000 CFD) execution facts for the explorer -- running the producer
  via submit_mpi_task inside the existing allocation (no qsub), cluster-specific
  quirks from subeddy.pbs (rank count, sourced env), field-file naming, and which
  files to ignore. Does not prescribe the CFD/visualization math -- that's left to
  the explorer's own reasoning.
---

# Eddy_uv (Nek5000 CFD) — Explorer Skill

Cluster/environment facts for executing the Nek5000 `eddy_uv` workflow. This
skill covers only what isn't derivable by reading the case files in isolation
or from general CFD/Nek5000 knowledge -- the actual mechanics of running this
specific case on this specific cluster.

---

## When to Use This Skill

Load whenever the explorer is executing a workflow that submits/reads a
Nek5000 `eddy_uv` run (paths under
`/lcrc/project/PEDAL/jalemu/Nek5000/NekExamples-master/eddy_uv/`).

---

## Stage 1: Run the Producer (submit_mpi_task, inside the existing allocation)

**No new PBS job here, and no `qsub`.** The producer runs via `submit_mpi_task`
inside the same allocation this whole run is already using — the same allocation
the field-file analysis/visualization stages run in — so producer and consumer are
always one job, never two separate submissions.

`subeddy.pbs` still exists on disk as the vendor sample script — read it first, but
only as a reference for the env file and executable name, not something to submit.

```
get_resources()   # confirm in_pbs is true AND ntasks/cpus_per_task >= NRANKS

submit_mpi_task(
    name="run_eddy_uv_producer",
    command="bash -c 'source /lcrc/project/PEDAL/jalemu/HACC/HACC_go/env/bashrc.improv.cpu && export OMP_NUM_THREADS=1 && ./nek5000'",
    num_ranks=<NRANKS>,
    work_dir="/lcrc/project/PEDAL/jalemu/Nek5000/NekExamples-master/eddy_uv",
)
```

- `NRANKS` comes from the task (the user may override it); default to `8` — the
  value confirmed working for this producer. Pass it explicitly as `num_ranks`
  rather than relying on `submit_mpi_task`'s `num_ranks=0` default (which uses
  however many ranks the interactive allocation happens to have).
- If `get_resources` reports fewer than `NRANKS` ranks available, stop and tell the
  user to restart their interactive PBS job with enough resources — do not fall
  back to a smaller rank count silently, and do not work around it by submitting a
  separate batch job.
- `work_dir` must be the `eddy_uv/` directory so `./nek5000` (relative) is found and
  field-file output lands alongside the case files.
- Sourcing `bashrc.improv.cpu` inside the `bash -c` wrapper is required before the
  binary can find its shared libraries — the same shared cluster env the
  `cosmology` use case's HACC build also uses; not a sign of misconfiguration.
- `nek5000` in this directory is **already built** (`build.log` exists) --
  never try to rebuild it.
- This call blocks until the run finishes (or times out) — there's no separate job
  to poll with `qstat`; when `submit_mpi_task` returns, the producer is done.

### Quirks in the original `subeddy.pbs` (for reference only — this file is no longer submitted)
- `#PBS -l select=1:mpiprocs=32` requests a node with headroom for 32 procs,
  but the script's `mpiexec -np ${NTOTRANKS}` only launches **8** ranks
  (`NRANKS=8` is set explicitly in the script body) -- 8 is the actual rank
  count for this producer, not 32. Pass `num_ranks=8` to `submit_mpi_task`
  directly; there's no headroom concept to replicate.
- The script sources
  `/lcrc/project/PEDAL/jalemu/HACC/HACC_go/env/bashrc.improv.cpu` to set up
  the Polaris/Improv module environment. That's the shared cluster bashrc
  reused from the `cosmology` use case's HACC build -- it is not a sign the
  script is misconfigured or belongs to a different project; keep sourcing it
  in the `submit_mpi_task` command too.

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

Whatever derived quantity the task asks for, the consumer stage is not done
until it has rendered and saved a visualization per field file (matplotlib,
`Agg` backend, saved as PNG) -- not just printed or pickled numeric arrays.
The consumer overall is single-process work (do not wrap the whole thing in
`submit_mpi_task`).

---

## Where Real Parallelism Lives: Per Field File, via Separate `submit_task` Calls

`submit_task` already runs the `python_code` you give it through the
selected engine's task machinery automatically -- the server wraps it in
`@python_app` (Parsl) or `@task`/`compss_start()` (PyCOMPSs) on its own
persistent worker pool, for every call, with zero code from you (see
`systems/parsl` / `systems/pycompss`).

**Never write `import parsl`, `Config`, `parsl.load(...)`, `compss_start()`,
`@task`, or `@python_app` yourself inside the code you submit.** That nests
a second runtime inside one the server already started. Confirmed in a real
run of this exact workflow: doing exactly that let Parsl auto-detect the
node's full core count and spin up **128 worker processes** (certs, ZeroMQ
sockets, manager/interchange processes) to run one synchronous function call
once -- multiple seconds of pure overhead for work that runs in a fraction
of a second, with zero benefit.

The field-file series (`eddy_uv0.f00001` .. `f00011`) is the one place
independent units of work exist here -- each file's (read -> compute ->
render) is fully independent of every other file. **The correct way to
parallelize it: call `submit_task` 11 separate times, once per file, each
with plain Python code.** The server's own worker pool already runs those
concurrently. This is the same whether the engine is `parsl`, `pycompss`, or
`adios` -- the consumer's code never changes, only what the server does with
it underneath does.

---

## Common Pitfalls

| Pitfall | Solution |
|---|---|
| Assuming the producer needs 32 MPI ranks | That was the original `subeddy.pbs`'s headroom quirk (`mpiprocs=32` vs. `mpiexec -np` 8). Pass `num_ranks=8` to `submit_mpi_task` directly -- 8 unless the task says otherwise. |
| Explorer calls `qsub`/`qstat` for this project | Don't -- run `./nek5000` via `submit_mpi_task` (num_ranks=8, work_dir=eddy_uv/) inside the existing allocation instead. |
| `submit_mpi_task` fails with a launcher/env error | Wrap the command in `bash -c '...'`, sourcing `bashrc.improv.cpu` before `./nek5000`, and confirm `work_dir` is the `eddy_uv/` directory. |
| Globbing picks up `erreddy_uv0.f00001` alongside the main series | Glob `eddy_uv0.f*` specifically -- it won't match the `err`-prefixed file. |
| Assuming 4-digit field indices (`f0001`) | This case's files are 5-digit (`f00001`) -- check `eddy_uv.nek5000`'s `filetemplate`. |
| `ModuleNotFoundError: No module named 'pymech'` | `install_package("pymech")` -- it's a normal pip package, not a cluster binary. |
| Writing `import parsl`/`Config`/`@python_app` (or PyCOMPSs equivalents) inside `python_code` | Never -- `submit_task` already wraps it for you. Write plain Python only. |
| Trying to parallelize the file series with your own executor instead of multiple `submit_task` calls | Don't build concurrency yourself -- call `submit_task` once per file and let the server's worker pool handle it. |

---

## Output Files (in the work dir)

- Whatever intermediate arrays the explorer chooses to save (npz, csv, etc.)
- One or more rendered PNGs -- the visualization deliverable
- `summary.txt` -- files read, PNGs produced
