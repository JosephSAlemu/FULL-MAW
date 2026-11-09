---
name: use_cases/new_molecular_nucleation/domain
description: >
  Molecular nucleation / water crystallization domain skill. Run one or many LAMMPS
  molecular dynamics simulations of a water box, then detect ice crystal structures in
  each resulting trajectory and render the nucleation over time — LAMMPS, OVITO,
  in.watbox, data.init, AW.tersoff, lammpstrj, diamond structure, cubic ice,
  hexagonal ice. How many simulations run, and on how many ranks each, comes from
  the goal.
---

# Molecular Nucleation (LAMMPS + OVITO) — Domain Skill

Reproduce a water crystallization nucleation result: run the existing LAMMPS
simulation of a supercooled water box, then analyze its trajectory for ice-like
structure and produce per-frame renders, an animation, and a nucleation
timeseries. Applies whenever the work involves LAMMPS water crystallization,
OVITO diamond structure identification, or the `in.watbox*` / `data.init` /
`AW.tersoff` case files.

## Workflow Overview

The workflow has 3 main stages:
1. **LAMMPS simulation** -- run MD simulation of water molecules, output trajectory frames
2. **OVITO analysis** -- read trajectory frames, detect ice crystal structures
3. **Visualization** -- render frames and generate plots

**The goal decides the shape of the run.** One simulation, ten, or a hundred is
the same workflow with a different instance count, and the rank count per
simulation is likewise a parameter. Nothing in this file assumes a number.

---

# Running LAMMPS: the module, the binary, the launch

`module load lammps` provides both the LAMMPS executable and the MPI it was
built against (OpenMPI), plus the matching compiler. It announces both on load
and unloads any other versions of them. Nothing else sets up MPI for this
domain -- no library paths to compose, no vendor install to name.

**The load goes in the command string of each task that launches ranks**, not in
a shell beforehand. A module loaded in the agent's shell is inherited by every
task, including OVITO, which needs no MPI at all.

**The binary name varies.** The module may call it `lmp`, `lmp_mpi`, or
`lmp_<compiler>_<mpi>`. Resolve it at run time rather than writing one in:

```
LMP=$(command -v lmp || command -v lmp_mpi || command -v lmp_g++_openmpi)
```

**The shape of the launch**, per instance:

```
module load lammps
  -> resolve $LMP
  -> cd into this instance's own directory
  -> OMP_NUM_THREADS=1   (all the parallelism is in the ranks)
  -> pin to this instance's cores
  -> <launcher> -n <ranks> $LMP -in <this instance's script> -log log.lammps
```

`-log` matters: without it the log lands wherever the process started rather
than in this instance's directory, and two concurrent instances overwrite each
other's.

After the module is loaded, the launcher it provides is the right one. There is
no second MPI to confuse it with.

**The ranks are the executable, not a Python program.** That is what keeps this
workflow free of MPI bindings: nothing re-invokes the workflow file as a rank
program, nothing needs a rank-mode flag, and nothing imports `mpi4py`.

---

# Tools

## LAMMPS

The molecular dynamics engine. How it is loaded and launched is above; what
follows is how to tell whether it worked.

**Verify every multi-rank run from `log.lammps`.** It reports the task count and
the processor grid. One task where N were asked for means the launcher did not
take, and the exit code will not tell you — the ranks run as N independent
one-rank jobs that overwrite each other's output and exit 0.

## OVITO

The trajectory structure-analysis tool used to identify ice. It provides the
diamond-structure identification that separates liquid water from cubic and
hexagonal ice. Use OVITO's own identification result; never reimplement
crystal-structure detection.

OVITO is single-process here. It reads the trajectory LAMMPS wrote and needs no
MPI at all — do not load the module for it, do not launch it with a launcher,
and do not give it ranks.

Three things about how the result must be read:

- **Each ice type has three classes, not one.** The identification reports a
  primary class plus a first-neighbour and a second-neighbour class for cubic and
  for hexagonal ice. Cubic ice is all three cubic classes added together;
  hexagonal ice is all three hexagonal classes. Counting only the primary classes
  undercounts the crystal by roughly a factor of ten.
- **Ask OVITO for the class numbers rather than writing them in.** The tool
  publishes them; a hardcoded list of numbers will be wrong if the tool changes.
- **Every rendered image must be told which frame to draw.** Omitting that
  produces a folder of identical pictures and an animation that does not move.

**CRITICAL -- IdentifyDiamondModifier structure type mapping:**

| Type | Meaning |
|---|---|
| 0 | Other (liquid, amorphous water) |
| 1 | Cubic diamond |
| 2 | Cubic diamond (1st neighbor) |
| 3 | Cubic diamond (2nd neighbor) |
| 4 | Hexagonal diamond (wurtzite ice) |
| 5 | Hexagonal diamond (1st neighbor) |
| 6 | Hexagonal diamond (2nd neighbor) |

