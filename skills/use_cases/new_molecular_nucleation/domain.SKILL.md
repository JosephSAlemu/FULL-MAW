---
name: use_cases/new_molecular_nucleation/domain
description: >
  Molecular nucleation / water crystallization domain skill. Run the existing LAMMPS
  molecular dynamics simulation of a water box, then detect ice crystal structures in
  the resulting trajectory and render the nucleation over time — LAMMPS, OVITO,
  in.watbox, data.init, AW.tersoff, lammpstrj, diamond structure, cubic ice,
  hexagonal ice.
---

# Molecular Nucleation (LAMMPS + OVITO) — Domain Skill

Reproduce a water crystallization nucleation result: run the existing LAMMPS
simulation of a supercooled water box, then analyze its trajectory for ice-like
structure and produce per-frame renders, an animation, and a nucleation timeseries.
Applies whenever the work involves LAMMPS water crystallization, OVITO diamond
structure identification, or the `in.watbox` / `data.init` / `AW.tersoff` case files.

---

# Tools

**LAMMPS** — the molecular dynamics engine. It is already built and installed; it is
run, never rebuilt. Locally the shared library lives under `/usr/local/lib`. On the
cluster, LAMMPS is provided by the cluster itself and is MPI-linked. Use the
dedicated LAMMPS run path that selects the cluster binary or the local install
automatically — do not hand-roll an alternative way of launching it.

**OVITO** — the trajectory structure-analysis tool used to identify ice. It provides
the diamond-structure identification used to separate liquid water from cubic and
hexagonal ice. Use OVITO's own identification result; do not reimplement crystal
structure detection.

---

# Input Parameters

All simulation settings are fixed in the case files and must be used as they are —
do not invent, override, or edit them. The case files live in `/app/data/`:

- `/app/data/in.watbox` — the LAMMPS input script. It is the single source of truth
  for temperature, pressure, timestep, run length, ensemble, and output frequency.
  It is user-controlled and must never be modified.
- `/app/data/data.init` — the initial atom positions for the water box.
- `/app/data/AW.tersoff` — the Tersoff force-field parameter file for water.

All three files must be copied into the run directory *before* the simulation starts,
and `in.watbox` should always be re-copied fresh in case the user edited it.

If the paper states a parameter that differs from what is in `in.watbox`, record the
discrepancy as a literature finding, but run with the value in `in.watbox`.

**Paths.** Work happens under `/app/work/run0/`, with `/app/work/run0/frames/` for
trajectory output and `/app/work/run0/renders/` for images. Always use `/app/`-rooted
paths; never cluster-specific absolute paths such as `/lcrc/project/`, `/gpfs/`, or
`/scratch/`.

---

# Outputs

**Simulation stage**
- `/app/work/run0/frames/step.*.lammpstrj` — the trajectory frames, one file per
  dumped step, in LAMMPS text trajectory format
- `/app/work/run0/log.lammps` — the simulation log

**Analysis stage**
- `/app/work/run0/results.csv` — one row per trajectory frame, with the columns
  `frame`, `timestep`, `cubic_diamond_count`, `hexagonal_diamond_count`

**Visualization stage (all three are required — none may be skipped)**
- `/app/work/run0/renders/frame_NNNN.png` — one image per trajectory frame, atoms
  colored by whether they are liquid, cubic ice, or hexagonal ice
- `/app/work/run0/renders/animation.gif` — the frame images assembled into a looping
  animation of the crystallization
- `/app/work/run0/renders/nucleation_timeseries.png` — a line chart of cubic ice
  count and hexagonal ice count against timestep, with a total-ice line

---

# Pitfalls

| Pitfall | What to do instead |
|---|---|
| LAMMPS fails repeatedly without producing trajectory files | Verify that `/app/work/run0/` exists, contains all required input case files (`in.watbox`, `data.init`, `AW.tersoff`), and is writable. Also confirm the output trajectory path matches the simulation script configuration (e.g., `/app/work/run0/frames/`). | 
| MPI initialization fails | Confirm the Intel MPI runtime libraries are correctly sourced by running `/path/to/MCP_Approach/setup_hpc.sh` before launching the agent. For interactive PBS jobs on LCRC, ensure `mpirun` is used and relevant PBS variables (e.g., `PBS_NP`) are set and passed. |
| The simulation cannot find its initial positions or force field | Copy all three case files into the run directory *before* starting the simulation, not during it. |
| The trajectory output folder is empty after the run | The run directory was not in place, or the run failed. The trajectory paths are written relative to the run directory, so the run must actually take place inside `/app/work/run0/`. Check the simulation's own error output before assuming the analysis stage is at fault. |
| The simulation is terminated with an MPI/initialization error | This is an environment problem, not a modelling problem. Use the standard LAMMPS run path, which picks the right launch mode for the cluster or the local machine, and confirm the allocation was queried first on the cluster. |
| Ice counts come out at roughly a tenth of the expected value, or near zero | The structure identification reports a primary classification *and* neighbor classifications for each ice type. Cubic ice is the primary cubic class plus both of its neighbor classes; hexagonal ice is the primary hexagonal class plus both of its neighbor classes. Counting only the primary classes badly undercounts the crystal. |
| The renders come out empty, unreadable, or with atoms too small to see | Use a headless/offscreen rendering mode, use a clearly visible atom size rather than the default, and use the mandated colors rather than the default coloring. |
| The run reports success but `results.csv` is missing | The analysis stage did not finish writing. Verify the trajectory frames exist first, then re-run only the analysis — not the simulation. |
| The animation step reports no frames found | The per-frame images have not been produced yet. Render the frames before assembling the animation. |
| Tempted to make LAMMPS write its own analysis-friendly output format | It cannot — the trajectory format is fixed. Convert or re-read downstream instead of changing how the simulation writes. |


---

# Guidelines

- LAMMPS is a pre-built, pre-installed simulation engine. Run it and read its results;never build it, reinstall it, or reimplement any part of its physics. LAMMPS is NOT a **PYTHON PACKAGE**.
- OVITO's structure identification is the authority for what counts as ice. Never
  reimplement crystal-structure detection by hand.
- `in.watbox` is user-controlled and must be used exactly as given. If a modified
  configuration is ever needed, write a separate copy instead of editing the original.
- Anything already sitting in `/app/work/` at the start of a run is left over from a
  previous run — ignore it and do not analyze it as if it were this run's output.
- Do not re-run the simulation to fix an analysis or visualization problem. The
  trajectory already exists; fix and re-run only the stage that failed.
- The three visualization outputs (per-frame images, animation, timeseries chart) are
  all part of the result. A run that produces the numbers but not the images is
  incomplete.
- Color conventions for the renders are fixed and must not be substituted: liquid or
  unstructured water is cyan, cubic ice is blue, and hexagonal ice is red.

