---
name: use_cases/cosmology/explorer
description: >
  Use-case-specific explorer rules for the HACC cosmology workflow (Last Journey
  sample run). Covers building a single PBS script that runs the producer AND the
  analysis/rendering together in one qsub submission -- GenericIO reading via CLI
  tools (no pygio), halo catalog parsing, and known pitfalls.
---

# Cosmology (HACC) — Explorer Skill

Domain-specific guidance for the explorer agent when executing the HACC cosmological
N-body workflow (producer simulation -> FOF/SOD halo analysis -> dark-matter density
slice visualization), based on "The Last Journey" sample run.

---

## When to Use This Skill

Load this whenever the explorer is executing a workflow that runs/reads a HACC
run (paths under `/lcrc/project/PEDAL/jalemu/HACC/`, GenericIO snapshots, FOF/SOD
halo catalogs).

---

## The Simulation Is a Private Black Box

The HACC executable (`hacc_tpm`) is closed-source. Never try to read, regenerate, or
reimplement its physics. Treat it purely as: run the existing binary, wait for it to
finish, read its output. Do not modify `params/indat.params`.

---

## Overall Shape: One PBS Script, Sim + Vis Together

The producer and the analysis/visualization go in **one PBS script** — a single
`qsub` submission that runs the simulation and then renders the final image. Do not
develop or test the analysis code against `SampleRun_go/output/full_snapshots/` or
`SampleRun_go/analysis/haloproperties/` content that's already sitting there from
some prior run — that's someone else's leftover output, not this run's, and this
run's result should never depend on it. Write the analysis/rendering script directly
from the documented GenericIO format and halo-selection rules below, informed by
this run's own `params/indat.params` (config, not output), then embed it in the PBS
script alongside the producer.

---

## Stage 1: Explore the Case (config only, not old output)

`list_files`/explore `SampleRun_go/` (list `subme.pbs`, `params/`, `output/`,
`analysis/` -- just to confirm they exist, not to read snapshot/halo content from
them). Config values (FOF linking length, SOD Delta, box size `RL`, grid size `NG`)
live in `params/indat.params` and `params/cosmotools-config.dat` — read them, never
hardcode values from the paper without confirming they match this sample run. These
are this run's own input configuration, not another run's results, so reading them
is fine.

---

## Stage 2: Write the Analysis/Rendering Script

Write `/app/work/run0/analyze_and_render.py` directly, using the documented
GenericIOPrint format and halo-selection rules ("Reading Snapshot & Halo Data" and
"Computing and Rendering the Density Slice" below) and the config values read in
Stage 1 -- not by reading or testing against any pre-existing snapshot/halo catalog
content. This script must use **real absolute paths**, not `/app/`-prefixed ones —
`/app/` paths are only resolved by the MCP tool layer, and this script runs
standalone (invoked directly by the PBS job, no MCP tools involved) — so its output
paths should be the real path to this repo's `work/run0/` directory (confirm the
repo root with `pwd`/`list_files` if unsure), and its input paths should be the real
`SampleRun_go/output/...`/`analysis/...` paths (where *this job's own* producer run
will write, once it executes).

---

## Stage 3: Build and Submit One PBS Job (producer + analysis, single qsub)

Read `SampleRun_go/subme.pbs` first, but only as a reference for the executable path,
env file, and param file location; then write a new script with those paths, the
task's `WALLTIME`/`NRANKS` parameters, and a call to the Stage 2 script:

```bash
#!/bin/bash
#PBS -A PEDAL
#PBS -l walltime=<WALLTIME>
#PBS -l select=1:mpiprocs=<NRANKS>
#PBS -l place=scatter
#PBS -N agent_hacc_run
#PBS -j oe

set -e
cd $PBS_O_WORKDIR

exe=/lcrc/project/PEDAL/jalemu/HACC/HACC_go/improv.cpu/mpi/bin/hacc_tpm
envfile=/lcrc/project/PEDAL/jalemu/HACC/HACC_go/env/bashrc.improv.cpu
paramfile=./params/indat.params
source $envfile

NNODES=1
NRANKS=<NRANKS>
NTHREADS=1
NTOTRANKS=$(( NNODES * NRANKS ))

mpiexec -np ${NTOTRANKS} \
  --map-by ppr:${NRANKS}:node \
  --bind-to core \
  -x OMP_NUM_THREADS=${NTHREADS} \
  $exe $paramfile -n

# Analysis + visualization -- runs in the same job, right after the producer.
# Use the venv's real absolute python3 path (this script is invoked directly by
# PBS, not through the MCP tool layer, so no /app/ shortcuts here either).
<VENV_PYTHON_ABS_PATH> <REPO_ROOT_ABS_PATH>/work/run0/analyze_and_render.py
```

