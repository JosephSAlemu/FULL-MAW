---
name: systems/parsl
description: >
  Writing a Parsl workflow file for MAW: the execution contract, the four rank
  layouts (one/many nodes x serial/parallel), executor and worker sizing, file
  passing, and the pitfalls table. Load for every --engine parsl run; the API
  manual is knowledge/PARSL.
---

# Parsl — System Skill

Parsl runs every step of a workflow — simulation binaries and Python analysis
alike — as apps scheduled by a DataFlowKernel inside the one file you generate.
Load for every `--engine parsl` run; the API manual is `knowledge/PARSL`.

**This run's engine is Parsl. Do not import, use or reference PyCOMPSs, ADIOS2
or Wilkins.**

# Rules

## Main tools

| Tool | What it does |
|---|---|
| `write_file(path, content)` | Writes a file under `/app/work/run0/`. The ONE Parsl driver goes through here, and so does anything that driver reads |
| `run_workflow(filename)` | The server launches that driver as `<python> workflow.py` |

These are the only execution tools. Nothing takes a code string or a command
string and runs it for you, and `ranks` on `run_workflow` does nothing here --
rank counts live in the `@bash_app` command string.

## ONE file, and Parsl runs everything in it

Generate exactly one `workflow.py` holding the whole workflow as Parsl apps. A
simulation plus its visualization is one file with one `@bash_app` and one
`@python_app` -- not two files and not two `run_workflow` calls.

Never execute anything yourself: no `subprocess`, no CLI call, no bash or PBS
script, no `mpirun` typed by you. Shell work is a `@bash_app` whose body
**returns the command string**; Python work is a `@python_app`.

## Scope matters: there are no globals

**Every import and every constant an app uses must be inside that app's own
body, or passed in as a parameter.** The worker is a separate process that never
imported your file, so a module-level name used in a body is a `NameError` on
the worker -- the single most common way a file that looks right fails at run
time.

```python
# WRONG                                   # RIGHT
import os                                 @python_app
WORD = "hello"                            def a(word):
@python_app                                   import os
def a():                                      os.makedirs("d", exist_ok=True)
    os.makedirs("d")   # NameError            print(word)
    print(WORD)        # NameError
```

A constant in a DEFAULT argument is fine; one read in the body is not. The same
applies to module-level helper functions -- inline it or make it its own app.

## Match the allocation's walltime on every `@bash_app`

`walltime` is in SECONDS and defaults to none. Set it to the allocation's, so an
app cannot outlive the job holding it: `def sim(..., walltime=3600)` for a
`01:00:00` allocation.

## Resources come from `get_resources`, not from the file

Call `get_resources` **before** writing anything that asks for ranks:

| Field | Meaning |
|---|---|
| `nnodes` | how many nodes are available |
| `ntasks` | total rank slots across all of them |
| `ranks_per_node` | rank slots on each node |
| `nodelist` | the node names, comma-separated |
| `launcher` | the launcher to call |

Those numbers go into the generated file **as constants**, written by you from
what the tool returned. The workflow file itself discovers nothing: it reads
no environment variables, inspects no scheduler, and reads no host file.
Everything it needs was known before it was written.

```python
# From get_resources(). Do not compute these inside the file.
NODES = 1
RANKS_PER_NODE = 8
LAUNCHER = "mpirun"
```

**Totals must fit.** `tasks x ranks_per_task` may not exceed
`nnodes x ranks_per_node`. Check that in `main()` and fail with the numbers,
rather than letting the launcher report "not enough slots available" from inside
a worker.

**The launcher is a parameter, not a literal.** Pass it into every app that
launches ranks. A bare `mpirun` resolves to whatever is first on PATH, and
against a program built for a different MPI it silently produces N independent
one-rank jobs that overwrite each other's output and exit 0.

## 1. One node, multiple ranks, one task

A single multi-rank program on one node. Resources: 1 node, 8 rank slots, one
task using all 8.

`max_workers_per_node=1`: the `@bash_app` calling the launcher occupies **one
worker slot**, and the ranks are not worker slots — the launcher places them.

