---
name: use_cases/<your workflow name>/domain
description: >
  One line describing your workflow, plus the words that mean "this is my workflow"
  (the tool name, and any file or folder names you use). Example: "Cosmology density
  slice from a HACC simulation — HACC, GenericIO, halo catalog, Last Journey."
---


# Tools
**The tool you run.** Its name, where it lives (the full path), and whether it's
something already installed.
> Example: "Use HACC. The executable is installed at
> `/lcrc/project/PEDAL/jalemu/HACC/.../bin/hacc_tpm`"

# Input Parameters

**The inputs / settings.** Which file(s) hold the settings, and where they are. If
the real values must come from those files, say so.
> Example: "The settings are in `SampleRun_go/params/indat.params` — use the values
> in that file, don't make them up. We want the snapshot at step 624."

# Outputs

The results you expect and what they're called.
> Example:
> - `dm_density_slice.png` — the density picture
> - `summary.txt` — the settings used and the cluster that was picked


# Pitfalls

Things that tend to go wrong, and what to do instead. Once the workflow exits and diagnoses the issue, you can add it here.
> Examples:
> - Reading HACC's output with `pygio` doesn't work — use the `GenericIOPrint` tool instead.
> - "Biggest cluster" means the one with the largest `sod_halo_mass`; skip any row where `sod_halo_count` is `-101`.

# Guidelines

Any rules or things to know about your domain or workflow.
> Examples:
> - This simulation has to be launched as its own job on the supercomputer (a `qsub` submission), and the
>   picture should be made in that same job.
> - Don't change the original settings files — make a copy if a change is needed.
> - The files already sitting in the output folder are from an old run; ignore them.
> - Do multiple runs with modified temperatures for cosmology
