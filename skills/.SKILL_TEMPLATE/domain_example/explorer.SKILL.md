---
name: uses_cases/<your domain name>/explorer
description: >
  (your domain name) execution facts for the explorer. Describe the tools used (Ex: HACC), whether PBS is used or not (mention qsub / submit_mpi_task), and a concise description of the overall task in a sentence.
---
<!-- Remove all comments in your copy. -->
<!-- Note: don't use bullet points in section headers, but tables and step bodies may use them (see examples). -->
# (your domain name) [Optional: a tool name] — Explorer Skill
**Purpose**: This is where you mention what (your domain name) workflow the explorer executes.

**Template**:
> Domain-specific guidance for the explorer agent when executing the [Optional: a tool name] (your domain name) workflow ([one-line description of producer → analysis → visualization]).

**For example**:
> Domain-specific guidance for the explorer agent when executing the HACC cosmological N-body workflow (producer simulation -> FOF/SOD halo analysis -> dark-matter density slice visualization), based on "The Last Journey" sample run.

---

## When to Use This Skill
**Purpose**: Mention when to load this file.

**Template**:
> Load this whenever the explorer is executing a (your domain name) workflow (goal mentions \<keyword>).
```
<keyword> may be any of the following:
- a specific <tool>
- a specific library or word related to your <domain_name>
- a specific <path> mentioned
```
**For example**:
> Load this whenever the explorer is executing a workflow that runs/reads a HACC run (paths under `/lcrc/project/PEDAL/jalemu/HACC/`, GenericIO snapshots, FOF/SOD halo catalogs).

---


<!-- This section is relatively the same across all use_cases. Almost no need to modify -->
## Flow for This Project

```
planner → installer → explorer → end
```

Same shape as other use cases. Route to installer after planner completes.

---

## [Optional: The Producer Is a Private Black Box]
**Purpose**: This section is for a rule when the producer/executable must be treated as opaque — the agent should run the existing binary and read its output, never reimplement, rebuild, or modify it. Usually written after seeing the agent try to regenerate physics or rebuild an already-built binary.

**Template**:

Content varies. I reccomend going to the skill files in the examples.

**For Example**:
> File: `use_cases\cosmology\explorer`
>
> Section: "The Simulation Is a Private Black Box"

    "The HACC executable (hacc_tpm) is closed-source. Never try to read, regenerate, or reimplement its physics. Treat it purely as: run the existing binary, wait for it to finish, read its output."
> File: `use_cases\eddy_uv\explorer`
>
> Section: quirks note under "Stage 1"

    "nek5000 in this directory is already built (build.log exists) -- never try to rebuild it."

---

## Stage-by-Stage Execution
**Purpose**: The core of the explorer skill. Break the run into ordered stages (explore the case, write the analysis/render script, run the producer, report/recover). Name the exact tool for each stage (`submit_shell_task`, `submit_mpi_task`, `run_lammps`, `qsub`), the exact paths, and the expected output.

**Template**:
Some of these stages are optional depending on what your workflow needs. Number them and give each a concrete command block.

