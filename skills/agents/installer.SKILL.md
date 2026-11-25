---
name: agents/installer
description: >
  Operating manual for the installer agent. Two phases: write requirements.txt
  from the planner's stack, stop for the orchestrator, then pip install what the
  orchestrator approved. Covers what the approval gate can and cannot change.
---

# Installer Agent — Base Skill

Prepares the venv for this run. Two phases, with the orchestrator between them.

**You have no language model.** You write a list to disk and run pip. Nothing in
this skill asks you to decide anything, and nothing you are told between the two
phases can change what you produce — see the gate below.

## Phase 1: write the list, then stop

Write `work/builds/requirements.txt` from the planner's `stack_decision`,
report `installer_requirements_pending_approval`, and stop.

**Always regenerate the file.** A `requirements.txt` already on disk is from a
previous run, possibly a different engine or domain, and reusing it is how a
stale package reaches the venv.

## Phase 2: install what was approved

Entered when `requirements_approved` is set. `requirements_override` wins over
`stack_decision` — it is the orchestrator's corrected list, and if one is present
the file on disk is stale, so rewrite it before reading it back.

Read the list back **from the file**, not from `stack_decision`: the orchestrator
approved the file, and rebuilding from state would install something it never saw.

Then `pip install` into the interpreter this process is running in. If
`state["modules"]` is non-empty, run pip under those modules in a login shell,
after a `module purge` — a compiled package (`mpi4py`, anything with MPI-linked
extensions) otherwise builds against whatever the launching shell happened to
have, which is not the MPI the tasks will load.

A pip failure is **reported, not raised**. The explorer has `check_package` and
can report what is actually importable, which is better evidence than pip's exit
code.

## What the approval gate can and cannot do

The gate buys one thing: the orchestrator sees the package list **before**
anything is installed.

It cannot buy a different list out of you. You have no model, so re-entering
phase 1 regenerates a byte-identical file. The only thing that changes the
outcome is the orchestrator supplying `requirements_override`, which it does
itself — it never sends the work back here for a rewrite, because there is
nothing here to rewrite it with.

## Constraints

- Never install in phase 1, and never install without an approval in state.
- Only pip packages. Environment modules are loaded per task, not installed.
- Nothing else is tracked: no build state, no runtime state, no other file.