```python
import os

import parsl
from parsl import bash_app, python_app
from parsl.config import Config
from parsl.data_provider.files import File
from parsl.executors import HighThroughputExecutor
from parsl.providers import LocalProvider

WORK = "/app/work/run0"

# From get_resources().
NODES = 1
RANKS_PER_NODE = 8
LAUNCHER = "mpirun"

NRANKS = NODES * RANKS_PER_NODE

@bash_app
def simulate(exe, paramfile, nranks, rundir, launcher,
             inputs=(), outputs=(),
             stdout=parsl.AUTO_LOGNAME, stderr=parsl.AUTO_LOGNAME):
    """Return the COMMAND STRING; Parsl runs it. cd into the run directory --
    a program writing relative to its cwd must start where this run can write."""
    product = outputs[0].filepath
    return (
        f"set -eo pipefail && "
        f"cd {rundir} && "
        f"{launcher} -n {nranks} {exe} {paramfile} && "
        f"test -s {product}"
    )

@python_app
def summarize(outdir, inputs=()):
    """All imports inside the body -- the worker never imported this file."""
    import os
    product = inputs[0].filepath          # Parsl guarantees it exists here
    report = os.path.join(outdir, "summary.txt")
    with open(report, "w") as f:
        f.write(f"bytes: {os.path.getsize(product)}\n")
    return report

def main():
    config = Config(
        executors=[
            HighThroughputExecutor(
                label="htex",
                max_workers_per_node=1,    # ONE worker: it runs the launcher
                provider=LocalProvider(init_blocks=1, min_blocks=1, max_blocks=1),
            )
        ],
        run_dir=os.path.join(WORK, "runinfo"),
        strategy="none",
        initialize_logging=False,
    )
    parsl.load(config)
    try:
        os.makedirs(WORK, exist_ok=True)
        sim = simulate(
            exe="/abs/path/to/binary", paramfile="/abs/path/to/params",
            nranks=NRANKS, rundir=WORK, launcher=LAUNCHER,
            outputs=[File(os.path.join(WORK, "result.dat"))],
        )
        # Wired by the DataFuture, not by a .result() call.
        report = summarize(WORK, inputs=sim.outputs)
        print(report.result())            # the ONLY .result()
    finally:
        parsl.clear()

if __name__ == "__main__":
    main()
```

## 2. One node, multiple ranks, parallel tasks

Several multi-rank programs at once, sharing one node. Resources: 1 node, 8 rank
slots, two tasks of 4 ranks each. Two things change from example 1:

**`max_workers_per_node` becomes the task count** — each concurrent launcher
needs its own worker slot, from the same variable the DAG loops over. A
hardcoded number silently serializes the run.

**Each task needs a disjoint core range.** Concurrent launchers all claim slots
from the front of whatever list the launcher consults, so without pinning both
tasks land on cores 0-3 and half the node idles. `taskset -c` restricts a
launcher and every rank it starts, and works for any launcher — a launcher's own
pinning flags do not.

```python
# From the goal.
NTASKS = 2
RANKS_PER_TASK = 4

@bash_app
def simulate(exe, paramfile, nranks, cores, rundir, launcher,
             inputs=(), outputs=(),
             stdout=parsl.AUTO_LOGNAME, stderr=parsl.AUTO_LOGNAME):
    """Pinned to `cores`; the ranges are disjoint across instances."""
    product = outputs[0].filepath
    return (
        f"set -eo pipefail && "
        f"cd {rundir} && "
        f"taskset -c {cores} {launcher} -n {nranks} {exe} {paramfile} && "
        f"test -s {product}"
    )

# In main(): config as example 1 but max_workers_per_node=NTASKS and
# cpu_affinity="none". Check the budget before submitting anything.
if NTASKS * RANKS_PER_TASK > NODES * RANKS_PER_NODE:
    raise SystemExit(f"need {NTASKS * RANKS_PER_TASK} rank slots")

futures = []
for i in range(NTASKS):
    rundir = os.path.join(WORK, f"task{i}")
    os.makedirs(rundir, exist_ok=True)
    first = i * RANKS_PER_TASK
    futures.append(simulate(
        exe=EXE, paramfile=f"/abs/path/to/params_{i}",
        nranks=RANKS_PER_TASK,
        cores=f"{first}-{first + RANKS_PER_TASK - 1}",
        rundir=rundir, launcher=LAUNCHER,
        outputs=[File(os.path.join(rundir, "result.dat"))],
    ))

# Everything was submitted before this loop, so the instances overlap; the loop
# only fixes the printing order. exception() first, so one failure does not hide
# the other instances' results.
failed = [i for i, f in enumerate(futures) if f.exception() is not None]
if failed:
    raise SystemExit(f"failed: {failed}")
```

## 3. Multiple nodes, multiple ranks, one task

One program whose rank count exceeds a single node. Resources: 2 nodes, 4 rank
slots each, one task using all 8.

Still **one worker**, as in example 1: the pool sits on one node and runs the
launcher, which reaches the others itself. Parsl places nothing on them, so the
program owns them as one MPI world.