- Cubic = types 1 + 2 + 3 (NOT just type 1)
- Hexagonal = types 4 + 5 + 6 (NOT just type 4)
- Counting only primary types gives ~10% of the actual crystal count

### OVITO's interface differs between versions

Three names and options changed between OVITO releases, and the version installed
here is on the newer side of all three. Each one stops the analysis or rendering
stage outright, after the simulation has already succeeded.

- The **first- and second-neighbour class names** are abbreviated on some builds
  and spelled out on others (`_FIRST_NEIGH` versus `_FIRST_NEIGHBOR`, same for
  `_SECOND_`). Asking OVITO for the class numbers does not help if the name used
  to ask is wrong, so accept either spelling.
- The **sort-by-timestep option** on the LAMMPS dump reader was removed and newer
  readers reject it. It is not needed: a wildcard file sequence is ordered by the
  number the wildcard matches, and `step.<N>.lammpstrj` is already in timestep
  order. Confirm from the `timestep` column of `results.csv`, which should come
  out monotonically increasing.
- The **modifier that runs a user-supplied colouring function** is called
  `PythonScriptModifier` on older builds and `PythonModifier` on newer ones.

Both the analysis stage and the rendering stage open the trajectory and run the
identification, so all three apply in **two** places per simulation. Fixing one
stage and not the other moves the failure one stage later rather than removing
it.

Rendering also adds the trajectory to OVITO's scene, which is shared by
everything in the same process. It must be removed again even when rendering
fails, or the next piece of work inherits it; the engine skill covers how.

---

# Rendering is the expensive stage

Rendering dominates this workflow's wall clock, and the two biggest costs are
both choices rather than requirements.

**A scatter plot, not a ray tracer, unless the goal asks for one.** A 3D scatter
of the atom positions, coloured by structure type, satisfies every rule in the
table above and rasterizes in milliseconds. A CPU ray tracer casts rays per
pixel and costs orders of magnitude more for the same information. Reach for a
renderer only when the goal names one.

**When a ray tracer IS required, turn off what it does for free elsewhere.**
Ambient occlusion and antialiasing each multiply the ray count and contribute
nothing to flat-coloured structure renders -- the colours are categorical, not
shaded. Shadows and depth of field likewise. Set them off defensively, by
attribute name, because these options get renamed between releases just as the
modifier names do.

**Build the renderer once per task, not once per frame.** Constructing it inside
the per-frame loop pays its setup on every image.

**Split the frames across workers.** Frames are independent, so a render stage
that loops over all of them in one task leaves most of the allocation idle
through the longest part of the run. Fan the frames out; the engine skill covers
the shape.

**Do not identify structures twice.** The analysis stage already runs the
identification over every frame. Having it return the frame count alongside its
CSV lets the render stage fan out without a second pass to discover how many
frames there are.

---

# Scaling the run

Three numbers come from the goal, not from this file:

| Quantity | Where it comes from |
|---|---|
| How many simulations | the goal, or the number of input scripts it names |
| Which input script each uses | the goal, or discovered by pattern under `/app/data/` |
| Ranks per simulation | the goal; if unstated, divide the allocation by the simulation count |

Write the workflow so all three are variables. A file that hardcodes two
simulations cannot answer a goal asking for ten, and one that hardcodes ten
wastes an allocation on a goal asking for one.

**Discovering the inputs.** When the goal names scripts, use those. When it only
says how many, glob `/app/data/in.watbox*` and take them in sorted order — never
invent a filename that is not on disk.

**Naming the instances.** Each simulation gets its own directory under
`/app/work/run0/`, named for its index or its input script, with its own `frames`
and `renders` subdirectories. Instances share nothing: no directory, no output
file, no OVITO process.

**Sizing against the allocation.** Simulations × ranks-per-simulation must not
exceed the slots the allocation holds. When it would, either reduce the rank
count or run the simulations in batches rather than all at once — both are
correct, and which to prefer depends on whether the goal cares more about
wall-clock per simulation or total throughput. Over-subscribing makes every
simulation slower and is never the right answer.

**Separating concurrent simulations.** Two launchers started from the same place
claim rank slots from the front of the same list, so without separation both
simulations land on the same cores. On one node, give each a disjoint core range
with `taskset -c`. The engine skill covers the shapes.

**Independence.** Every simulation is independent of every other. Start as many
as the allocation allows at once, and connect each analysis and rendering stage
only to its own simulation. One simulation failing must not stop the others.

---

# Input Parameters

