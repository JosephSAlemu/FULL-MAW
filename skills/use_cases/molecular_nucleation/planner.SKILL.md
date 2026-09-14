---
name: use_cases/molecular_nucleation/planner
description: >
  Molecular nucleation extraction rules for the planner. Covers what parameters to extract
  from LAMMPS water crystallization papers, the correct stack_decision for this project,
  and MCP-style execution tasks for the explorer to run step by step.
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

## Task Templates — MCP Execution Style

Tasks describe what the **explorer executes via MCP tool calls**, step by step.
There is no workflow.py, no @python_app/@task wrapping, no main(), no bash launcher.
Each task maps to one or a few `submit_task` or `submit_shell_task` calls.

---

## Few-Shot Examples — Target Level of Detail

### Too vague (BAD)
> "Run LAMMPS and analyze the output."

### Artifact approach (BAD — do not write tasks like this)
> "Write a @python_app/@task run_lammps that copies files and runs the simulation."
> "Write a main() function with argparse."
> "Write a run_workflow.sh launcher."

### MCP approach (GOOD — states what the explorer executes and the critical constraints)
> "Run LAMMPS via submit_task: os.chdir('/app/work/run0') MUST come before creating the lammps instance because dump paths in in.watbox are relative to CWD. Use Python API: from lammps import lammps; lmp = lammps(cmdargs=['-screen','none']); lmp.file('/app/work/run0/in.watbox'); lmp.close(). Do not modify in.watbox."

> "Run OVITO analysis via submit_task: load all frames with ovito.io.import_file, apply IdentifyDiamondModifier. Count cubic ice as structure types 1+2+3 combined and hexagonal ice as types 4+5+6 combined — not 1-2 and 3-4, which misclassifies type 3. Write results.csv with columns: frame, timestep, cubic_diamond_count, hexagonal_diamond_count."

---

## Task List Template

```
1. "Check that required packages are installed: check_package for ovito, numpy,
   matplotlib, pillow."

2. "Create the run directory structure using submit_shell_task:
   mkdir -p /app/work/run0/frames /app/work/run0/renders"

3. "Copy all required data files into /app/work/run0/ using submit_shell_task:
   AW.tersoff, data.init, and in.watbox from /app/data/. Always re-copy in.watbox
   fresh — the user may have edited it."

4. "Run the LAMMPS simulation via the run_lammps tool:
   run_lammps(script='in.watbox', work_dir='/app/work/run0').
   The server automatically selects mpirun+binary on HPC or Python API locally.
   Never modify in.watbox."

5. "Verify simulation output via list_files on /app/work/run0/frames/ — confirm
   .lammpstrj trajectory files exist before proceeding to analysis."

6. "Run OVITO diamond structure analysis via submit_task: load all frames from
   /app/work/run0/frames/ using ovito.io.import_file, apply IdentifyDiamondModifier.
   Count cubic ice as structure types 1+2+3 combined, hexagonal ice as types 4+5+6
   combined. Write /app/work/run0/results.csv with columns: frame, timestep,
   cubic_diamond_count, hexagonal_diamond_count."

7. "Render each trajectory frame as a 3D matplotlib scatter plot via submit_task.
   Color: liquid/unstructured (type 0) cyan #00BFFF, cubic diamond (types 1-3)
   blue #0000FF, hexagonal diamond (types 4-6) red #FF2200. Minimum atom size s=25,
   alpha >= 0.6. Use matplotlib.use('Agg') for headless rendering. Save each frame
   as frame_NNNN.png in /app/work/run0/renders/."

8. "Assemble the rendered PNGs into an animated GIF via submit_task using pillow:
   load sorted frame_*.png files from /app/work/run0/renders/, save as
   /app/work/run0/renders/animation.gif with loop=0 and duration=100ms per frame."

9. "Generate a nucleation timeseries plot via submit_task: read /app/work/run0/results.csv,
   plot cubic ice count (blue #0000FF) and hexagonal ice count (red #FF2200) vs timestep,
   add a dashed black total-ice line. Save as /app/work/run0/renders/nucleation_timeseries.png."
```

---

## Key Rules

- **LAMMPS task MUST use `run_lammps`** — do NOT use `submit_task`, `submit_mpi_task`, or any other tool for running LAMMPS. `run_lammps` handles HPC vs local automatically.
- **All paths in tasks MUST use `/app/`** — never use `/lcrc/project/`, `/gpfs/`, `/scratch/`, or any cluster-specific path. `/app/` is always resolved correctly by the server regardless of environment.
- Do NOT add tasks for "install LAMMPS" or "set up the venv" — the environment is pre-built
- Do NOT write tasks that say "write a @python_app/@task", "write a main()", or "write a bash launcher"
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
    "Check that ovito, numpy, matplotlib, pillow are installed via check_package.",
    "Create /app/work/run0/frames/ and /app/work/run0/renders/ via submit_shell_task.",
    "Copy AW.tersoff, data.init, in.watbox from /app/data/ to /app/work/run0/ via submit_shell_task — always re-copy in.watbox.",
    "Run LAMMPS via run_lammps(script='in.watbox', work_dir='/app/work/run0'). Server picks mpirun+binary or Python API automatically.",
    "Verify frames exist in /app/work/run0/frames/ via list_files before proceeding.",
    "Run OVITO analysis via submit_task: IdentifyDiamondModifier, cubic=types 1+2+3, hexagonal=types 4+5+6, write results.csv.",
    "Render frames via submit_task: matplotlib Agg backend, color by structure type, s=25 min, save PNGs to renders/.",
    "Assemble GIF via submit_task: pillow sorted PNGs -> animation.gif.",
    "Plot timeseries via submit_task: results.csv -> nucleation_timeseries.png."
  ]
}
```
