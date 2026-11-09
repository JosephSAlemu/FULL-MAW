---
name: use_cases/new_comsology/domain
description: >
  Reproduce a HACC cosmological N-body result: run the existing HACC simulation, then
  analyze its halo output and produce a dark-matter density-slice image. Applies
  whenever the work involves HACC, GenericIO snapshots, FOF/SOD halo catalogs, or "The
  Last Journey" sample run.
---

# Cosmology (HACC) — Domain Skill

# Modules

HACC is built with OpenMPI 4.1.7. is built with gcc-13.2.0.
Order of loading modules:
`module load gcc/13.2.0`
`module load openmpi/4.1.7`

# Tools
**HACC** - a closed source binary. Do not recreate it using Python.

HACC is a command-line MPI program. It runs on the number of total specified ranks and compute nodes specified in the goal. The env file must be sourced before the executable in the same invocation — otherwise the run fails.

**HACC File Paths**

exe=`/lcrc/project/PEDAL/HACC/HACC_go/improv.cpu/mpi/bin/hacc_tpm`
envfile=`/lcrc/project/PEDAL/HACC/HACC_go/env/bashrc.improv.cpu`
paramfile=`/home/jalemu/FULL-MAW/data/indat.params`
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
- The steps at which HACC writes full particle snapshots and halo catalogs are listed separately in indat.params, and they are not the final step. In the sample configuration, snapshots are written at steps 205 and 310, halo properties at step 205, and only the power spectrum at the final step 624. Read both step lists from indat.params and use a step that appears in both, normally the latest common one. Never assume the final step, and never take the step from the paper.
- The snapshot and halo output steps are set by files, not by the paper or by fixed numbers in this skill. Take the full-snapshot step list from indat.params. Take the halo-properties step list from the halo-steps file named in the CosmoTools configuration that indat.params references (in the sample run this is params/halosteps.dat, which lists 624). Read both from those files on every run.

# Outputs
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
|Reading a halo property fails with IndexError: only integers, slices (':'), ellipsis ('...'), numpy.newaxis ('None') and integer or boolean arrays are valid indices | A column name was used directly as an array index. Every column has to go through the name-to-index map built from the # header, including the radius: index with col["sod_halo_radius"], never with "sod_halo_radius" itself. Look up sod_halo_mass, sod_halo_count, sod_halo_radius and fof_halo_center_x/y/z the same way, and raise if a name is missing from the header rather than guessing its position. R_200c is in the column named sod_halo_radius — the catalog has no column called R_Delta or R_200c.|
| Tempted to re-run the whole job to fix an analysis bug | Don't re-run the simulation — its output already exists. Fix the analysis script and re-run only that step. |
| The reference PBS job file lists the executable, environment file, and parameter paths under a different user's project tree (e.g. a `jacoboh` path) than the sample-run tree actually being used (e.g. the `jalemu` tree). | Do not copy the paths out of the reference PBS file verbatim. Use the executable, environment file, and parameter file that live under the same sample-run and HACC build tree you are actually running from, so the binary and its build environment match the run. |
| The simulation errors or hangs when the build-environment file is not sourced in the same invocation as the executable, or when the parameter file is given as a relative path but the working directory is not the sample-run directory that contains it. | Launch the simulation with the environment file sourced immediately before the executable in one combined invocation, and set the working directory to the sample-run directory so the relative parameter path resolves. Run the simulation exactly once per run. |
| The catalog reader hangs or produces nothing when pointed at a per-rank catalog shard (files ending in `#0`, `#1`, ...) or when it is not run as its own standalone command-line step. | Point the reader at the single master halo-properties file for the target step (the name without any `#N` suffix); the per-rank shards are read automatically. Run the reader as its own command-line step ordered after the producer, not nested inside another process. |

---

## Guidelines (optional)

- HACC is an existing, closed-source private simulation — run it and read its results;
  never rebuild, regenerate, or reimplement its physics.
- HACC's simulation inputs are already fixed in its own config
files — do not invent or override them. When the analysis needs physical values (box
size `RL`, grid size `NG`, FOF linking length, SOD overdensity `Delta`, cosmology
parameters), read them from HACC's own config files, not from a paper (the sample run
is downscaled and may not match published values)
- The producer writes its results under a `hacc_run/` subfolder of the working directory, with full particle snapshots under `output/full_snapshots/step_<step>/` (one master file plus eight `#0`..`#7` per-rank shards) and the halo catalog under `analysis/haloproperties/step_<step>/` as `m000p-<step>.haloproperties` (master file) plus eight per-rank shards; SOD property bins appear under `analysis/sodpropertybins/step_<step>/`.
- HACC run directory (under the run's hacc_run folder): output/full_snapshots/step_<S>/m000p.full.mpicosmo.<S> holds the particle snapshot, and analysis/haloproperties/step_<S>/m000p-<S>.haloproperties holds the halo catalog. Both are GenericIO files with one small master file (about 3 KB) plus one shard per MPI rank, numbered #0 to #<ranks-1>, so a 64-rank run gives #0..#63. The folder also contains analysis/sodpropertybins/step_<S>/ (SOD radial profiles) and output/power_spectra/m000p.pk.<step> (power spectra, written at more steps than the snapshots, including the final step).
- hacc_run/output/full_snapshots/step_<S>/ — one GenericIO particle snapshot per configured snapshot step (in the sample run: 205, 310 and the final step 624). Each holds a master file plus one shard per MPI rank (64 for an 8x8 run). The final-step folder is normally the one used for the slice.

---

# Guidelines

- The reference PBS job file is for reference only: never modify it, never submit it, and never fall back to running the workflow by submitting a batch script. The producer and the catalog reader are each a single command-line step that the workflow runs directly; the whole producer–analysis–visualization sequence must be expressible as ordered command-line steps in one run without any batch submission.
Before naming the producer's expected output files, confirm the snapshot and halo-catalog step from indat.params. If the expected master files are missing after a run, first list the step_<S> folders that were written before concluding the simulation failed. Never re-run the simulation just because the expected step was guessed wrong.
Do not hard-code or forbid particular time steps. The workflow's result should represent the end of the simulation whenever the configuration writes snapshots and halos there. Any expected step numbers in this skill are examples only; the configuration files and the step folders on disk take precedence.

---

# Pitfalls

| The run 'fails' right after the simulation even though hacc_run is full of snapshot and halo files | The workflow looked for snapshot and catalog files at the final step, or expected one shard per node. Check the output step against the snapshot and halo-property step lists in indat.params and against the step_<S> folders actually written. Expect exactly one shard per MPI rank (64 for 8 nodes x 8 ranks), not one per node. |
| The slice and halo catalog come from an intermediate step (e.g. 205 or 310) even though the simulation ran to the final step (624) and wrote a full snapshot there | Choose the latest step that has both a full snapshot folder and a halo-properties folder actually written under the run directory. When the final step has both, it is the analysis step. Do not exclude the final step on the assumption that it holds only a power spectrum. |