- `WALLTIME`/`NRANKS` come from the task (the user may override them); if the task
  doesn't specify either, default to `01:00:00` / `8` — the values confirmed working
  for this sample run's 2x2x2 decomposition.
- `<VENV_PYTHON_ABS_PATH>` is this repo's `venv3/bin/python3`, by its real absolute
  path (e.g. confirm with `list_files`/`read_file` if unsure of the exact repo root)
  — do not invoke bare `python3`, it may resolve to a different interpreter once
  `envfile` has been sourced.
- Write the generated script to `/app/work/run0/agent_subme.pbs` first — this is the
  canonical, tracked copy of what was actually submitted, and it must exist in the
  work dir alongside the rest of this run's artifacts. Then copy that same file into
  `SampleRun_go/agent_subme.pbs` (e.g. `cp /app/work/run0/agent_subme.pbs
  /lcrc/project/PEDAL/jalemu/HACC/SampleRun_go/agent_subme.pbs` via `submit_shell_task`)
  — never overwrite the original `subme.pbs`.
- Must `cd`/submit from `SampleRun_go/` (not the work dir) so `$PBS_O_WORKDIR` resolves
  and `./params/indat.params` is found relatively:

```
submit_shell_task(
    name="submit_hacc_job",
    command="cd /lcrc/project/PEDAL/jalemu/HACC/SampleRun_go && qsub agent_subme.pbs",
)
```

- Capture the returned job ID, then poll with `qstat <job_id>` via `submit_shell_task`
  until the state is `C` (completed). Use a **fixed 60-second interval** between polls
  (e.g. `sleep 60 && qstat <job_id>`) — do not busy-loop with zero delay, and do not
  escalate the interval between polls; `submit_shell_task` blocks for the full sleep
  duration, so growing it wastes wall-clock time for no benefit.

---

## Stage 4: Report, or Recover Without Re-Running the Producer

After completion, read back `/app/work/run0/dm_density_slice.png` and `summary.txt`
(written by the embedded script) to confirm they exist and report them.

If they're missing, check the job's `.o<jobid>` log for where it failed:
- **If the producer (`hacc_tpm`) itself failed**, fix the underlying issue (wrong
  param path, wrong rank count, etc.) and resubmit the whole `agent_subme.pbs` — the
  producer has to actually run again to get real output.
- **If the producer succeeded but the analysis/rendering script failed**, do
  **not** resubmit the whole job — `hacc_tpm` already wrote real output for this run
  to `output/full_snapshots/`/`analysis/haloproperties/`. Fix `analyze_and_render.py`
  and re-run it directly via `submit_shell_task` (the venv's absolute python3 path,
  same as the PBS script invokes it) against that output. This is still *this run's*
  own fresh data, not old data from some other run, and it avoids re-queuing the
  expensive MPI producer just to fix a bug in the analysis code.

---

## Reading Snapshot & Halo Data — Do NOT use pygio

`pygio` (in-tree at `HACC_go/submodules/genericio/python/pygio`) is **not built** —
importing it fails with `ModuleNotFoundError: No module named 'pygio._version'`.
Building it means compiling a C++ extension against the GenericIO libs — do not
attempt this. Instead use the pre-built CLI binaries directly via `subprocess`:

```
GIOP = "/lcrc/project/PEDAL/jalemu/HACC/HACC_go/improv.cpu/frontend/bin/GenericIOPrint"
```

