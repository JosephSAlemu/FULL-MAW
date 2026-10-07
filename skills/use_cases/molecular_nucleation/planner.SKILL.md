---
name: use_cases/molecular_nucleation/planner
description: >
  Molecular nucleation extraction rules for the planner. Covers what parameters to extract
  from LAMMPS water crystallization papers, the correct stack_decision for this project,
  and how to write the workflow steps the explorer hands to the engine as one generated file.
---

# Molecular Nucleation — Planner Skill

Extraction and task-writing rules specific to the LAMMPS water crystallization workflow.

---

## When to Use This Skill

Load when planning a molecular nucleation or water crystallization workflow. Provides the exact parameter names, stack constraints, and task templates for this project.

---

## What to Extract from the Paper

### Simulation parameters to find and record
- Temperature (K) — e.g., "180 K undercooling"
- Timestep (ps) — e.g., "dt = 0.01 ps"
- Run length (steps) — e.g., "9000 steps"
- Ensemble — NPT, NVT, NVE
- Pressure (atm or bar) — for NPT runs
- Thermostat / barostat coupling constants
- Force field name — e.g., "Tersoff AW potential", "TIP4P/Ice", "SPC/E"
- System size — number of atoms or box dimensions
- Seed (if stated)

### Software tools to identify
- MD engine: LAMMPS (always present in this project)
- Structure analysis: OVITO with IdentifyDiamondModifier
- Workflow orchestration: whichever engine this run was started with (`--engine parsl`/`pycompss`/`adios`)
- Any post-processing tools

---

## Stack Decision

The venv provides EXACTLY these pip-installable packages. Use only these:

| Package | Version constraint |
|---|---|
| ovito | (latest installed) |
| numpy | (latest installed) |
| matplotlib | (latest installed) |
| pillow | (required for GIF generation) |

**Engine-specific package (depends on `--engine`):**
- `--engine parsl`: add `parsl>=2024.0.0` -- normal pip package, hard requirement.
- `--engine pycompss`: do NOT add `pycompss` -- it is never pip-installed by the
  installer (see `systems/pycompss` skill); the server detects the hand-built
  COMPSs runtime via `COMPSS_HOME` automatically.
- `--engine adios`: you MAY add `adios2`, but it's optional -- the workflow has a
  numpy-I/O fallback (see `systems/adios` skill), so don't block the run if it's
  missing.

**LAMMPS is NOT in stack_decision.** It is source-built and pre-installed — do NOT list it as a pip package.

**Do NOT add:** lammps, scipy, ase, mdanalysis, h5py, or any other package not listed above.
Add `mpi4py` only if the environment knowledge confirms MPI is available.

---

## Task Templates — Describe the Work, Not the Dispatch

Tasks describe **what work must happen**, in order, with enough specificity that the
explorer can write exact code. The explorer combines every task into **one** generated
workflow file and the engine runs it, so never name a tool, a decorator, a `main()`,
a bash launcher, or a PBS script in a task.

---

## Few-Shot Examples — Target Level of Detail

### Too vague (BAD)
> "Run LAMMPS and analyze the output."

### Prescribing the mechanism (BAD — do not write tasks like this)
> "Write a @python_app/@task that copies files and runs the simulation."
> "Write a main() function with argparse."
> "Write a run_workflow.sh launcher."

### Describing the work (GOOD — states what runs and the critical constraints)
> "Run the LAMMPS simulation from /app/work/run0/ using in.watbox, unmodified. Inside a PBS allocation with an MPI launcher, run the cluster module's `lmp` under mpirun: `module load lammps/22Jul2025`, in a login shell so `module` resolves, with PMI_SIZE, PMI_RANK and I_MPI_HYDRA_BOOTSTRAP unset before mpirun. Without a launcher, run it single-process through the Python API: os.chdir('/app/work/run0') MUST come before creating the lammps instance because dump paths in in.watbox are relative to CWD, then `from lammps import lammps; lmp = lammps(cmdargs=['-screen','none']); lmp.file('/app/work/run0/in.watbox'); lmp.close()`. Clear frames/*.lammpstrj first. Exit 11 is a SIGSEGV on cleanup — if trajectory frames exist, it is a success."

> "Run the OVITO analysis: load all frames with ovito.io.import_file, apply IdentifyDiamondModifier. Count cubic ice as structure types 1+2+3 combined and hexagonal ice as types 4+5+6 combined — not 1-2 and 3-4, which misclassifies type 3. Write results.csv with columns: frame, timestep, cubic_diamond_count, hexagonal_diamond_count."

---

## Task List Template

