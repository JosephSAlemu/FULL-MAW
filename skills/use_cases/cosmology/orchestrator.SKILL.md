---
name: use_cases/cosmology/orchestrator
description: >
  Cosmology (HACC) routing rules for the orchestrator. Covers the one-workflow rule
  (producer AND analysis/visualization handed to the workflow engine together, no
  qsub), requirements approval for this project, and HACC-specific error pattern
  recognition.
---

# Cosmology (HACC) — Orchestrator Skill

Routing rules specific to the HACC cosmological N-body workflow (Last Journey sample run).

---

## When to Use This Skill

Load when orchestrating a HACC / cosmology workflow (goal mentions HACC, GenericIO,
FOF/SOD halo catalogs, "Last Journey", or paths under
`/lcrc/project/PEDAL/jalemu/HACC/`).

---

## Flow for This Project

```
planner → installer → explorer → end
```

Same shape as other use cases. Route to installer after planner completes.

---

## No `qsub` Exception — The Engine Runs the Producer

There used to be an explicit exception here letting this use case submit a real new
PBS batch job. **That is gone.** This project now follows the same general LCRC rule
as every other one: the agent never submits a new PBS job and never writes a
`.pbs`/`.sh` launcher — the workflow engine runs the producer inside the existing
allocation. If the explorer calls `qsub`/`qstat` or generates a batch script, that is
a violation to flag and route back.

Three things to verify:
1. **Producer and analysis are one workflow** — the simulation and the
   analysis/rendering must be handed to the engine together, with the analysis
   ordered after the producer as a dependency. Two unrelated executions, or polling
   a job queue, is a violation.
2. **No direct execution** — the explorer must not invoke the CLI, use `subprocess`,
   or run `mpirun` itself. `hacc_tpm` and `GenericIOPrint` are command-line steps the
   engine runs.
3. **The analysis was written from documented format rules, not developed against
   pre-existing sample output** — `output/full_snapshots/` and
   `analysis/haloproperties/` may already contain content from some prior run; the
   explorer must not read, parse, or render from that content when writing or
   testing the analysis. That's leftover data from another run, not this run's own
   result.

---

## Requirements Approval

When the installer presents `requirements.txt`, verify it contains the basics:
- `numpy`
- `matplotlib`
- `mpi4py` (only if environment knowledge confirms MPI is available)

`adios2` may or may not appear depending on whether the planner decided to request it
for this engine — approve either way; if it's missing and `--engine adios` was
requested, that's fine, the workflow has a documented numpy-I/O fallback (see
`systems/adios` skill) rather than a hard requirement.

**Do NOT approve** a requirements.txt that includes `ovito`, `pillow`, or `lammps` —
those belong to the `molecular_nucleation` use case, not this one; their presence
usually means a stale `requirements.txt` from a previous run leaked through.

**Do NOT approve** anything that tries to pip-install HACC itself, `pygio`, or
GenericIO bindings — those are pre-built cluster binaries/private code, never
pip-installable, and attempting to build them is a real installer risk (compiling a
C++ extension against GenericIO libs).

---

## Error Pattern Recognition

| Error pattern | Route to | Feedback |
|---|---|---|
| `ModuleNotFoundError: No module named 'pygio._version'` | explorer | "pygio is not built and should not be built. Dump the file with the `GenericIOPrint` CLI tool as its own engine-run command step, redirecting stdout to a text file, then parse that file in the Python step." |
| Explorer calls `qsub`/`qstat`, or writes a `.pbs`/`.sh` script | explorer | "Do not submit a PBS job or write a launcher script — you are already inside an allocation. Have the workflow engine run hacc_tpm on 8 ranks with the env file sourced in the same command and SampleRun_go/ as the working directory." |
| Explorer calls `subprocess`, or invokes `hacc_tpm`/`GenericIOPrint`/`mpirun` directly | explorer | "You never execute commands yourself. Every CLI invocation must be handed to the workflow engine as a command string." |
| Explorer reads/parses/renders from `output/full_snapshots/`/`analysis/haloproperties/` content before this run's own producer has executed | explorer | "That's leftover output from some prior run, not this run's own result — write the analysis from the documented GenericIO format/halo-selection rules and this run's own params/indat.params, not by testing against old data." |
| Producer and analysis submitted as two unrelated executions, or the analysis runs before the producer finishes | explorer | "They are one workflow: express the analysis as depending on the producer so the engine orders them, rather than running them separately or relying on timing." |
| Analysis stage fails and the explorer re-runs the whole workflow | explorer | "Don't repeat hacc_tpm just to fix an analysis bug — this run's real output already exists on disk from the producer that already ran. Run a workflow containing only the corrected analysis step against that output." |
| Halo selection picks an unexpectedly small/odd halo | explorer | "Select most massive halo by `sod_halo_mass` (M_200c), excluding rows where `sod_halo_count == -101` — not by `fof_halo_mass`." |
| Visualization image missing or blank | explorer | "Verify density_slice data was actually computed (non-zero `sigma`) before rendering; check the halo's z-coordinate was passed through correctly as the slice center." |
| All other failures | explorer | Full stderr content |

---

## Notes

- After a successful run (workflow completed, density slice image produced, summary
  written), route to "end"
- Never route back to planner unless the task list itself is structurally wrong (e.g.
  missing a required stage) — config-value mistakes should go back to explorer with
  feedback to re-read `params/indat.params`/`cosmotools-config.dat`, not to planner
- This is a single-trial-per-run workflow (no per-frame animation stage, unlike
  `molecular_nucleation`) — do not expect or require a GIF/animation output here
