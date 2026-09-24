---
name: uses_cases/<your domain name>/planner
description: >
  (your domain name) extraction rules for the planner. Covers what parameters to extract from the paper, the correct stack_decision for this project, any qsub / no-qsub exception, and MCP-style task templates for the pipeline.
---
<!-- Remove all comments in your copy. -->
<!-- Note: don't use bullet points in section headers; extraction lists, tables, and task blocks may use them (see examples). -->
# (your domain name) [Optional: a tool name] — Planner Skill
**Purpose**: One sentence stating that this holds the extraction and task-writing rules specific to this workflow.

**Template**:
> Extraction and task-writing rules specific to the [Optional: a tool name] (your domain name) workflow.

**For example**:
> Extraction and task-writing rules specific to the HACC cosmological N-body workflow.

---

## When to Use This Skill
**Purpose**: Mention when to load this file when planning.

**Template**:
> Load when planning a (your domain name) workflow (paper or goal mentions \<keyword>).
```
<keyword> may be any of the following:
- a specific <tool>
- a specific library or word related to your <domain_name>
- a specific <path> mentioned
```
**For example**:
> Load when planning a HACC / cosmological N-body workflow (paper mentions HACC, Mira, Last Journey, FOF/SOD halo finding, or the goal references `/lcrc/project/PEDAL/jalemu/HACC/`).

---

## What to Extract from the Paper
**Purpose**: List the exact parameter names, tools, and conventions the planner should pull out of the paper. Be specific — name the parameters, not "the simulation settings".

**Template**:
>- \<parameter #1 and its units/example> (e.g. temperature, box size, timestep, run length)
>- \<parameter #2> ...
>- \<the software tools to identify> (MD engine, analysis tool, orchestration engine)
>- \<conventions to record> (e.g. halo-center convention, ensemble, force field name)
>
> Treat the paper as descriptive ground truth for *what the pipeline should compute*, but always confirm actual numeric config values against this run's real config files rather than hardcoding paper values that may not match this sample run.

**Example**:
> File: `use_cases\molecular_nucleation\planner`
>
> Section: "What to Extract from the Paper"

    "Temperature (K), Timestep (ps), Run length (steps), Ensemble (NPT/NVT/NVE), Pressure, Thermostat/barostat coupling, Force field name, System size, Seed. Software: LAMMPS, OVITO with IdentifyDiamondModifier, orchestration engine."
> File: `use_cases\cosmology\planner`
>
> Section: "What to Extract from the Paper"

    "Box size RL and grid resolution NG (confirm against params/indat.params rather than assuming the paper's full-scale values), Omega_m/Omega_cdm/Omega_b/h/sigma_8/n_s, FOF linking length b, SOD Delta, halo center convention, target snapshot step."

---

## Stack Decision
**Purpose**: The exact pip-installable packages for `stack_decision`, plus engine-specific handling and a "do not add" list. Mirrors the installer's Package Requirements — keep them consistent.

**Template**:
> ```
> <standard packages, e.g. numpy, matplotlib>
> <the one non-obvious package>   # what it does; do not hand-parse
> mpi4py        (only if environment knowledge confirms MPI is available)
> ```
>
> Engine-specific package (depends on `--engine`):
>- `parsl`: add `parsl>=2024.0.0` — normal pip package, hard requirement.
>- `pycompss`: do NOT add `pycompss` — detected via `COMPSS_HOME` (see `systems/pycompss` skill).
>- `adios`: `adios2` — optional (numpy-I/O fallback) OR required if a task depends on `adios2.Stream`; state which for your workflow.
>
> **Do NOT add:** \<packages that belong to other use cases; note which are not pip-installable at all, e.g. pre-built binaries>.

**Example**:
> File: `use_cases\eddy_uv\planner`
>
> Section: "Stack Decision"

    "numpy, matplotlib, pymech (reads Nek5000 .f##### field files -- pip installable, do not hand-parse). Don't pre-load with packages from other use cases (scipy, mpi4py, ovito, pillow, lammps, adios2) unless the task genuinely calls for them."
> File: `use_cases\molecular_nucleation\planner`
>
> Section: "Stack Decision"

    "ovito, numpy, matplotlib, pillow. LAMMPS is NOT in stack_decision (source-built/pre-installed). Do NOT add: lammps, scipy, ase, mdanalysis, h5py."

---

## [Optional: qsub / No-qsub Exception]
**Purpose**: State explicitly how the producer is launched, since this differs per use case. Either the general LCRC rule (run inside the existing allocation via submit_mpi_task, never qsub) OR the deliberate exception (this use case may qsub a new PBS job because the sim only runs through its own batch script). Write it into the relevant task, don't rely on inference.

**Template**:

Content varies. I reccomend going to the skill files in the examples.

