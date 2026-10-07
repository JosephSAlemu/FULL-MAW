---
name: use_cases/cosmology/explorer
description: >
  Use-case-specific explorer rules for the HACC cosmology workflow (Last Journey
  sample run). Covers expressing the producer AND the analysis/rendering as one
  workflow handed to the engine -- GenericIO reading via CLI tools (no pygio),
  halo catalog parsing, and known pitfalls.
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

## Overall Shape: One Workflow, Sim + Vis Together

The producer and the analysis/visualization are **one workflow**, expressed to the
workflow engine in a single generated file, and the engine runs it.

**Never write a PBS script, never call `qsub`/`qstat`, and never launch a new batch
job.** You are already inside an allocation. The producer is a command-line MPI
program, so it is a CLI step the engine runs; the analysis/rendering is pure Python,
so it is a Python step the engine runs. On the parsl engine that means one file with
a `@bash_app` for the producer and a `@python_app` for the analysis — see
`systems/parsl` for the exact shape.

Do not develop or test the analysis code against `SampleRun_go/output/full_snapshots/`
or `SampleRun_go/analysis/haloproperties/` content that's already sitting there from
some prior run — that's someone else's leftover output, not this run's, and this
run's result should never depend on it. Write the analysis from the documented
GenericIO format and halo-selection rules below, informed by this run's own
`params/indat.params` (config, not output).

---

## Stage 1: Explore the Case (config only, not old output)

`list_files`/explore `SampleRun_go/` (list `subme.pbs`, `params/`, `output/`,
`analysis/` -- just to confirm they exist, not to read snapshot/halo content from
them). Config values (FOF linking length, SOD Delta, box size `RL`, grid size `NG`)
live in `params/indat.params` and `params/cosmotools-config.dat` — read them, never
hardcode values from the paper without confirming they match this sample run. These
are this run's own input configuration, not another run's results, so reading them
is fine.

Read `SampleRun_go/subme.pbs` too — but **only** as a reference for the executable
path, env file, param file location, and rank count. You are reading it for those
values, not copying its structure. Never submit it, never adapt it, never write a
file like it.

---

## Stage 2: The Producer Step (CLI, run by the engine)

The producer is `hacc_tpm` under MPI. Express it as a single command string for the
engine to run, built from the paths read in Stage 1:

- `envfile` must be sourced **before** the executable, in the same command
- `cd` into `SampleRun_go/` in the same command, so `./params/indat.params`
  resolves relatively
- `NRANKS` comes from the task; default to **8** (this sample run's 2x2x2
  decomposition) if unspecified. `WALLTIME` is irrelevant now — there is no batch
  job to request walltime for.

The resulting command has the shape:

```
source <envfile> && cd <SampleRun_go> && mpirun -n <NRANKS> <exe> ./params/indat.params -n
```

On parsl this string is the return value of a `@bash_app`. You never run it yourself.

---

## Stage 3: The Analysis/Rendering Step (Python, run by the engine)

Implement the analysis using the documented GenericIOPrint format and halo-selection
rules ("Reading Snapshot & Halo Data" and "Computing and Rendering the Density Slice"
below) plus the Stage 1 config values — not by reading or testing against any
pre-existing snapshot/halo catalog content.

It must run **after** the producer. Express that ordering to the engine (on parsl,
resolve the producer's future before invoking the analysis app, or pass the future
in as a dependency). Do not rely on wall-clock timing or polling.

Paths inside this step:
- **Inputs** are the real absolute `SampleRun_go/output/...` and
  `analysis/haloproperties/...` paths — where *this run's own* producer just wrote.
- **Outputs** go to `/app/work/run0/`.

Put all imports inside the function body, and set `matplotlib.use("Agg")` before
importing `pyplot` — the node is headless.

---

## Stage 4: Report, or Recover Without Re-Running the Producer

After the workflow completes, read back `/app/work/run0/dm_density_slice.png` and
`summary.txt` to confirm they exist and report them.

If they're missing, read the workflow's stdout/stderr to find where it failed:
- **If the producer (`hacc_tpm`) itself failed**, fix the underlying issue (wrong
  param path, wrong rank count, env not sourced) and re-run the workflow — the
  producer has to actually run again to get real output.
- **If the producer succeeded but the analysis/rendering failed**, do **not** re-run
  the producer. `hacc_tpm` already wrote real output for this run to
  `output/full_snapshots/`/`analysis/haloproperties/`. Write a workflow containing
  **only** the corrected analysis step, pointed at that existing output, and run
  that. This is still *this run's* own fresh data, and it avoids repeating the
  expensive MPI producer just to fix a bug in the analysis code.

---

## Reading Snapshot & Halo Data — Do NOT use pygio

`pygio` (in-tree at `HACC_go/submodules/genericio/python/pygio`) is **not built** —
importing it fails with `ModuleNotFoundError: No module named 'pygio._version'`.
Building it means compiling a C++ extension against the GenericIO libs — do not
attempt this. Instead use the pre-built CLI binary:

```
GIOP = "/lcrc/project/PEDAL/jalemu/HACC/HACC_go/improv.cpu/frontend/bin/GenericIOPrint"
```

