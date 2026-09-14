---
name: use_cases/cosmology/planner
description: >
  Cosmology (HACC) extraction rules for the planner. Covers what parameters to
  extract from cosmological N-body papers (Last Journey / HACC), the correct
  stack_decision for this project, the real-qsub exception (producer AND
  analysis/visualization written directly into one generated PBS script, submitted
  once -- never developed against pre-existing sample output), and MCP-style task
  templates for the pipeline.
---

# Cosmology (HACC) — Planner Skill

Extraction and task-writing rules specific to the HACC cosmological N-body workflow.

---

## When to Use This Skill

Load when planning a HACC / cosmological N-body workflow (paper mentions HACC, Mira,
Last Journey, FOF/SOD halo finding, or the goal references
`/lcrc/project/PEDAL/jacoboh/HACC/`).

---

## The Simulation Is a Private Black Box

The `hacc_tpm` executable is closed-source. Tasks must have the explorer build a PBS
batch script that runs the existing binary and then the analysis/rendering script,
submitting both in one `qsub` call, and read its output — never write tasks that ask
the explorer to "reimplement", "recreate", or "regenerate" the simulation logic in
Python. v1 scope is strictly "call the existing binary, read its output."

---

## What to Extract from the Paper

- Box size `RL` and grid resolution `NG` (the SampleRun uses small downscaled values —
  confirm against `params/indat.params` rather than assuming the paper's full-scale
  values apply, e.g. the real Last Journey run is far larger than this sample)