**For Example**:
> File: `use_cases\eddy_uv\planner`
>
> Section: "No `qsub` Exception — Producer Runs in the Existing Allocation"

    "never submit a new PBS job -- run inside the existing interactive allocation instead. The Nek5000 producer runs via submit_mpi_task... producer and consumer are always one job."
> File: `use_cases\cosmology\planner`
>
> Section: "Architectural Exception: This Use Case May `qsub`"

    "Unlike every other use case, a task here is allowed to instruct the explorer to submit a real, new PBS batch job via qsub... because the simulation only runs through its own batch script. Critically, this must be one job that does both stages."

---

## Task List Template
**Purpose**: The ordered, copy-ready list of tasks the explorer will execute via MCP tool calls. Each task maps to one or a few `submit_task` / `submit_shell_task` / tool calls. State the critical constraints inline (paths, rank counts, which tool to use, what not to modify).

**Template**:
> ```
> 1. "Call get_resources FIRST. Confirm in_pbs... STOP if not. This run requires <N> ranks."
> 2. "Explore <case dir> before assuming anything; read this run's own config files; do not read old output content."
> 3. "Write <analysis/render script> directly from the documented format and this run's config."
> 3a. "IF `--engine adios` WAS SELECTED (its own required/optional task): use adios2.Stream for the inter-stage arrays."
> 4. "Run/submit the producer via <exact tool: qsub / submit_mpi_task / run_lammps> with <NRANKS/WALLTIME>."
> 5. "Poll / verify output exists."
> 6. "Read back <deliverables> to confirm; on analysis-only failure, re-run analysis against this run's own fresh output, not a full producer resubmit."
> ```

**Example**:
> File: `use_cases\cosmology\planner`
>
> Section: "Task List Template" (6 numbered tasks + 3a/5a adios sub-tasks)

    "1. Call get_resources FIRST... 2. Explore SampleRun_go/... 3. Write analyze_and_render.py directly... 4. Build a PBS batch script yourself... submit `qsub agent_subme.pbs`... 5. Poll `qstat <job_id>`... 6. Read back dm_density_slice.png and summary.txt."
> File: `use_cases\molecular_nucleation\planner`
>
> Sections: "Task Templates — MCP Execution Style", "Few-Shot Examples — Target Level of Detail", "Task List Template" (9 numbered tasks)

    "Tasks describe what the explorer executes via MCP tool calls... There is no workflow.py, no @python_app/@task wrapping, no main(), no bash launcher." (Includes Too vague/Artifact/MCP good-vs-bad few-shots.)

---

## Key Rules
**Purpose**: The hard constraints the planner must always honor when writing tasks: which paths to use, what the explorer must never modify, which tool is mandatory for a given step, and that the task list must end with the required deliverable.

**Template**:
>- \<path convention: `/app/` vs real LCRC absolute paths — state which and why>.
>- Never write a task that asks the explorer to modify \<the config/input files that must stay untouched>.
>- \<the mandatory tool for a key step, e.g. run_lammps for LAMMPS — not submit_task>.
>- The task list must always end with \<the required deliverable, e.g. a rendering/visualization task that produces PNG output>.

**Example**:
> File: `use_cases\molecular_nucleation\planner`
>
> Section: "Key Rules"

    "LAMMPS task MUST use run_lammps... All paths in tasks MUST use /app/... Do NOT write tasks that say 'write a @python_app/@task', 'write a main()', or 'write a bash launcher'... the explorer must never modify in.watbox."
> File: `use_cases\eddy_uv\planner`
>
> Section: "Key Rules"

    "Source data paths are real LCRC paths... reference by actual absolute path, not /app/data/. Never write a task asking the explorer to modify eddy_uv.rea/.usr/.map/SIZE/SESSION.NAME/subeddy.pbs. The task list must always end with a rendering/visualization task that produces PNG image output."

---

## [Optional: Example Output]
**Purpose**: A full example of the planner's JSON output (`literature_findings`, `stack_decision`, `tasks`) so the shape is unambiguous. Note the engine assumption and how it changes per `--engine`.

**Template**:
> ```json
> {
>   "literature_findings": ["<key facts extracted from the paper>"],
>   "stack_decision": ["<the packages from Stack Decision above>"],
>   "tasks": ["<the condensed task list, one string per task>"]
> }
> ```

**Example**:
> File: `use_cases\molecular_nucleation\planner`
>
> Section: "Example Output"

    "{ literature_findings: [...], stack_decision: ['ovito', 'parsl>=2024.0.0', 'numpy', 'matplotlib', 'pillow'], tasks: [...] }" — prefaced with "This example assumes --engine parsl was selected -- swap the engine-specific package per the Stack Decision rules if a different engine was chosen."