The layout flag must agree with `ranks_per_node`. `--map-by ppr:4:node` against
4 slots per node is right; the same flag where there are 8 reports "not enough
slots available". 1x8 and 2x4 are both 8 ranks and are not interchangeable.

Only the app body changes from example 1:

```python
NODES = 2
RANKS_PER_NODE = 4
NRANKS = NODES * RANKS_PER_NODE
NTHREADS = 1          # threads per rank; see the note in the app body

@bash_app
def simulate(exe, paramfile, nranks, ranks_per_node, nthreads, rundir, launcher,
             inputs=(), outputs=(),
             stdout=parsl.AUTO_LOGNAME, stderr=parsl.AUTO_LOGNAME):
    """One program across every available node. ranks_per_node builds the
    layout flag, so the layout asked for is the one that exists. Without
    OMP_NUM_THREADS a threaded program starts one thread per core in every rank
    and they contend; 1 is right when all the parallelism is in the ranks. The
    layout flag spelling is MPI-specific -- take it from the domain skill."""
    product = outputs[0].filepath
    return (
        f"set -eo pipefail && "
        f"cd {rundir} && "
        f"{launcher} -n {nranks} "
        f"--map-by ppr:{ranks_per_node}:node "
        f"-x OMP_NUM_THREADS={nthreads} "
        f"{exe} {paramfile} && "
        f"test -s {product}"
    )
```

## 4. Multiple nodes, multiple ranks, parallel tasks

More instances than one node can hold; each gets its own nodes, so they cannot
contend. Resources: 4 nodes, 4 rank slots each, four tasks of 4 ranks.

`taskset` is the wrong tool here — it pins within the node a launcher starts on
and says nothing about which nodes it picks. Give each instance a **host file**
naming only its own nodes. A `@python_app` slices `NODELIST` and declares one
`File` per instance; each `@bash_app` takes its own as `inputs`, so Parsl will
not start an instance until its host file exists. `max_workers_per_node` is
`NTASKS`, as in example 2.

```python
# From get_resources(). nodelist comes back comma-separated; split it here so
# the file never has to parse anything at run time.
NODELIST = ["node001", "node002", "node003", "node004"]
RANKS_PER_NODE = 4

# From the goal.
NTASKS = 4
NODES_PER_TASK = 1
RANKS_PER_TASK = NODES_PER_TASK * RANKS_PER_NODE

@python_app
def write_hostfiles(nodelist, nodes_per_task, ranks_per_node, outputs=()):
    """One host file per instance. One output File each, not one for the lot,
    so a consumer depends on its own file rather than on "splitting finished"."""
    import os
    for i, out in enumerate(outputs):
        mine = nodelist[i * nodes_per_task:(i + 1) * nodes_per_task]
        if len(mine) < nodes_per_task:
            raise RuntimeError(f"instance {i} wanted {nodes_per_task} node(s), "
                               f"only {len(mine)} left of {len(nodelist)}")
        os.makedirs(os.path.dirname(out.filepath), exist_ok=True)
        with open(out.filepath, "w") as f:
            for host in mine:
                # "<host> slots=<n>" is one spelling; others want "<host>:<n>".
                # Take the format from the domain skill file.
                f.write(f"{host} slots={ranks_per_node}\n")

@bash_app
def simulate(exe, paramfile, nranks, rundir, launcher,
             inputs=(), outputs=(),
             stdout=parsl.AUTO_LOGNAME, stderr=parsl.AUTO_LOGNAME):
    """inputs[0] is this instance's host file -- a DataFuture, so Parsl will
    not start this app until write_hostfiles has produced it. The host file flag
    is MPI-specific (--hostfile, -f, -machinefile); take it from the domain
    skill."""
    hostfile = inputs[0].filepath
    product = outputs[0].filepath
    return (
        f"set -eo pipefail && "
        f"cd {rundir} && "
        f"{launcher} -n {nranks} --hostfile {hostfile} {exe} {paramfile} && "
        f"test -s {product}"
    )

# In main(), after parsl.load():
hosts = write_hostfiles(
    NODELIST, NODES_PER_TASK, RANKS_PER_NODE,
    outputs=[File(os.path.join(WORK, f"task{i}", "hosts")) for i in range(NTASKS)],
)
futures = [
    simulate(exe=EXE, paramfile=f"/abs/path/to/params_{i}",
             nranks=RANKS_PER_TASK, rundir=os.path.join(WORK, f"task{i}"),
             launcher=LAUNCHER, inputs=[hosts.outputs[i]],
             outputs=[File(os.path.join(WORK, f"task{i}", "result.dat"))])
    for i in range(NTASKS)
]
```