All simulation settings are fixed in the case files and must be used as they are —
do not invent, override, or edit them. The case files live in `/app/data/`:

- `/app/data/in.watbox*` — the LAMMPS input scripts, one per simulation. Each is
  the single source of truth for temperature, pressure, timestep, run length,
  ensemble, and output frequency for its simulation. They are user-controlled and
  must never be modified.
- `/app/data/data.init` — the initial atom positions for the water box, shared by
  all simulations.
- `/app/data/AW.tersoff` — the Tersoff force-field parameter file for water,
  shared by all simulations.

For each simulation, all three files must be copied into that simulation's run
directory **before** it starts, and the input script should always be re-copied
fresh in case the user edited it. Different simulations may use different input
scripts but the same `data.init` and `AW.tersoff`.

If the paper states a parameter that differs from what is in the input script,
record the discrepancy as a literature finding, but run with the value in the
script.

**How often frames are written is decided by the input script, and nowhere else.**
Do not assume a step number. Anything that names a specific frame file — for
example a file ending `step.1000.lammpstrj` — is a guess, and the run fails on it
even when the simulation succeeded. Wait on the simulation log instead, which is
always written, and find the frames by pattern afterwards. Different input
scripts may dump at different intervals, so do not assume two simulations produce
the same number of frames.

The same script sets how much work the visualization stage has: the frame count
is the run length divided by the dump interval. Every frame gets its own render,
and rendering is the longest stage of this workflow by a wide margin — with many
simulations it dominates the run.

**Paths.** Work happens under `/app/work/run0/`, with one subdirectory per
simulation. Always use `/app/`-rooted paths for anything the run produces; never
`/lcrc/project/`, `/gpfs/`, or `/scratch/`.

---

# Outputs

Per simulation, in that simulation's own directory:

**Visualization rules (MANDATORY -- do not deviate).** Stated as intent plus the
spelling each tool uses, because the two renderers below take different keywords
for the same thing:

| Requirement | Scatter plot | OVITO renderer |
|---|---|---|
| Atoms clearly visible at the image size | `s=25` minimum (a default near 2 makes them invisible) | a per-particle `Radius` large enough to see |
| Liquid / Other (type 0), cyan, mostly transparent | `#00BFFF`, `alpha >= 0.3` | `#00BFFF`, `Transparency <= 0.7` |
| Cubic diamond (types 1-3), blue, near-opaque | `#0000FF`, `alpha >= 0.8` | `#0000FF`, `Transparency <= 0.2` |
| Hexagonal diamond (types 4-6), red, near-opaque | `#FF2200`, `alpha >= 0.8` | `#FF2200`, `Transparency <= 0.2` |

Do NOT use either tool's default colouring.

**Simulation stage**
- `frames/step.*.lammpstrj` — the trajectory frames, one file per dumped step, in
  LAMMPS text trajectory format
- `log.lammps` — the simulation log, including the MPI decomposition line

**Analysis stage**
- `results.csv` — one row per trajectory frame, with the columns `frame`,
  `timestep`, `cubic_diamond_count`, `hexagonal_diamond_count`

**Visualization stage (all three are required — none may be skipped)**
- `renders/frame_NNNN.png` — one image per trajectory frame, atoms coloured by
  whether they are liquid, cubic ice, or hexagonal ice
- `renders/animation.gif` — the frame images assembled into a looping animation
  of the crystallization
- `renders/nucleation_timeseries.png` — a line chart of cubic ice count and
  hexagonal ice count against timestep, with a total-ice line

Every simulation produces the full set. A run that completes the simulations but
delivers these for only some of them is incomplete.

The frames and `log.lammps` are written relative to the directory the simulation
started in, and a previous run's copies will satisfy a check that they exist.
Delete each simulation's own frames and log at the start of that simulation so
the analysis stage reads this run's output.

---

# Pitfalls

