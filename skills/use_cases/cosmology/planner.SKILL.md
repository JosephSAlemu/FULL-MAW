---
name: use_cases/cosmology/planner
description: >
  Cosmology (HACC) extraction rules for the planner. Covers what parameters to
  extract from cosmological N-body papers (Last Journey / HACC), the correct
  stack_decision for this project, the one-workflow rule (producer AND
  analysis/visualization handed to the engine together -- never developed against
  pre-existing sample output), and task templates for the pipeline.
---

# Cosmology (HACC) — Planner Skill

Extraction and task-writing rules specific to the HACC cosmological N-body workflow.

---

## When to Use This Skill

Load when planning a HACC / cosmological N-body workflow (paper mentions HACC, Mira,
Last Journey, FOF/SOD halo finding, or the goal references
`/lcrc/project/PEDAL/jalemu/HACC/`).

---

## The Simulation Is a Private Black Box

The `hacc_tpm` executable is closed-source. Tasks must have the explorer run the
existing binary through the workflow engine and then analyze its output — never
write tasks that ask the explorer to "reimplement", "recreate", or "regenerate" the
simulation logic in Python. v1 scope is strictly "call the existing binary, read its
output."

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

## No `qsub` Exception — The Engine Runs the Producer

There used to be an exception here letting this use case submit a real new PBS batch
job. **That is gone.** This use case now follows the same rule as every other one:
never submit a new PBS job, never write a `.pbs` or `.sh` script, and never instruct
the explorer to call `qsub`/`qstat`. The run is already inside an allocation, and the
workflow engine launches the producer.

The producer and the analysis/visualization are still **one workflow that does both
stages**, handed to the engine together — the simulation runs, and the analysis runs
after it, with the ordering expressed as a dependency rather than by polling a job
queue. Never write tasks that run the producer and then separately re-run
analysis/visualization as an unrelated execution.

Carry forward the `NRANKS` value (default `8` unless the paper or user's goal
specifies otherwise) as the rank count the producer must run with. `WALLTIME` is no
longer meaningful — there is no batch job to request a walltime for. Read
`subme.pbs` only as a reference for the executable/env/param paths; never submit or
adapt it.

**Never write a task that has the explorer read, parse, or render from
`output/full_snapshots/`/`analysis/haloproperties/` content before this run's own
producer has executed.** Anything already sitting in those directories is leftover
output from some prior run, not this run's own result — this run's analysis must not
depend on it in any way. Write the analysis/rendering script directly from the
documented GenericIO format, mass formula, and halo-selection rules (see the
explorer skill), informed by this run's own `params/indat.params` (config, not
output) — not by testing the code against old data first.

If the analysis stage fails after the producer has run, the recovery task should have
the explorer fix the analysis and run **only that step** against *this run's own*
fresh output that the producer already wrote — never repeat the expensive producer
just to fix an analysis bug, and never fall back to old data from another run.

---

## Task List Template