```
1. "Check that required packages are installed: ovito, numpy, matplotlib, pillow."

2. "Create the run directory structure: /app/work/run0/frames and
   /app/work/run0/renders."

3. "Copy AW.tersoff, data.init, and in.watbox from /app/data/ into /app/work/run0/.
   Always re-copy in.watbox fresh — the user may have edited it — and never modify it."

4. "Delete any stale frames/*.lammpstrj in /app/work/run0/ before the simulation so
   old output cannot be mistaken for this run's."

5. "Run the LAMMPS simulation on in.watbox with /app/work/run0 as the working
   directory. Inside a PBS allocation with an MPI launcher, use the cluster module
   (module load lammps/22Jul2025, gcc 13.2.0 + OpenMPI 5.0.6) in a login shell so
   `module` is defined, and unset PMI_SIZE, PMI_RANK and I_MPI_HYDRA_BOOTSTRAP before
   mpirun — the pip lammps wheel's bundled lmp binary does not load on this cluster's
   kernel. Without a launcher, run single-process via the Python API, calling
   os.chdir('/app/work/run0') BEFORE creating the lammps instance because dump paths
   in in.watbox are relative to CWD. Exit 11 is a SIGSEGV on cleanup: if trajectory
   frames were written, treat it as success. Exit 143 is an MPI init SIGTERM and is a
   real failure."

6. "Verify simulation output: confirm .lammpstrj trajectory files exist in
   /app/work/run0/frames/ before proceeding to analysis."

7. "Run OVITO diamond structure analysis: load all frames from
   /app/work/run0/frames/ using ovito.io.import_file, apply IdentifyDiamondModifier.
   Count cubic ice as structure types 1+2+3 combined, hexagonal ice as types 4+5+6
   combined. Write /app/work/run0/results.csv with columns: frame, timestep,
   cubic_diamond_count, hexagonal_diamond_count."

8. "Render each trajectory frame as a 3D matplotlib scatter plot.
   Color: liquid/unstructured (type 0) cyan #00BFFF, cubic diamond (types 1-3)
   blue #0000FF, hexagonal diamond (types 4-6) red #FF2200. Minimum atom size s=25,
   alpha >= 0.6. Use matplotlib.use('Agg') for headless rendering. Save each frame
   as frame_NNNN.png in /app/work/run0/renders/."

9. "Assemble the rendered PNGs into an animated GIF using pillow: load sorted
   frame_*.png files from /app/work/run0/renders/, save as
   /app/work/run0/renders/animation.gif with loop=0 and duration=100ms per frame."

10. "Generate a nucleation timeseries plot from the per-frame counts: plot cubic ice
   count (blue #0000FF) and hexagonal ice count (red #FF2200) vs timestep, add a
   dashed black total-ice line. Save as
   /app/work/run0/renders/nucleation_timeseries.png."
```

If `--engine adios` was selected, add a task requiring the per-frame counts to move
between the analysis step and the timeseries step through `adios2.Stream` — written
with `stream.write(...)` and genuinely read back with `stream.read(...)` — rather than
being passed in memory.

---

## Key Rules

- **All paths in tasks MUST use `/app/`** — never use `/lcrc/project/`, `/gpfs/`, `/scratch/`, or any cluster-specific path. `/app/` is always resolved correctly by the server regardless of environment.
- Do NOT add tasks for "install LAMMPS" or "set up the venv" — the environment is pre-built
- Do NOT write tasks that name a specific tool, decorator, `main()`, bash launcher, or PBS script — describe the work; the explorer expresses it in one generated workflow file
- Do NOT write tasks that have the explorer call the CLI, `subprocess`, `mpirun`, or `qsub` itself
- If the paper uses a parameter not in the current in.watbox, note it in literature_findings but do NOT hardcode it — in.watbox controls the simulation and must be used as-is
- The input script (in.watbox) is user-controlled; the explorer must never modify it

---

## Example Output

This example assumes `--engine parsl` was selected -- swap the engine-specific
package per the Stack Decision rules above if a different engine was chosen
(omit it for `pycompss`, use `adios2` optionally for `adios`).

```json
{
  "literature_findings": [
    "Water crystallization simulation using LAMMPS with AW Tersoff potential",
    "NPT ensemble at 180 K, 1.0 atm",
    "Timestep: 0.01 ps, run length: 9000 steps",
    "Ice structure detection via OVITO IdentifyDiamondModifier",
    "Cubic diamond (types 1-3) and hexagonal diamond (types 4-6) tracked per frame"
  ],
  "stack_decision": ["ovito", "parsl>=2024.0.0", "numpy", "matplotlib", "pillow"],
  "tasks": [
    "Check that ovito, numpy, matplotlib, pillow are installed.",
    "Create /app/work/run0/frames/ and /app/work/run0/renders/.",
    "Copy AW.tersoff, data.init, in.watbox from /app/data/ to /app/work/run0/ — always re-copy in.watbox fresh, never modify it.",
    "Delete stale frames/*.lammpstrj in /app/work/run0/ before the run.",
    "Run LAMMPS on in.watbox with /app/work/run0 as working directory: under PBS with a launcher use the cluster module lammps/22Jul2025 in a login shell with PMI_SIZE/PMI_RANK/I_MPI_HYDRA_BOOTSTRAP unset before mpirun; otherwise use the Python API single-process with os.chdir('/app/work/run0') before creating the lammps instance. Exit 11 with frames written is success; exit 143 is MPI init failure.",
    "Verify .lammpstrj frames exist in /app/work/run0/frames/ before proceeding.",
    "Run OVITO analysis: IdentifyDiamondModifier, cubic=types 1+2+3, hexagonal=types 4+5+6, write results.csv with frame, timestep, cubic_diamond_count, hexagonal_diamond_count.",
    "Render frames: matplotlib Agg backend, type 0 cyan #00BFFF, cubic blue #0000FF, hexagonal red #FF2200, s=25 min, alpha >= 0.6, save frame_NNNN.png to renders/.",
    "Assemble the GIF with pillow from sorted frame_*.png -> animation.gif, loop=0, duration=100ms.",
    "Plot the nucleation timeseries from the per-frame counts -> nucleation_timeseries.png."
  ]
}
```