| Pitfall | What to do instead |
|---|---|
| Pip-installing `lammps`, or calling the wheel's bundled `lmp` | That binary does not load on this cluster's kernel (elf segment-layout mismatch). Use the module's `lmp` |
| Adding `mpi4py` to the stack | The ranks are a compiled binary. Nothing here imports MPI bindings, so nothing needs them |
| Writing one `lmp` binary name into the command | The module may call it `lmp`, `lmp_mpi` or `lmp_<compiler>_<mpi>`. Resolve it with `command -v` at run time |
| Re-invoking the workflow file as the rank program | Only needed when the ranks are Python. Here they are the `lmp` executable |
| Ray-tracing every frame when the goal did not ask for one | A scatter plot satisfies every colour rule and is orders of magnitude cheaper |
| Leaving ambient occlusion and antialiasing on in a ray-traced render | Both multiply the ray count and add nothing to flat categorical colours. Turn them off by attribute name |
| Constructing the renderer inside the per-frame loop | Build it once per task; per frame it pays setup on every image |
| A render stage that loops over every frame in one task | Frames are independent. Split them across workers or the longest stage runs on one core |
| Running the structure identification in both the analysis and render stages | It is then paid twice over the whole trajectory. Return the frame count from analysis so render needs no second pass |
| Zooming the viewport once before the loop and never again | It frames whichever frame is current. If the box grows during crystallization, late frames clip -- check the last render |
| Treating a nonzero exit code as a failed simulation | A SIGSEGV during LAMMPS cleanup (exit 11) arrives after the frames were written. Check for trajectory frames and fail only when none exist |
| Not loading the lammps module in the task's command string | Without it there is no LAMMPS and no MPI. The load belongs in the command, with the launcher it provides |
| Loading the module in a shell before the run instead of per task | Every task then inherits it, including OVITO, which needs no MPI at all |
| Trusting a multi-rank run without checking `log.lammps` | It reports the task count and processor grid. One task when N were asked for means the launcher did not take, and the exit code is 0 either way |
| Giving OVITO ranks, a launcher, or the module | It is single-process and reads files LAMMPS already wrote |
| Inventing an input-script filename | Use the scripts the goal names, or glob `/app/data/in.watbox*` in sorted order |
| Assuming every simulation produces the same frame count | Each input script sets its own dump interval. Count frames per simulation |
| Two concurrent simulations with no core separation | Both claim rank slots from the front of the same list. Give each a disjoint `taskset -c` range |
| One simulation's failure stopping the others | They are independent. Wire each analysis stage only to its own simulation |
| Writing a helper script, a probe, or any second program file | One generated file only. If a fact about a tool is missing, it belongs in this file, not in something the run executes |
| Naming a specific frame file, such as `step.1000.lammpstrj` | The frame interval is in the input script. Wait on `log.lammps` and find the frames by pattern |
| Ice counts about ten times too low, or near zero | Add the first- and second-neighbour classes to the primary class for each ice type |
| An error naming a structure-type class, a dump-reader option, or the user-function modifier | OVITO renamed all three between versions. Accept either name, and drop the sort-by-timestep option |
| Fixing an OVITO call in the analysis stage only | The rendering stage opens the trajectory the same way. Both stages need the same fix |
| Every rendered image looks the same | Each image must be told which frame to draw |
| Writing the structure class numbers in by hand | Ask OVITO for them |
| Renders empty, unreadable, or atoms too small to see | Render without a display, set a clearly visible atom size, and use the required colours rather than the defaults |
| A simulation's trajectory folder is empty after the run | It must run inside its own directory — output paths are relative to where it started. Read its `log.lammps` before blaming the analysis stage |
| Analyzing frames a previous run left behind | Delete each simulation's own frames and log at the start of that simulation |
| The case files are missing when a simulation starts | Copy all three into its run directory before starting it, not during |
| A run reports success but `results.csv` is missing for some simulation | That analysis stage did not finish. Confirm its frames exist, then re-run only that stage |
| The animation step reports no frames found | Render the per-frame images before assembling the animation |
| Re-running a simulation to fix an analysis or image problem | The trajectory already exists. Fix and re-run only the stage that failed |
| Trying to make LAMMPS write an analysis-friendly format | It cannot; the trajectory format is fixed. Convert it afterwards instead |

---

# Guidelines

- LAMMPS comes from `module load lammps` as an executable, and is never
  pip-installed. Run it and read its results; do not build it from source and do
  not reimplement any part of its physics. The ranks being a binary is what
  keeps this workflow free of MPI bindings entirely.
- OVITO's structure identification is the authority for what counts as ice. Never
  reimplement crystal-structure detection by hand.
- The input scripts are user-controlled and must be used exactly as given. If a
  modified configuration is ever needed, write a separate copy instead of editing
  the original.
- The number of simulations and the ranks each gets are inputs to the workflow,
  not properties of it. Read them from the goal and pass them as variables.
- Anything already sitting in `/app/work/` at the start of a run is left over from
  a previous run — ignore it and do not analyze it as if it were this run's output.
- Do not re-run a simulation to fix an analysis or visualization problem.
- The three visualization outputs (per-frame images, animation, timeseries chart)
  are part of the result for **every** simulation. Numbers without images is an
  incomplete run.
- Colour conventions for the renders are fixed and must not be substituted:
  liquid or unstructured water is cyan, cubic ice is blue, hexagonal ice is red.
- Simulations are independent of one another. Run as many concurrently as the
  allocation allows, and because OVITO's scene is shared within a process, never
  let two of them share one — see the engine skill for how to keep them apart.