```
1. "Call get_resources FIRST. Confirm in_pbs is true and report PBS_NP/PBS_NUM_NODES.
   If in_pbs is false, STOP and tell the user to start a PBS interactive job first.
   This run requires 8 MPI ranks for the HACC producer."

2. "Explore /lcrc/project/PEDAL/jalemu/HACC/SampleRun_go/ before assuming anything:
   list subme.pbs, params/, output/, and analysis/ (just to confirm they exist —
   do not read snapshot/halo content from output/ or analysis/, that's leftover
   data from a prior run, not this run's). Read params/indat.params and
   cosmotools-config.dat for this run's actual config values (FOF linking length,
   SOD Delta, RL, NG). Do not fabricate config values not present in these files."

3. "Run the HACC producer: hacc_tpm on 8 MPI ranks against params/indat.params,
   with the env file sourced before the executable in the same invocation and
   SampleRun_go/ as the working directory so ./params/indat.params resolves.
   This is a command-line step — the workflow engine runs it. Do not write a PBS
   or shell script, and do not call qsub. Use subme.pbs only as a reference for
   the executable, env file, and param file paths; never modify or submit it."

4. "Dump the particle snapshot and the halo catalog to text with the GenericIOPrint
   CLI tool, redirecting each to a file under /app/work/run0/. These are
   command-line steps run by the engine, ordered after the producer — pygio is not
   built and must not be used, and GenericIOPrint must not be called via subprocess."

5. "Analyze and render, ordered after the dumps: parse the snapshot dump
   (x,y,z,vx,vy,vz,phi; compute mass from Omega_m/rho_crit0/RL/NP), parse the halo
   catalog dump (read the tab-separated header for column order, select the most
   massive halo by sod_halo_mass excluding sod_halo_count == -101), compute a
   4 Mpc/h-thick xy density slice centered on that halo's z, render it with
   matplotlib using LogNorm (Agg backend, headless), and write summary.txt.
   Outputs go to /app/work/run0/. Do not develop or test this against any
   pre-existing snapshot/halo content — those paths won't hold this run's real
   data until the producer has executed."

5a. "IF `--engine adios` WAS SELECTED (this is its own required task, not optional):
   the analysis MUST `import adios2` and use `adios2.Stream` — not `.npz` — for
   `particles_step<N>.bp` (write x,y,z,vx,vy,vz,phi,mass with `stream.write`, then
   re-open the file and read them back with `stream.read` before using them) and
   `density_slice.bp` (write the computed grid, then read it back before passing it
   to matplotlib). Do not defer this or treat it as an enhancement to add if time
   allows."

6. "Verify the workflow's output: confirm dm_density_slice.png and summary.txt exist
   in /app/work/run0/ and report them. If they're missing, read the workflow's
   stderr: if hacc_tpm itself failed, fix the cause and re-run; if only the analysis
   failed, fix it and re-run ONLY the analysis step against this run's own fresh
   producer output — do not repeat the producer just for an analysis bug, and do not
   fall back to old data from another run."

6a. "IF `--engine adios` WAS SELECTED: confirm `particles_step<N>.bp` and
   `density_slice.bp` exist in /app/work/run0/. If either is missing, the analysis
   did not actually use `adios2.Stream` as required by task 5a — fix it (add the real
   `stream.write`/`stream.read` calls) and re-run the analysis step against this
   run's already-produced output. Do not report the run as complete with `.npz` in
   place of the required `.bp` files."
```

---

## Key Rules

- Source data paths (the reference PBS script, snapshot, halo catalog) are real LCRC
  paths under `/lcrc/project/PEDAL/jalemu/HACC/SampleRun_go/` — these are external to
  the repo and must be referenced by their actual absolute path, not `/app/data/`
  (unlike use cases whose input data is staged into the repo's data directory).
- Never write a task that asks the explorer to modify `subme.pbs`, `indat.params`, or
  `cosmotools-config.dat` in place — if a derived param file is needed (e.g. for a
  slice tool), write a separate copy.
- Never write a task that has the explorer generate a `.pbs` or `.sh` script, call
  `qsub`/`qstat`, or invoke the CLI/subprocess directly. All command-line work is
  handed to the workflow engine.
- Never write a task that reads, parses, or renders from `output/full_snapshots/` or
  `analysis/haloproperties/` content before this run's own producer has executed —
  that's leftover data from some prior run, not this run's result, and this run's
  analysis must never depend on it.
- If recovery from a failed analysis stage is needed, that recovery must target
  *this run's own* fresh output by re-running only the analysis step against it —
  never repeat the producer just to fix an analysis bug, and never fall back to old
  data from another run.
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
    "Run hacc_tpm on 8 MPI ranks against params/indat.params, with the env file sourced before the executable in the same invocation and SampleRun_go/ as the working directory. Do not write a PBS script or call qsub -- subme.pbs is a path reference only.",
    "Dump the particle snapshot and halo catalog to text files in /app/work/run0/ using the GenericIOPrint CLI tool, ordered after the producer. Do not use pygio.",
    "Analyze and render, ordered after the dumps: parse the snapshot (x,y,z,vx,vy,vz,phi; compute mass from Omega_m/rho_crit0/RL/NP), select the most massive halo by sod_halo_mass excluding sod_halo_count == -101, compute a 4 Mpc/h-thick xy density slice on that halo's z, render with matplotlib LogNorm (Agg backend), and write summary.txt.",
    "Confirm dm_density_slice.png and summary.txt exist in /app/work/run0/; if only the analysis failed, fix and re-run that step alone against this run's own fresh output, never repeating the producer."
  ]
}
```