# Configuring Parsl

## The driver never asks for resources

They already exist when the driver starts; `get_resources` is how you learn
their shape and the numbers go in as constants. Use `LocalProvider` with one
fixed block and let the launcher inside a `@bash_app` reach other nodes itself.
Never use a scheduler provider in a generated workflow, and never write a submit
script.

## `max_workers_per_node` is the concurrency knob

It is how many apps may run at once.

| Situation | Value |
|---|---|
| One app that launches its own ranks | `1` |
| K launcher apps at once | `K`, plus disjoint `taskset` ranges (one node) or host files (many) |
| K independent single-process chains | the DAG's PEAK concurrency, not necessarily K |

Size it from the same variable the DAG loops over. For the third row count the
real peak: a stage fanning out to two consumers of one future wants two slots.

`cores_per_worker` is **not** a reservation -- it is a DIVISOR for the worker
count. On 128 cores `cores_per_worker=8` gives 16 workers; set above the visible
core count it gives **zero** workers and the DAG hangs with no error. Use
`max_workers_per_node` for concurrency and `taskset` in the command string to
give an app a core range, with `cpu_affinity="none"` so Parsl's own affinity
does not fight it.

## Keep the executor process-based

Do not swap `HighThroughputExecutor` for `ThreadPoolExecutor` when apps touch
**global library state** -- a module-level scene, context or registry, as most
visualization packages have. Separate worker processes keep concurrent instances
independent; threads share one interpreter and corrupt each other. Unregister
global state in a `finally` so the next task on that worker does not inherit it.

## `initialize_logging=False`

The default `True` writes a DEBUG `parsl.log` including the scaling strategy
every 5 seconds -- thousands of lines of `more slots than tasks` -- into the
directory whose contents are the run's scientific output.

**Turn it back on for one run when a DAG hangs or fails in a way the result does
not explain.** `parsl.log` is the only record of submission and dependency
resolution: whether an app was submitted at all, which executor took it, what it
waited on, and the full traceback of a failed app body. Or keep the trail and
drop the polling noise:

```python
parsl.load(config); logging.getLogger("parsl").setLevel(logging.INFO)
```

## The skeleton

Example 1's `Config` is the skeleton: `label`, `max_workers_per_node`,
`provider`, plus `run_dir` (else logs land in the process CWD), `strategy="none"`
(fixed topology) and `initialize_logging=False`. Add `cpu_affinity="none"` when
`taskset` does the pinning. Add nothing else -- an unrecognized keyword is a
`TypeError` at load. Lifecycle is `parsl.load(config)` once at the top of
`main()`, submit, resolve once, `parsl.clear()` in a `finally`.

# Files Between Apps

## Declare products, do not name them in strings
A path passed between apps as a plain string is invisible to the DataFlowKernel:
it cannot order the apps, stage the file, or tell you the producer never wrote
it. Read the path off the `File` object inside the body, so the command writes
exactly what Parsl tracks.

```python
@bash_app
def simulate(exe, rundir, outputs=()):
    return f"cd {rundir} && {exe} > {outputs[0].filepath}"

@python_app
def analyze(inputs=()):
    import os
    return os.path.getsize(inputs[0].filepath)   # Parsl guarantees it exists

sim = simulate(EXE, WORK, outputs=[File(os.path.join(WORK, "result.dat"))])
out = analyze(inputs=sim.outputs)                # the edge, not a .result()
```

One `File` per product, not one for the lot, so a consumer depends on exactly
the file it reads.

A future passed as an ORDINARY argument arrives in the body as its resolved
value, so a small picklable dict of numbers and strings needs no `File` at all.
Do not return an open handle or a library object.

## Build the DAG for N, resolve once

```python
def build_instance(i, ranks):
    ...                        # this instance's apps, wired to each other
    return terminal_future     # module-level helper, called from main()

finals = [build_instance(i, RANKS_PER_TASK) for i in range(NTASKS)]
for f in finals:
    print(f.result())
```

Everything that differs between instances -- directory, input file, core range,
host file -- is derived from the index inside that function. Resolving in a loop
still submits everything up front, so the instances overlap. Where the instance
count exceeds what can run at once, keep the DAG and let the worker count
throttle it.