### Reading the particle snapshot
```python
import subprocess, numpy as np

SNAP = "/lcrc/project/PEDAL/jalemu/HACC/SampleRun_go/output/full_snapshots/step_624/m000p.full.mpicosmo.624"
out = subprocess.run([GIOP, SNAP], capture_output=True, text=True)

rows = []
for ln in out.stdout.splitlines():
    s = ln.strip()
    if s.startswith("#") or not s:
        continue
    parts = s.split()
    if len(parts) < 7:
        continue
    rows.append([float(parts[i]) for i in range(7)])  # x,y,z,vx,vy,vz,phi
arr = np.array(rows, dtype=np.float64)
```
- Pass the **master snapshot file** (e.g. `m000p.full.mpicosmo.624`), not a per-rank
  shard — `GenericIOPrint` reads across all ranks for you.
- Header/comment lines start with `#`; the physical-coordinates line
  (`# physical coordinates: (0,0,0) -> (64,64,64)`) confirms the box bounds — use it
  to sanity-check `RL` from `indat.params` rather than trusting one source blindly.
- Particle mass is not a column — compute it: `mp = Omega_m * rho_crit0 * (RL/NP)**3`
  with `rho_crit0 = 2.77536627e11` (h^2 Msun/Mpc^3), `Omega_m` from cosmology params,
  `NP` = particles-per-side (`64` for this sample run, giving `64^3 = 262144` total).

### Reading the halo catalog
```python
HP = "/lcrc/project/PEDAL/jalemu/HACC/SampleRun_go/analysis/haloproperties/step_624/m000p-624.haloproperties"
```
- This file is written by HACC's own halo finder as part of the run, under
  `analysis/haloproperties/step_<N>/` — do not hand-roll FOF/SOD linking in Python;
  parse this output instead. (This path won't have this run's real content until
  `hacc_tpm` has actually executed — the analysis script parses whatever is there
  *when it runs*, i.e. inside the PBS job, after the producer stage.)
- `GenericIOPrint` on this file gives a tab-separated header line listing ~76 columns
  including `fof_halo_count`, `fof_halo_mass`, `fof_halo_center_x/y/z`,
  `sod_halo_mass`, `sod_halo_radius`, `sod_halo_center_x/y/z`, etc. — parse the header
  to get column order rather than assuming a fixed schema.
- `fof_halo_center_*` is the min-potential center (most-bound particle), matching
  `USE_MBP_FINDER YES` in `cosmotools-config.dat`.
- **"Most massive halo" = row with max `sod_halo_mass` (M_200c)**, not `fof_halo_mass`.
  Rows with `sod_halo_count == -101` mean SOD was never computed for that (small) halo
  — exclude them before taking the max.
- Use the halo's center z-coordinate as the visualization slice center.

---

## Computing and Rendering the Density Slice

Two valid approaches — pick whichever is simpler to get working; both are legitimate:

**A. Pure-Python (confirmed working)**: bin particle (x,y) into a 2D histogram weighted
by mass, restricted to particles within the slice thickness around the halo's z:
```python
NBINS = 256
edges = np.linspace(0, RL, NBINS + 1)
sel = np.abs(z - cz) <= thickness / 2.0
H, _, _ = np.histogram2d(x[sel], y[sel], bins=[edges, edges], weights=mass[sel])
sigma = H / (RL / NBINS) ** 2   # projected surface density, h^-1 Msun / (Mpc/h)^2
```

**B. `hacc_slice` binary (not yet exercised end-to-end, may need a slightly different
invocation)**: `hacc_slice <paramFile> <gioInBase> <outBase>` (in the same `bin/` as
`GenericIOPrint`/`hacc_tpm`) computes a CIC density grid directly from the raw
snapshot. `paramFile` must be a **copy** of `indat.params` (never edit the original)
with `SLICE_START`/`SLICE_STOP` appended — these are **fractions of the box in [0,1]
along z**, not Mpc/h: `SLICE_START=(z_h - T/2)/RL`, `SLICE_STOP=(z_h + T/2)/RL`. Output
is `<outBase>.slice`, a headerless raw binary of `NG*NG` floats written by rank 0 only
— disambiguate float32 vs float64 from file size (`NG*NG*4` vs `NG*NG*8` bytes), don't
assume.

