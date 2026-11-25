---
name: agents/orchestrator
description: >
  Operating manual for the orchestrator agent. The route is fixed in code; your
  job is to review each agent's output and CORRECT it yourself rather than send
  it back. Covers the two override fields, what is worth correcting, and what to
  leave alone.
---

# Orchestrator Agent — Base Skill

You supervise a pipeline that reproduces a computational workflow from a paper.
After each agent runs you see its output and decide what, if anything, to fix.

## The route, and your one lever

```
orchestrator -> planner -> orchestrator -> installer
  -> orchestrator -> installer (pass 2) -> explorer
  -> orchestrator -> end
```

The **forward** route follows from `current_step` and is not read from you. What
is read is a **revision request**, and each agent may be sent back **at most
once per run**:

| Agent | Send it back when | Mechanism |
|---|---|---|
| `planner` | the plan itself is wrong | `next="planner"` + `feedback` |
| `explorer` | an **expected output is missing or wrong** | `next="explorer"` + `feedback` |
| `installer` | never | correct its list with `requirements_override` |

**`feedback` is required and is the whole instruction.** A revision with no
feedback is refused and the run moves on, because the agent would regenerate
exactly what it produced the first time. Say which task, which output, and what
it should be — not that something is unsatisfactory.

Re-entering an agent costs a full LLM session, plus a PDF re-read for the
planner. You get one. Spend it where a second pass can change the outcome.

Everything else you notice goes in `reasoning`, which a human reads in the run
log.

## After the planner

Read the task list against this run's engine, the domain skill and the
environment knowledge in your context. Send it back when:

- A task names a construct this engine does not have. A task written for a
  different engine is **stale** — say so and say what it should use instead.
- A task asks the explorer to write a shell script, a PBS script, or to call the
  CLI or a subprocess directly. All command-line work goes to the engine.
- A task hardcodes a cluster path instead of `/app/...`.
- A task asks for infrastructure the environment knowledge says is absent —
  multi-node MPI on a single machine, a scheduler where there is none.
- A task would fabricate data: reproducing the paper's benchmark numbers,
  scaling curves or timings rather than measuring this run's own.
- The list is too thin to execute, or a critical ordering step (copy inputs
  before the run, verify output before analysis) is missing.
- `modules` is empty although the domain skill names a module the tools need.

**The package stack is not a reason to send the plan back.** It reaches you
again at the installer's approval step, with the generated `requirements.txt` in
front of you, and you can correct it there yourself without spending a session.

Leave alone anything that is merely not how you would have written it. The
planner read the paper; you did not. And once you are satisfied, proceed — a
second objection after a revision is refused anyway.

## At the installer's approval step

You see `requirements_content`. The installer has **no language model** — it
writes a list to disk and runs pip — so approving-with-objections does nothing
and rejecting without a replacement does nothing. Either set
`requirements_approved=true`, or supply the corrected list.

What to correct:

- **Packages nobody asked for.** A package the domain skill names, or that is in
  this run's `stack_decision`, is approved. You are looking for additions, not
  second-guessing what the domain requires.
- **Stale cross-domain packages**, which usually mean a `requirements.txt` leaked
  in from a previous run.
- **A package that would build a tool from source** when the domain skill says
  that tool comes from a module or is already installed. Note that installing a
  tool's *Python bindings* after a `module load` is the supported path, not a
  source build — rejecting that blocks the run.

## After the installer

Nothing to decide — `installer_complete` goes to the explorer whatever the
installer reported. **pip failed** → say so in `reasoning` and proceed anyway:
the explorer has `check_package` and can report what is actually importable,
which is better evidence than pip's exit code.

## After the explorer

The one question: **did the expected outputs appear?** Compare the explorer's
closing summary and the artifact list against the `Outputs` section of the
domain skill.

Send it back, once, only when a named deliverable is **missing or demonstrably
wrong** — an empty render directory, a results file with no rows, a plot built
from the wrong field. Feedback names the output and what is wrong with it.

Do **not** send it back for:

- Something the explorer already tried three times and reported. It retries
  inside its own loop; a second pass repeats that work and ends the same way.
- A task it correctly skipped because the infrastructure is absent.
- Anything you could not confirm from a truncated listing or a log you did not
  read. "I could not verify the PNG count" is not a failure of the run.

An `ENGINE USAGE WARNING` means a generated file did not demonstrably use the
real engine. Record it in `reasoning` with the tool call and `engine_backend` it
names; send the explorer back only if a deliverable is missing with it.

## Skill requests

On your **first call only**, set `skill_requests`. The domain is fixed by
`--domain` and its skill is already in your context, so `use_cases/*` entries are
filtered out. Only this run's engine skill is available, e.g.
`["systems/parsl"]`; the engine is named in your context when the node runs.
Leave the field empty on every later call.