> ## Stage 1: Explore the Case (config only, not old output)
> [list/explore the case dir just to confirm structure; read this run's own config files; never hardcode values without confirming them]
>
> ## Stage 2: Write the Analysis/Rendering Script
> [write the script directly from the documented format and this run's config — not by testing against pre-existing output; use real absolute paths if it runs standalone]
>
> ## Stage 3: Run/Submit the Producer
> [name the exact tool: qsub for one use case, submit_mpi_task inside the existing allocation for another, run_lammps for another; give the command block and the default NRANKS/WALLTIME]
>
> ## Stage 4: Report, or Recover Without Re-Running the Producer
> [read back the deliverables to confirm they exist; if only the analysis stage failed, re-run it directly against this run's own fresh output — do not requeue the expensive producer for an analysis bug]

**For example**:
> File: `use_cases\cosmology\explorer`
>
> Sections: "Stage 1", "Stage 2", "Stage 3: Build and Submit One PBS Job (producer + analysis, single qsub)", "Stage 4: Report, or Recover Without Re-Running the Producer"

    Stage 3 embeds hacc_tpm and analyze_and_render.py in one agent_subme.pbs, submits it with `cd .../SampleRun_go && qsub agent_subme.pbs`, captures the job ID, then polls `qstat <job_id>` on a fixed 60-second interval until state C.
> File: `use_cases\eddy_uv\explorer`
>
> Section: "Stage 1: Run the Producer (submit_mpi_task, inside the existing allocation)"

    "No new PBS job here, and no qsub. The producer runs via submit_mpi_task inside the same allocation... default to 8 the value confirmed working for this producer."

---

## [Optional: Reading the Producer's Output]
**Purpose**: If reading the producer's output format is non-obvious (a special binary, a CLI-only reader, a structure-type mapping), document the exact reader/library and any gotchas so the agent doesn't hand-parse or guess.

**Template**:

Content varies. I reccomend going to the skill files in the examples.

**For Example**:
> File: `use_cases\cosmology\explorer`
>
> Section: "Reading Snapshot & Halo Data — Do NOT use pygio"

    "pygio ... is not built ... Instead use the pre-built CLI binaries directly via subprocess: GenericIOPrint ..."
> File: `use_cases\molecular_nucleation\explorer`
>
> Section: "CRITICAL -- IdentifyDiamondModifier structure type mapping"

    "Cubic = types 1 + 2 + 3 (NOT just type 1); Hexagonal = types 4 + 5 + 6 (NOT just type 4). Counting only primary types gives ~10% of the actual crystal count."

---

## [Optional: ADIOS2 Engine Notes (when `--engine adios`)]
**Purpose**: If your workflow supports the ADIOS engine, state which inter-stage numerical arrays must round-trip through `adios2.Stream` (write then read back) and which files stay plain (final human-facing images, small metadata). See `systems/adios` skill for the general API.

**Template**:

Content varies. I reccomend going to the skill files in the examples.

**For Example**:
> File: `use_cases\molecular_nucleation\explorer`
>
> Section: "ADIOS2 Engine Notes (when `--engine adios`)"

    "Stage 2 (OVITO analysis): write the per-frame counts with write_bp... Stage 3c: read those counts back with read_bp... Stage 3a and 3b do NOT need ADIOS2 (final human-facing visual artifacts)."

---

<!-- This section uses a two-column table -->
## Common Pitfalls
**Purpose**: A two-column table listing each pitfall the agent hits during a real run and how to solve/avoid it. Populate this after executing the workflow and finding what goes wrong.

**Template**:

>| Pitfall | Solution |
>|---|---|
>| \<the mistake the agent tends to make> | \<what to do instead> |
>| \<a wrong assumption about a file/format/rank count> | \<how to confirm the real value> |

**Example**:
>| Pitfall | Solution |
>|---|---|
>| `pygio` import fails (`No module named 'pygio._version'`) | Expected — it's not built. Use `GenericIOPrint` via `subprocess` instead; do not try to build/install pygio. |
>| Assuming 4-digit field indices (`f0001`) | This case's files are 5-digit (`f00001`) -- check `eddy_uv.nek5000`'s `filetemplate`. |

---

<!-- This section can have bullet points -->
## Output Files (in the work dir)
**Purpose**: List the artifacts the explorer is expected to produce in the work dir, so a run can be checked for completeness. Note which files stay plain (final images) regardless of engine mode.

**Template**:
>- \<intermediate array file(s)> — \<what they hold> (`.npz`/`.csv`, or `.bp` under `--engine adios`)
>- \<final rendered image(s)> — the visualization deliverable (always a plain file, never `.bp`)
>- `summary.txt` — human-readable reproduction summary

**Example**:
>- `density_slice.npy` / `.bp` + metadata — the projected density grid
>- `dm_density_slice.png` — final rendered image (always a plain file, never `.bp`)
>- `summary.txt` — human-readable reproduction summary (config values used, results)