`GenericIOPrint` is a CLI tool, so **the engine runs it, not you** — never call it
with `subprocess`. Make it its own CLI step that redirects stdout to a text file in
the work dir, then parse that file in the Python step:

```
<GIOP> <SNAP> > /app/work/run0/snapshot_dump.txt
```

On parsl that command string is a `@bash_app`, ordered before the `@python_app` that
parses it. (`import subprocess` is rejected outright by `write_workflow`.)

### Parsing the particle snapshot dump
```python
# inside the @python_app, after the GenericIOPrint step has written the dump
import numpy as np

rows = []
with open("/app/work/run0/snapshot_dump.txt") as fh:
    for ln in fh:
        s = ln.strip()
        if s.startswith("#") or not s:
            continue
        parts = s.split()
        if len(parts) < 7:
            continue
        rows.append([float(parts[i]) for i in range(7)])  # x,y,z,vx,vy,vz,phi
arr = np.array(rows, dtype=np.float64)
```
- The snapshot to dump is e.g.
  `SampleRun_go/output/full_snapshots/step_624/m000p.full.mpicosmo.624`.
- Pass the **master snapshot file**, not a per-rank shard — `GenericIOPrint` reads
  across all ranks for you.
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
- Dump it the same way as the snapshot: a CLI step running `GenericIOPrint` on it,
  redirected to a text file, then parsed in the Python step.
- This file is written by HACC's own halo finder as part of the run, under
  `analysis/haloproperties/step_<N>/` — do not hand-roll FOF/SOD linking in Python;
  parse this output instead. (This path won't have this run's real content until
  `hacc_tpm` has actually executed — so this step must be ordered after the
  producer step.)
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

When the run uses `--engine adios`, routing the inter-stage arrays through
ADIOS2 is **required, not an optional enhancement.** `import adios2` in the
generated workflow file and use `adios2.Stream(path, mode)` directly, with a
genuine `stream.write(...)` in the producing stage and `stream.read(...)` in
the consuming stage — write the array to the `.bp` file and then actually read
it back before using it, rather than writing it and continuing with the
in-memory copy.

This applies to the real inter-stage numerical data: `particles_step<N>.bp`
(the snapshot arrays x,y,z,vx,vy,vz,phi,mass) and `density_slice.bp` (the
projected density grid), matching the `.bp` variants listed under Output Files
below.

`halo_catalog` and `most_massive_halo` stay `.csv`/`.txt` — they're small,
one-shot metadata reads from HACC's own halo-finder output, not numerical
arrays flowing between stages. `dm_density_slice.png` and `summary.txt` stay
plain human-facing files regardless of engine.

---

## Common Pitfalls

| Pitfall | Solution |
|---|---|
| Tempted to write a `.pbs` script or call `qsub`/`qstat` | Never. You are already inside an allocation — the engine runs the producer directly. |
| `pygio` import fails (`No module named 'pygio._version'`) | Expected — it's not built. Dump with `GenericIOPrint` as a CLI step instead; do not try to build/install pygio. |
| Reaching for `subprocess` to call `GenericIOPrint` | Rejected by the engine. Make it a CLI step that redirects stdout to a file, then parse that file in the Python step. |
| `hacc_tpm` fails immediately / env errors | `envfile` must be sourced in the *same* command, before the executable — a separate step won't carry the environment over. |
| GenericIOPrint output has no obvious "mass" column | Mass isn't stored per-particle for this sample run — compute `mp` from `Omega_m`, `rho_crit0`, `RL`, `NP` (see above) and broadcast it. |
| Picking "most massive halo" by `fof_halo_mass` gives a different halo than expected | Use `sod_halo_mass` (M_200c), excluding rows where `sod_halo_count == -101`. |
| Workflow finishes but `dm_density_slice.png`/`summary.txt` are missing | The analysis step probably failed — read the workflow's stderr rather than assuming the producer itself failed. |
| Analysis runs before the producer has written output | The ordering wasn't expressed to the engine. Make the analysis step depend on the producer step; don't rely on timing. |
| Reading/parsing `output/full_snapshots/`/`analysis/haloproperties/` content before this run's own producer has executed | Don't — that's leftover data from some prior run, not this run's own result. Write the analysis from the documented format/rules, not by testing against old data. |
| Analysis stage fails -- tempted to re-run the whole workflow | Don't repeat `hacc_tpm` just to fix an analysis bug — the producer's real output for this run already exists on disk. Run a workflow with only the corrected analysis step against that output. |
| `hacc_slice` slice values look wrong / file size doesn't divide evenly | Check float32 vs float64 assumption first (`NG*NG*4` vs `NG*NG*8` bytes) rather than assuming a fixed dtype. |

---

## Output Files (in the work dir)

- `particles_step<N>.npz` / `.bp` — raw snapshot arrays (x,y,z,vx,vy,vz,phi,mass)
- `halo_catalog.npz` / `.csv` — parsed FOF+SOD halo catalog
- `most_massive_halo.npz` / `.txt` — selected halo's M_200c, R_200c, center
- `density_slice.npy` / `.bp` + metadata — the projected density grid
- `dm_density_slice.png` — final rendered image (always a plain file, never `.bp`)
- `summary.txt` — human-readable reproduction summary (config values used, results)