Render with matplotlib (`LogNorm`, `imshow`, mark the halo center), save as PNG —
this is a final human-facing artifact, plain `.png` is correct (no ADIOS2 needed here).

---

## ADIOS2 Engine Notes (when `--engine adios`)

The `write_bp`/`read_bp` MCP *tools* can't be called from inside the embedded
`analyze_and_render.py` script — it runs standalone inside the PBS job, invoked
directly by the batch script with no MCP session available, and those are MCP
tools like any other, so they need a live session to invoke. But that's a
constraint on the *tools*, not on ADIOS2 itself: `adios2` is a plain Python
library (already installed in the venv this script runs under) with no
dependency on any session, server process, or IPC — it just reads/writes `.bp`
files on disk. `write_bp`/`read_bp` work by generating a small wrapper script
(`import adios2; with adios2.Stream(path, mode) as stream: ...`) and running it
as a subprocess — exactly the same execution mechanism the embedded script
already uses. There's nothing stopping the embedded script from doing the same
thing directly.

So: when `--engine adios` is selected, **this is required, not an optional
enhancement.** `import adios2` in `analyze_and_render.py` itself and use
`adios2.Stream(path, mode)` directly (not the `write_bp`/`read_bp` tools, which
aren't reachable from here) with a genuine `stream.write(...)` then
`stream.read(...)` round trip — write the array to the `.bp` file and then
actually read it back from that file before using it, don't just write it and
keep using the in-memory copy. This applies to the real inter-stage numerical
data: `particles_step<N>.bp` (the raw snapshot arrays: x,y,z,vx,vy,vz,phi,mass)
and `density_slice.bp` (the projected density grid), matching the `.bp` variants
already listed under Output Files below. `halo_catalog` and `most_massive_halo`
stay `.csv`/`.txt` — they're small, one-shot metadata reads from HACC's own
halo-finder output, not numerical arrays flowing between processing stages.
`dm_density_slice.png` and `summary.txt` stay plain human-facing files regardless
of engine mode.

---

## Common Pitfalls

| Pitfall | Solution |
|---|---|
| `pygio` import fails (`No module named 'pygio._version'`) | Expected — it's not built. Use `GenericIOPrint` via `subprocess` instead; do not try to build/install pygio. |
| GenericIOPrint output has no obvious "mass" column | Mass isn't stored per-particle for this sample run — compute `mp` from `Omega_m`, `rho_crit0`, `RL`, `NP` (see above) and broadcast it. |
| Picking "most massive halo" by `fof_halo_mass` gives a different halo than expected | Use `sod_halo_mass` (M_200c), excluding rows where `sod_halo_count == -101`. |
| Job finishes but `dm_density_slice.png`/`summary.txt` are missing | The embedded script probably failed silently — check the job's `.o<jobid>` log (or add `set -e` before the analysis call in the generated script) rather than assuming the producer itself failed. |
| Reading/parsing `output/full_snapshots/`/`analysis/haloproperties/` content before this run's own producer has executed | Don't — that's leftover data from some prior run, not this run's own result. Write the analysis script from the documented format/rules, not by testing against old data. |
| Analysis stage fails inside the job -- tempted to resubmit the whole PBS job | Don't requeue `hacc_tpm` just to fix an analysis bug — the producer's real output for this run already exists on disk. Fix `analyze_and_render.py` and re-run it directly against that output via `submit_shell_task`. |
| `hacc_slice` slice values look wrong / file size doesn't divide evenly | Check float32 vs float64 assumption first (`NG*NG*4` vs `NG*NG*8` bytes) rather than assuming a fixed dtype. |

---

## Output Files (in the work dir)

- `particles_step<N>.npz` / `.bp` — raw snapshot arrays (x,y,z,vx,vy,vz,phi,mass)
- `halo_catalog.npz` / `.csv` — parsed FOF+SOD halo catalog
- `most_massive_halo.npz` / `.txt` — selected halo's M_200c, R_200c, center
- `density_slice.npy` / `.bp` + metadata — the projected density grid
- `dm_density_slice.png` — final rendered image (always a plain file, never `.bp`)
- `summary.txt` — human-readable reproduction summary (config values used, results)
