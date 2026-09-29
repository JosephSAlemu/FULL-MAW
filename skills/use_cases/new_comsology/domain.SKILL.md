---
name: use_cases/new_comsology/domain
description: >
  Cosmology (HACC / "The Last Journey" sample run) domain skill. Run the existing
  HACC simulation via its PBS script, then read its GenericIO output and render a
  dark-matter density slice around the most massive halo.
---

# Cosmology (HACC) — Domain Skill

Reproduce a HACC cosmological N-body result: run the existing HACC simulation, then
analyze its halo output and produce a dark-matter density-slice image. Applies
whenever the work involves HACC, GenericIO snapshots, FOF/SOD halo catalogs, or "The
Last Journey" sample run.

---
 <!-- Don't talk too much about the workflow, that's more in the goal. Skill file is more for the input and output file information-->
## Task Details

HACC is a closed source binary

**Inputs / config.** HACC's simulation inputs are already fixed in its own config
files — do not invent or override them. When the analysis needs physical values (box
size `RL`, grid size `NG`, FOF linking length, SOD overdensity `Delta`, cosmology
parameters), read them from HACC's own config files, not from a paper (the sample run
is downscaled and may not match published values):

- `params/indat.params`
- `params/cosmotools-config.dat`

Do not read, parse, or test the analysis against files already in `output/` or
`analysis/` before the simulation runs — those are leftover results from a previous
run. The case files live at `/lcrc/project/PEDAL/jalemu/HACC/SampleRun_go/`.

**Reading the output.** Use the pre-built `GenericIOPrint` command-line tool (not the
`pygio` module, which is not built).

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

**Visualization.** Bin the particles' (x, y) into a mass-weighted 2D histogram,
restricted to particles within a slice thickness (default 4 Mpc/h) around the halo z:


Render, save a PNG, and write
a short text summary of the config values used and the result.

---

## Expected Output

- `particles_step<N>` — raw snapshot arrays (x, y, z, vx, vy, vz, phi, mass)
- `halo_catalog` — parsed FOF+SOD halo catalog
- `most_massive_halo` — the selected halo's M_200c, R_200c, and center
- `density_slice` — the projected density grid
- `dm_density_slice.png` — the final rendered image
- `summary.txt` — human-readable summary (config values used, results)

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
- Don't change any parameters, config, or scripts that already exist. If a modified
  config is ever needed, write a separate copy instead of editing the original.
