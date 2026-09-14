---
name: use_cases/cosmology/orchestrator
description: >
  Cosmology (HACC) routing rules for the orchestrator. Covers the real-qsub
  architectural exception (producer AND analysis/visualization embedded in one
  generated PBS script, submitted once), requirements approval for this project,
  and HACC-specific error pattern recognition.
---

# Cosmology (HACC) — Orchestrator Skill

Routing rules specific to the HACC cosmological N-body workflow (Last Journey sample run).

---

## When to Use This Skill

Load when orchestrating a HACC / cosmology workflow (goal mentions HACC, GenericIO,
FOF/SOD halo catalogs, "Last Journey", or paths under
`/lcrc/project/PEDAL/jacoboh/HACC/`).

---

## Flow for This Project

```
planner → installer → explorer → end
```

Same shape as other use cases. Route to installer after planner completes.

---

## Architectural Exception: Real `qsub` Is Allowed Here

The general LCRC rule is "the agent never submits a new PBS job — run inside the
existing interactive allocation instead." **This use case is an explicit, deliberate
exception**: the explorer builds one PBS script (e.g. `agent_subme.pbs`, using
`SampleRun_go/subme.pbs` only as a reference for paths) that runs the `hacc_tpm`
producer **and then** the analysis/rendering script, and submits it via a single
`qsub agent_subme.pbs` — not a violation to flag or route back for correction. Do
not give feedback telling the explorer to avoid `qsub` for this project.

Two things to verify:
1. **Genuinely one `qsub` call covering both stages** — if the explorer submits the
   producer via `qsub` and then separately re-runs the analysis/visualization from
   the live session afterward, that *is* a violation (two executions instead of one
   job) and should be routed back.
2. **The analysis/rendering script was written from documented format rules, not
   developed against pre-existing sample output** — `output/full_snapshots/` and
   `analysis/haloproperties/` may already contain content from some prior run; the
   explorer must not read, parse, or render from that content when writing or
   testing `analyze_and_render.py`. That's leftover data from another run, not this
   run's own result.

This exception is specific to this use case — still enforce the no-new-qsub rule for
every other use case (e.g. `molecular_nucleation`, and `eddy_uv`, which runs its
producer via `submit_mpi_task` inside the existing allocation instead).

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
| `ModuleNotFoundError: No module named 'pygio._version'` | explorer | "pygio is not built and should not be built. Use `GenericIOPrint` via subprocess instead." |
| `qsub` / `qstat` command not found or job ID not captured | explorer | "Re-run via submit_shell_task with `cd /lcrc/project/PEDAL/jacoboh/HACC/SampleRun_go && qsub agent_subme.pbs` (the script you built) — qsub must run with that directory as cwd so $PBS_O_WORKDIR resolves." |
| Explorer submits the original `subme.pbs` unmodified instead of building its own combined script | explorer | "Build your own PBS script (e.g. `agent_subme.pbs`) that runs hacc_tpm and then calls analyze_and_render.py, using `subme.pbs` only as a reference for the executable/env/param paths. Submit that generated file, not the original." |
| Explorer reads/parses/renders from `output/full_snapshots/`/`analysis/haloproperties/` content before this run's own producer has executed | explorer | "That's leftover output from some prior run, not this run's own result — write analyze_and_render.py from the documented GenericIO format/halo-selection rules and this run's own params/indat.params, not by testing against old data." |
| Explorer submits the producer via `qsub` and separately re-runs analysis/visualization from the live session afterward (not as failure recovery) | explorer | "The analysis/rendering script must be embedded in the same PBS script and run as part of the same `qsub` job — don't re-execute it afterward from the live session as a matter of course; just read back the PNG/summary the job already produced." |
| Analysis stage fails inside a completed job and the explorer resubmits the whole PBS job | explorer | "Don't requeue hacc_tpm just to fix an analysis bug — this run's real output already exists on disk from the producer that already ran. Fix analyze_and_render.py and re-run it directly via submit_shell_task against that output instead." |
| Halo selection picks an unexpectedly small/odd halo | explorer | "Select most massive halo by `sod_halo_mass` (M_200c), excluding rows where `sod_halo_count == -101` — not by `fof_halo_mass`." |
| Visualization image missing or blank | explorer | "Verify density_slice data was actually computed (non-zero `sigma`) before rendering; check the halo's z-coordinate was passed through correctly as the slice center." |
| All other failures | explorer | Full stderr content |

---

## Notes

- After a successful run (PBS job completed, density slice image produced, summary
  written), route to "end"
- Never route back to planner unless the task list itself is structurally wrong (e.g.
  missing a required stage) — config-value mistakes should go back to explorer with
  feedback to re-read `params/indat.params`/`cosmotools-config.dat`, not to planner
- This is a single-trial-per-run workflow (no per-frame animation stage, unlike
  `molecular_nucleation`) — do not expect or require a GIF/animation output here