| Pitfall | Rule |
|---|---|
| Looking for a tool that runs a code or command string | There isn't one. Use `write_file` + `run_workflow` |
| Splitting a workflow across multiple generated files | One file per workflow; multiple apps inside it |
| Writing a shell or submit script to run the work | Never. The command belongs in a `@bash_app` string |
| `import subprocess` / `os.system` in the generated file | Takes the work out of Parsl — express it as a `@bash_app` |
| A launcher call in the driver body instead of inside a `@bash_app` | Parsl never sees the launch — move it into the app body |
| The workflow file discovering its own resources | It cannot see them reliably and does not need to. Call `get_resources` first and write the numbers in as constants |
| A module-level import, constant or helper used in an app body | `NameError` on the worker — it never imported your file. Import inside the body, pass constants as parameters (a DEFAULT argument is fine), and inline or app-ify the helper |
| `.result()` inside an app, or between submissions | Inside an app it deadlocks the worker; between submissions it serializes the driver and hides the DAG. Submit everything, resolve once, in `main()` |
| Passing file paths between apps as plain strings | Invisible to the DFK. `outputs=[File(p)]` on the producer, `inputs=producer.outputs` on the consumer. An `os.path.exists` check on another app's output is the symptom |
| Debugging the app named in `DependencyError` | That app never ran. Read the chain back to its root and debug the app in the FIRST traceback |
| Trusting a `BashExitFailure` exit code at face value | It is the LAST command's code, and MPI aborts are truncated mod 256. Read the app's stderr file |
| Omitting `stdout=`/`stderr=` on a `@bash_app` | The output is discarded and a failure has no explanation at all. Pass `parsl.AUTO_LOGNAME` for both |
| Leaving `run_dir` at its default | Per-app logs land relative to the process CWD, a different tree from the server's. Pin it under the work directory |
| Relying on plain `set -e` | It ignores failure on the LEFT of a pipe (use `set -eo pipefail`) and does not catch an unset variable (add `set -u`) |
| `except OSError: pass` around setup inside an app | Hides the real failure and returns the downstream symptom from somewhere unrelated. Setup must fail loudly |
| `cores_per_worker=N` to "size a slot for N ranks", or `max_workers_per_node=1` for K chains | `cores_per_worker` is a DIVISOR — above the visible core count it gives ZERO workers and the DAG hangs silently. Size `max_workers_per_node` to the DAG's peak concurrency |
| Two or more launcher apps at once without separation | They all claim slots from the front of the same list and overlap. Disjoint `taskset` ranges on ONE node; per-instance host files ACROSS nodes — `taskset` cannot pick nodes |
| A hardcoded instance count or rank count | Both come from the goal and from `get_resources`. Derive the worker count from the same variable |
| `--map-by ppr:N:node` with N != the available ranks per node | "not enough slots available". 1x8 and 2x4 are both 8 ranks and are not interchangeable |
| Instances x ranks greater than the available slots | Check it in `main()` against the constants and fail with the numbers, rather than letting the launcher report it from inside a worker |
| Many workers with `OMP_NUM_THREADS` unset | Each rank starts one thread per core and they contend. Set it explicitly |
| Using whatever launcher is on `PATH` for a program built against another MPI | That launcher belongs to a different MPI. Ranks then run as N separate one-rank jobs and exit 0. Use the launcher `get_resources` and the domain skill name, and verify the rank count from the program's own output |
| A file that re-invokes itself as its own ranks | Guard on an explicit `--mpi` flag checked first in `__main__` (launcher env vars are unreliable both ways), import MPI-linked libraries inside the rank function, and use `parse_known_args` — some launchers append arguments |
| A `@python_app` that assigns a library-path variable then imports the library | No effect — the loader fixed its search path when the worker started. Use a `@bash_app` that exports then launches |
| `ThreadPoolExecutor` for apps touching global library state | They share one interpreter and corrupt each other. Use process-based HTEX, and unregister global state in a `finally` so the next task does not inherit it |
| One app producing both a cheap and an expensive deliverable | A failure in the expensive half takes the cheap one with it. Split them |
| Rendering without `matplotlib.use("Agg")` | Headless nodes have no display; set it INSIDE the `@python_app` |
| `rm -rf` on a directory the app also reads or writes | A retry wipes the previous attempt's output before you can read it. `mkdir -p`, and remove only the subdirectory being replaced |
| Simulation app invoked more than once per run | Call it exactly once per workflow invocation — re-invoking on replan silently produces duplicate runs instead of reusing the result |
| Deviating from the config skeleton | An extra kwarg is a `TypeError` at load; `strategy` other than `"none"` makes auto-scaling fight fixed resources; `initialize_logging` left at its default floods the run directory. `parsl.load()` twice without `parsl.clear()` raises `NoDataFlowKernelError` |
| `WorkerLost` | The worker process crashed — read stderr for the actual exception |
