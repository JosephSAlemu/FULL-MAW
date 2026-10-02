---
name: use_cases/new_comsology/domain
description: >
  Reproduce a HACC cosmological N-body result: run the existing HACC simulation, then
  analyze its halo output and produce a dark-matter density-slice image. Applies
  whenever the work involves HACC, GenericIO snapshots, FOF/SOD halo catalogs, or "The
  Last Journey" sample run.
---

# Cosmology (HACC) — Domain Skill

# Tools
**HACC** - a closed source binary. Do not recreate it using Python.

HACC is a command-line MPI program. It runs on **8 MPI ranks**, and the envfile
must be sourced before the executable in the same invocation — otherwise the run
fails.

**HACC File Paths**

exe=`/lcrc/project/PEDAL/HACC/HACC_go/improv.cpu/mpi/bin/hacc_tpm`
envfile=`/lcrc/project/PEDAL/HACC/HACC_go/env/bashrc.improv.cpu`
paramfile=`/lcrc/project/PEDAL/HACC/SampleRun_go/params/indat.params`
output=`/lcrc/project/PEDAL/HACC/SampleRun_go/params/output/__TYPE__/__STEP__/m000p`

**HACC Path Explanations**

exe:  the path of the HACC executable


envfile: the file of environment variables that must be loaded before using HACC.


paramfile:  the file that is run with the HACC executable


output: is a path prefix for all output files; where all the ouput files are for HACC.

---

# Input Parameters
## HACC
Input file is already stated as paramfile

## Visualization
Takes in the particle snapshot and the halo catalog file to do the following:
- Get the particle mass
- Then computes and renders the density slices.
- Lastly, Bin the particles' (x, y) into a mass-weighted 2D histogram, restricted to particles within a slice thickness (default 4 Mpc/h) around the halo z:

# Ouputs
## HACC
The output of HACC are the following:
- *Particle snapshot* (e.g. `output/full_snapshots/step_624/m000p.full.mpicosmo.624`):
  read the master file (not a per-rank shard), skip `#` comment lines, take columns
  `x, y, z, vx, vy, vz, phi`. Mass is not a column — compute it:
  `mp = Omega_m * rho_crit0 * (RL / NP)**3`, with `rho_crit0 = 2.77536627e11`
  (h^2 Msun/Mpc^3) and `NP` = particles per side (`64` for this sample, `64^3` total).
- *Halo catalog* (e.g. `analysis/haloproperties/step_624/m000p-624.haloproperties`):
  written by HACC's own halo finder — parse it, do not reimplement FOF/SOD. Read the
  tab-separated header for column order. **Most massive halo = row with the largest
  `sod_halo_mass` (M_200c), excluding rows where `sod_halo_count == -101`.** Use that
  halo's z-coordinate as the slice center.

## Visualization
The visualization outputs are the following files:
- `summary.txt` — human-readable summary (config values used, results)
- `dm_density_slice.png` — the final rendered image


---

## Common Pitfalls

| Pitfall | Solution |
|---|---|
| `pygio` import fails (`No module named 'pygio._version'`) | Expected — it is not built. Use `GenericIOPrint` instead; do not build or install pygio. |
| No "mass" column in the snapshot output | Mass is not stored per particle — compute `mp` from `Omega_m`, `rho_crit0`, `RL`, `NP`. |
| "Most massive halo" by `fof_halo_mass` picks the wrong halo | Use `sod_halo_mass` (M_200c), excluding rows where `sod_halo_count == -101`. |
| Job finishes but PNG/summary missing | The analysis step likely failed — check the job log rather than assuming the simulation failed. |
| Using leftover `output/`/`analysis/` data before the simulation ran | Don't — that is old data from a prior run. Write the analysis from the rules above, not from old output. |
| Tempted to re-run the whole job to fix an analysis bug | Don't re-run the simulation — its output already exists. Fix the analysis script and re-run only that step. |

---

## Guidelines (optional)

- HACC is an existing, closed-source private simulation — run it and read its results;
  never rebuild, regenerate, or reimplement its physics.
- HACC's simulation inputs are already fixed in its own config
files — do not invent or override them. When the analysis needs physical values (box
size `RL`, grid size `NG`, FOF linking length, SOD overdensity `Delta`, cosmology
parameters), read them from HACC's own config files, not from a paper (the sample run
is downscaled and may not match published values)