- Cosmological parameters: `Omega_m`, `Omega_cdm`, `Omega_b`, `h`, `sigma_8`, `n_s`
- FOF linking length (`b`), FOF minimum particle count (`FOF_PMIN`)
- SOD overdensity multiple `Delta` (e.g. 200 -> M_200c/R_200c), `SOD_PMIN`
- Halo center convention (most-bound particle / min-potential vs center-of-mass)
- Which snapshot step is being targeted for visualization (this SampleRun config
  produces step 624 as the final step; that's the one used for the reproduction)

Treat the paper as descriptive ground truth for *what the pipeline should compute*,
but always confirm actual numeric config values against the real files in
`SampleRun_go/params/` rather than hardcoding paper values that may not match this
particular sample run.

---

## Stack Decision

```
numpy
matplotlib
mpi4py        (only if environment knowledge confirms MPI is available)
```

If `--engine adios` is selected, add `adios2` — for this engine it's a real
requirement, not an optional extra: task 3 below requires the analysis script to
use `adios2.Stream` (write and read back) for its intermediate arrays. If
`adios2` genuinely fails to install, fall back to the documented numpy-I/O path
(see `systems/adios` skill) rather than blocking the run — but don't omit it
from `stack_decision` by default just because it's not strictly required to
avoid a hard failure.

**Do NOT add:** `ovito`, `pillow`, `lammps`, `scipy`, `ase`, `h5py`, `pygio` — none of
these are needed, and `pygio`/HACC binaries are not pip-installable in the first place
(they're pre-built cluster executables or unbuildable in-tree extensions).

---

## Architectural Exception: This Use Case May `qsub`

Unlike every other use case, a task here is allowed to instruct the explorer to submit
a **real, new PBS batch job** via `qsub` (even from inside an already-running
interactive allocation) — because the simulation only runs through its own batch
script. State this explicitly in the relevant task rather than relying on the
explorer/orchestrator to infer it.

Critically, this must be **one job that does both stages**: the generated script runs
`hacc_tpm` and then, immediately after, the analysis/rendering script — submitted with
a single `qsub` call. Never write tasks that have the explorer submit the producer via
`qsub` and then separately re-run analysis/visualization afterward from the live
session; that's two executions, not one job. The explorer must **build the batch
script itself** rather than submitting the pre-existing `subme.pbs` unmodified — write
tasks that say so explicitly, and carry forward `WALLTIME`/`NRANKS` values (default
`01:00:00` / `8` unless the paper or user's goal specifies otherwise) for the explorer
to bake into the script it writes.

**Never write a task that has the explorer read, parse, or render from
`output/full_snapshots/`/`analysis/haloproperties/` content before this run's own
producer has executed.** Anything already sitting in those directories is leftover
output from some prior run, not this run's own result — this run's analysis must not
depend on it in any way. Write the analysis/rendering script directly from the
documented GenericIO format, mass formula, and halo-selection rules (see the
explorer skill), informed by this run's own `params/indat.params` (config, not
output) — not by testing the code against old data first.

If the analysis stage fails after the job runs, the recovery task should have the
explorer fix `analyze_and_render.py` and re-run it directly (via `submit_shell_task`,
not a new `qsub`) against *this run's own* fresh output that the producer already
wrote — never resubmit the whole PBS job just to fix an analysis bug, and never fall
back to old data from another run.

---

## Task List Template

```
1. "Call get_resources FIRST. Confirm in_pbs is true and report PBS_NP/PBS_NUM_NODES.
   If in_pbs is false, STOP and tell the user to start a PBS interactive job first.
   This run requires 8 MPI ranks for the HACC producer."

2. "Explore /lcrc/project/PEDAL/jacoboh/HACC/SampleRun_go/ before assuming anything:
   list subme.pbs, params/, output/, and analysis/ (just to confirm they exist —
   do not read snapshot/halo content from output/ or analysis/, that's leftover
   data from a prior run, not this run's). Read params/indat.params and
   cosmotools-config.dat for this run's actual config values (FOF linking length,
   SOD Delta, RL, NG). Do not fabricate config values not present in these files."

3. "Write /app/work/run0/analyze_and_render.py directly, using real absolute paths
   (not /app/ shortcuts — this script runs standalone, invoked by the PBS job).
   Base it on the documented GenericIOPrint format and halo-selection rules (see
   the explorer skill): read the particle snapshot (parse x,y,z,vx,vy,vz,phi,
   compute mass from Omega_m/rho_crit0/RL/NP), read the halo catalog (parse the
   tab-separated header for column order, select most massive halo by
   sod_halo_mass excluding sod_halo_count == -101), compute a 4 Mpc/h-thick xy
   density slice centered on the halo's z, render with matplotlib (LogNorm),
   write summary.txt. Do not develop or test this against any pre-existing
   snapshot/halo catalog content — those paths won't have this run's real data
   until the producer below has executed."

3a. "IF `--engine adios` WAS SELECTED (this is its own required task, not optional):
   analyze_and_render.py MUST `import adios2` and use `adios2.Stream` — not `.npz`
   — for `particles_step<N>.bp` (write x,y,z,vx,vy,vz,phi,mass with `stream.write`,
   then re-open the file and read them back with `stream.read` before using them)
   and `density_slice.bp` (write the computed grid with `stream.write`, then read
   it back with `stream.read` before passing it to matplotlib). Do this before
   moving on to the PBS script below — do not defer it or treat it as an
   enhancement to add if time allows."

4. "Build a PBS batch script yourself (do not submit subme.pbs unmodified) — use
   subme.pbs only as a reference for the executable/env/param paths. Use
   WALLTIME=<walltime, default 01:00:00> and NRANKS=<ranks, default 8>. The script
   must run hacc_tpm and then, immediately after, invoke analyze_and_render.py via
   the venv's absolute python3 path — one script, both stages. Write it to
   /app/work/run0/agent_subme.pbs, copy it into
   /lcrc/project/PEDAL/jacoboh/HACC/SampleRun_go/agent_subme.pbs, then submit via
   submit_shell_task: cd into SampleRun_go/ and run `qsub agent_subme.pbs` from that
   directory so $PBS_O_WORKDIR resolves correctly (real qsub is explicitly allowed for
   this use case). Capture and report the returned job ID. Do not modify subme.pbs."

5. "Poll the job with `qstat <job_id>` via submit_shell_task until it reaches a
   completed state. Use a fixed 60-second interval between polls (e.g. `sleep 60 &&
   qstat <job_id>`) — do not busy-loop, and do not grow the interval between polls;
   submit_shell_task blocks for the full sleep, so a longer interval just wastes
   wall-clock time."

5a. "IF `--engine adios` WAS SELECTED: use `list_files` on /app/work/run0/ to confirm
   `particles_step<N>.bp` and `density_slice.bp` exist. If either is missing,
   analyze_and_render.py did not actually use `adios2.Stream` as required by task
   3a — go back and fix it (add the real `stream.write`/`stream.read` calls), then
   re-run the analysis stage directly via submit_shell_task against this run's
   already-produced output. Do not report the run as complete with `.npz` in
   place of the required `.bp` files."

6. "Read back dm_density_slice.png and summary.txt from /app/work/run0/ (written by
   the embedded script when the job ran) to confirm they exist and report them. If
   they're missing, check the job's log: if hacc_tpm itself failed, fix and
   resubmit the whole job; if only the analysis stage failed, fix
   analyze_and_render.py and re-run it directly via submit_shell_task against this
   run's own fresh output — do not resubmit the whole job just for an analysis bug,
   and do not fall back to old data from another run."
```

---

## Key Rules

- Source data paths (the PBS script, snapshot, halo catalog) are real LCRC paths under
  `/lcrc/project/PEDAL/jacoboh/HACC/SampleRun_go/` — these are external to the repo and
  must be referenced by their actual absolute path, not `/app/data/` (unlike use cases
  whose input data is staged into the repo's data directory).
- Never write a task that asks the explorer to modify `subme.pbs`, `indat.params`, or
  `cosmotools-config.dat` in place — if a derived param file is needed (e.g. for a
  slice tool), write a separate copy. The explorer's own generated batch script
  (`agent_subme.pbs`) is expected to exist both under `/app/work/run0/` (the tracked
  copy) and under `SampleRun_go/` (the copy `qsub` actually runs) — these are new
  files, not modifications to `subme.pbs`.
- Never write a task that reads, parses, or renders from `output/full_snapshots/` or
  `analysis/haloproperties/` content before this run's own producer has executed —
  that's leftover data from some prior run, not this run's result, and this run's
  analysis must never depend on it.
- If recovery from a failed analysis stage is needed, that recovery must target
  *this run's own* fresh output (re-run `analyze_and_render.py` directly against it)
  — never resubmit the whole PBS job just to fix an analysis bug, and never fall back
  to old data from another run.
- This is a single producer -> analysis -> visualization pipeline per run, with no
  per-frame animation/GIF stage (that's specific to `molecular_nucleation`).

---

## Example Output

```json
{
  "literature_findings": [
    "Last Journey: gravity-only cosmological N-body simulation run with HACC on Mira; this is a downscaled 8-MPI-rank sample run",
    "Cosmology (Planck best-fit): Omega_m=0.310, h=0.6766, sigma_8=0.8102, n_s=0.9665",
    "Halo finding: FOF (linking length b) then SOD (grows sphere to Delta x critical density), most massive halo selected by M_200c",
    "Visualization: 4 Mpc/h-thick xy density slice centered on most massive halo's z-coordinate"
  ],
  "stack_decision": ["numpy", "matplotlib", "mpi4py"],
  "tasks": [
    "Call get_resources FIRST; confirm in_pbs true, 8 MPI ranks available.",
    "Explore SampleRun_go/ (subme.pbs, params/, output/, analysis/) to confirm structure and read config values -- do not read snapshot/halo content from output/ or analysis/.",
    "Write /app/work/run0/analyze_and_render.py directly from the documented GenericIOPrint format and halo-selection rules, using this run's own indat.params config values -- not developed against any pre-existing data.",
    "Build agent_subme.pbs (WALLTIME=01:00:00, NRANKS=8) that runs hacc_tpm then analyze_and_render.py, write to /app/work/run0/, copy into SampleRun_go/, submit `qsub agent_subme.pbs` from there (real qsub allowed for this use case, one job for both stages); capture job ID.",
    "Poll `qstat <job_id>` until completed.",
    "Read back dm_density_slice.png and summary.txt from /app/work/run0/ to confirm the job produced them; if the analysis stage failed, fix and re-run it directly against this run's own fresh output, not a job resubmit."
  ]
}
```
