---
name: uses_cases/<your domain name)/orchestrator
description: >
  (your domain name) routing facts for the orchestrator. Explain the tools and tasks it uses (Ex: HACC, MPI, PBSPro) in a concise sentence.
---
<!-- Remove all comments in your copy. -->
<!-- Note: don't use bullet points in actual file. -->
# (your domain name) [Optional: a tool name] — Orchestrator Skill
**Purpose**: This is where you mention what (u) workflow the orchestrator routes rules for.

**Template**: 
> Orchestrator routes rules specific to the [Optional: a tool name] (your domain name) workflow

**For example**: 
> Orchestrator routes rules specific to the HACC cosmological N-body workflow.

---

## When to Use This Skill
**Purpose**: Mention when to load this file

**Template**:
 > Load when orchestrating a (your domain name) workflow (goal mentions \<keyword>)
```
<keyword> may be any of the following:
- a specific <tool>
- a specific library or word related to your <domain_name>
- a specific <path> mentioned 
```
**For example**: 
 > Load when orchestrating a HACC / cosmology workflow (goal mentions HACC, GenericIO, FOF/SOD halo catalogs, "Last Journey", or paths under `/lcrc/project/PEDAL/jalemu/HACC/`).

---


<!-- This section is relatively the same across all use_cases. Almost no need to modify -->
## Flow for This Project

```
planner → installer → explorer → end
```

Same shape as other use cases. Route to installer after planner completes.

---

## [Optional: How to handle (insert agent issue).]
**Purpose**: This section is generally for a rule in case an agent misbehaves. Usually written after executing the workflow and finding steps the agent does that you don't want.

**Template**: 

Content varies. I reccomend going to the skill files in the examples.

**For Example**:

> File: `use_cases\eddy_uv\orchestrator`
>
> Section: "No qsub Exception — Producer Runs in the Existing Allocation" 
    
    "Agent should never submit a new pbs job and should run inside the existing pbs allocation"
> File: `use_cases\cosmology\orchestrator`
>
> Section: "Architectural Exception: Real qsub Is Allowed Here"
    
    "The general LCRC rule is "the agent never submits a new PBS job — run inside the existing interactive allocation instead." This use case is an explicit, deliberate exception"

---

## Requirements Approval
**Purpose**: Verify the specific python packages, external tools (mpi), and/or that a job was run. Varies depending on what you specifically need for your workflow.

**Template**:
Some of these options are optional depending on what your workflow needs.

> When the installer presents requirements.txt for approval, verify it contains:
>- \<Python_package#1>
>- \<Python_package#2>
>- ...
>- \<Python_package#n>

> The engine-specific package matching `--engine`:
>- `parsl`: `parsl` should/should not be present
>- `pycompss`: `pycompss` should/should not be present (nevepip-installed — see `systems/pycompss` skill)
>- `adios`: `adios2` should/should not be present (optional, hanumpy fallback)

> Ensure that (put any tools in these parantehesis like mpi, or PBS pro task) is loaded.
    
>**Do NOT approve**:
>- Insert specific package/tools you don't want installed.

**Example**:
>When the installer presents requirements.txt for approval, verify it contains:
>- `ovito`
>- `numpy`
>- `matplotlib`
>- `pillow`

>The engine-specific package matching `--engine`:
>- `parsl`: `parsl>=2024.0.0` should be present
>- `pycompss`: `pycompss` should NOT be present (never pip-installed — see `systems/pycompss` skill)
>- `adios`: `adios2` may or may not appear — approve either way (optional, has numpy fallback)

>**Do NOT approve**:
>- requirements.txt that includes `lammps` as a pip package — LAMMPS must be source-built (serial, `BUILD_MPI=off`). If `lammps` appears in the list, reject with feedback to remove it.

---

## Error Pattern Recognition

**Purpose**: Three column section which explain the error the agent may encounter, the agent to route to, and the feedback

**Template**:

>| Error pattern | Route to | Feedback |
>|---|---|---|
>| error related to missing package installation/missing tool | installer | "Thing you want the orchestrator to tell the explorer to do to fix it"
>| explorer does task you don't want | explorer | "Thing you want the orchestrator to tell the explorer to avoid"

**Example**:
>| Error pattern | Route to | Feedback |
>|---|---|---|
>| `ModuleNotFoundError: No module named 'pygio._version'` | explorer | "pygio is not built and should not be built. Use `GenericIOPrint` via subprocess instead." 
>| `qsub` / `qstat` command not found or job ID not captured | explorer | "Re-run via submit_shell_task with `cd /lcrc/project/PEDAL/jalemu/HACC/SampleRun_go && qsub agent_subme.pbs` (the script you built) — qsub must run with that directory as cwd so $PBS_O_WORKDIR resolves." 

---

<!-- This section can have bullet points -->
## Notes
**Purpose**: Section to at the end of the workflow to ensure  the end of the AI agent workflow. End of the AI agent workflow where you put what to do after the run ends.

**Template**:
>- After a sucessful run (put examples of a successful run for the workflow), route to end
>- Never route back to planner unless the task list itself is structurally wrong (put examples of a failure or missed step here). If a step is wrong it should (input what orchestrator should do next here)

**Example**:
>- After a successful run (PBS job completed, density slice image produced, summary written), route to "end"
>- Never route back to planner unless the task list itself is structurally wrong (e.g. missing a required stage) — config-value mistakes should go back to explorer with feedback to re-read `params/indat.params`/`cosmotools-config.dat`, not to planner
