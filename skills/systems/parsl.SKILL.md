---
name: systems/parsl
description: >
  Parsl parallel scripting library reference for MAW workflows. Covers the generated-file
  execution model (write_file/run_workflow), @python_app/@bash_app rules,
  HighThroughputExecutor config, reading a failed run, worker sizing, common pitfalls,
  and the exact config skeleton used here.
---

# Parsl — System Skill

Parsl orchestrates workflow steps as asynchronous Python functions. In MAW it runs
every step of a workflow — simulation binaries and Python analysis alike — as apps
scheduled by a Parsl DataFlowKernel. 

---

## When to Use This Skill

Load when generating or debugging Parsl workflow code. Covers the exact config and API patterns used in this project.

**IF YOU USE THIS SKILL, DO NOT IMPORT, USE, OR REFERENCE ANYTHING RELATED TO PYCOMPSS OR ADIOS**

---

# Rules

## Main tools

You have multiple tools derived from `/home/jalemu/FULL-MAW/servers/server.py`. Your main tools for creating and executing a file are:

| Tool | What it does |
|---|---|
| `write_file(path, content)` | Writes a file under `/app/work/run0/`. The ONE standalone Parsl driver file goes through here, and so does anything that file reads |
| `run_workflow(filename)` | The server launches that driver as `<python> workflow.py` |

These are the only execution tools this server has. There is no tool that takes
a code string or a command string and runs it for you.

## Parsl scoping rules

All modules and constants must be put in the body of the method you want to use them in. There is no such thing as a "global" in parsl.

## You Write One Parsl File, Parsl Runs Everything

**REMEMBER ONLY CREATE ONE PYTHON FILE**
There should only be one python file generated: workflow.py using the write_file method. It should have the entire workflow as parsl methods.

For example: A simulation plus its visualization is one file
containing one `@bash_app` and one `@python_app` — not two files, not two tool calls.

## Don't use other tools

**Don't ever execute the code as an agent**
 - No subprocess
 - No direct CLI call 
 - No bash
 - no PBS script, no `mpirun` typed by you.

When it comes to code, you should generate parsl code. 
If you need to use the shell or an external tool, use a @bash_app
If you need to create a parallel python program, you annotate a method with @python_app

## Scope Matters!
**!!!!!!ALL IMPORTS, CONSTANTS, ETCS. FOR PARSL ARE LOCAL TO THE METHODS THEMSELVES. YOU MUST IMPORT IN THE METHODS TO USE IN THE METHOD!!!!!!!!!!!!!!!**

#### WRONG
```python
import os
WORD = "hello"
@python_app
def a():
    os.mkdir() # YOU CAN'T USE OS IN THE METHOD. IT'S NOT A GLOBAL IMPORT
    print(WORD) # YOU CAN'T USE A CONSTANT IN THE BODY. CONSTANTS AREN'T GLOBAL
```
#### RIGHT
```python
@python_app
def a():
    import os
    WORD = "hello"
    os.mkdir() # Now that the import is local, it can create the directory.
    print(WORD) # Now that the constant is local (or pass as an argument), you can use it.

```
---

# Code Examples

## Resources come from `get_resources`, not from the file

Call `get_resources` **before** writing a workflow that asks for ranks. It
reports what this server can actually see:

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

Two consequences worth stating:

**Totals must fit.** `tasks x ranks_per_task` may not exceed
`nnodes x ranks_per_node`. Check that in `main()` against the constants and
fail with the numbers, rather than letting the launcher report "not enough
slots available" from inside a worker.

**The launcher is a parameter, not a literal.** Which launcher to call, and
with which flags, comes from `get_resources` and from the domain skill file.
Pass it into every app that launches ranks so the file has one place to
change. A bare `mpirun` resolves to whatever is first on PATH, and on a
program built against a different MPI it produces N independent one-rank jobs
that overwrite each other's output and exit 0.

---

## 1. One node, multiple ranks, one task

The simplest shape: a single multi-rank program on one node.

Resources: 1 node, 8 rank slots. One task using all 8.

`max_workers_per_node=1`, because the `@bash_app` that calls the launcher
occupies **one worker slot** and the ranks are not worker slots — the launcher
places them itself.

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
    """Return the COMMAND STRING. Parsl runs it; the driver never does.

    cd into the run directory: a program that writes relative to its cwd must
    start in the directory this run owns and can write to.
    """
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
    os.makedirs(outdir, exist_ok=True)
    report = os.path.join(outdir, "summary.txt")
    with open(report, "w") as f:
        f.write(f"bytes: {os.path.getsize(product)}\n")
    return report


def main():
    config = Config(
        executors=[
            HighThroughputExecutor(
                label="htex",
                # ONE worker: it is the thing that runs the launcher.
                max_workers_per_node=1,
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
            exe="/abs/path/to/binary",
            paramfile="/abs/path/to/params",
            nranks=NRANKS,
            rundir=WORK,
            launcher=LAUNCHER,
            outputs=[File(os.path.join(WORK, "result.dat"))],
        )

        # Wired by the DataFuture, not by a .result() call.
        report = summarize(WORK, inputs=sim.outputs)

        print(report.result())        # the ONLY .result()
    finally:
        parsl.clear()


if __name__ == "__main__":
    main()
```

---

## 2. One node, multiple ranks, parallel tasks

Several multi-rank programs at once, sharing one node. Each gets a slice of
the ranks.

Resources: 1 node, 8 rank slots. Two tasks of 4 ranks each.

Two things change from example 1:

**`max_workers_per_node` is the task count.** Each concurrent launcher needs
its own worker slot.

**Each task needs a disjoint core range.** Concurrent launchers started from
the same place all claim rank slots from the front of whatever list the
launcher consults, so without pinning both tasks land on cores 0-3 and half
the node sits idle. `taskset -c` restricts a launcher and every rank it starts
to a core set, and works the same way for any launcher — a launcher's own
pinning flags do not.

```python
import os

import parsl
from parsl import bash_app
from parsl.config import Config
from parsl.data_provider.files import File
from parsl.executors import HighThroughputExecutor
from parsl.providers import LocalProvider

WORK = "/app/work/run0"

# From get_resources().
NODES = 1
RANKS_PER_NODE = 8
LAUNCHER = "mpirun"

# From the goal.
NTASKS = 2
RANKS_PER_TASK = 4


@bash_app
def simulate(exe, paramfile, nranks, cores, rundir, launcher,
             inputs=(), outputs=(),
             stdout=parsl.AUTO_LOGNAME, stderr=parsl.AUTO_LOGNAME):
    """One instance, pinned to `cores`.

    The ranges are disjoint across instances, so two instances cannot land on
    the same cores however the launcher assigns its slots.
    """
    product = outputs[0].filepath
    return (
        f"set -eo pipefail && "
        f"cd {rundir} && "
        f"taskset -c {cores} {launcher} -n {nranks} {exe} {paramfile} && "
        f"test -s {product}"
    )


def main():
    slots = NODES * RANKS_PER_NODE
    if NTASKS * RANKS_PER_TASK > slots:
        raise SystemExit(
            f"need {NTASKS * RANKS_PER_TASK} rank slots, "
            f"have {slots} ({NODES} node(s) x {RANKS_PER_NODE})"
        )

    config = Config(
        executors=[
            HighThroughputExecutor(
                label="htex",
                # One worker per concurrent instance, from the same variable
                # the DAG loops over. A hardcoded number silently serializes.
                max_workers_per_node=NTASKS,
                cpu_affinity="none",        # taskset does the pinning
                provider=LocalProvider(init_blocks=1, min_blocks=1, max_blocks=1),
            )
        ],
        run_dir=os.path.join(WORK, "runinfo"),
        strategy="none",
        initialize_logging=False,
    )
    parsl.load(config)
    try:
        futures = []
        for i in range(NTASKS):
            rundir = os.path.join(WORK, f"task{i}")
            os.makedirs(rundir, exist_ok=True)

            first = i * RANKS_PER_TASK
            cores = f"{first}-{first + RANKS_PER_TASK - 1}"

            futures.append(
                simulate(
                    exe="/abs/path/to/binary",
                    paramfile=f"/abs/path/to/params_{i}",
                    nranks=RANKS_PER_TASK,
                    cores=cores,
                    rundir=rundir,
                    launcher=LAUNCHER,
                    outputs=[File(os.path.join(rundir, "result.dat"))],
                )
            )

        # Everything was submitted before this loop, so the instances overlap;
        # the loop only fixes the printing order. exception() first, so one
        # failure does not hide the other instances' results.
        failed = []
        for i, fut in enumerate(futures):
            err = fut.exception()
            if err is None:
                print(f"task{i} ok")
            else:
                failed.append(i)
                print(f"task{i} FAILED: {err}")
        if failed:
            raise SystemExit(f"failed: {failed}")
    finally:
        parsl.clear()


if __name__ == "__main__":
    main()
```

---

## 3. Multiple nodes, multiple ranks, one task

One program whose rank count is more than a single node provides. The launcher
spreads its ranks across every available node.

Resources: 2 nodes, 4 rank slots each. One task using all 8.

Still **one worker**. The worker pool sits on one node and runs the launcher;
the launcher reaches the others itself. Parsl places nothing on them, so the
program owns them outright as one MPI world.

The layout flag must agree with `ranks_per_node`. `--map-by ppr:4:node`
against 4 slots per node is right; the same flag where there are 8 reports
"not enough slots available". 1x8 and 2x4 are both 8 ranks and are not
interchangeable, which is why the number comes from `get_resources` rather
than being written from memory.

```python
import os

import parsl
from parsl import bash_app
from parsl.config import Config
from parsl.data_provider.files import File
from parsl.executors import HighThroughputExecutor
from parsl.providers import LocalProvider

WORK = "/app/work/run0"

# From get_resources().
NODES = 2
RANKS_PER_NODE = 4
LAUNCHER = "mpirun"

NRANKS = NODES * RANKS_PER_NODE
NTHREADS = 1          # threads per rank; see the note in the app body


@bash_app
def simulate(exe, paramfile, nranks, ranks_per_node, nthreads, rundir, launcher,
             inputs=(), outputs=(),
             stdout=parsl.AUTO_LOGNAME, stderr=parsl.AUTO_LOGNAME):
    """One program across every available node.

    ranks_per_node is what the layout flag is built from, so the layout the
    launcher is asked for is the layout that actually exists.

    OMP_NUM_THREADS is set for the ranks: without it a threaded program starts
    one thread per core in every rank and they contend. 1 is right when all
    the parallelism is in the ranks; raise it only for a program that is
    genuinely hybrid, and then lower the rank count to match.

    The layout flag spelling is MPI-specific -- take it from the domain skill.
    """
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


def main():
    print(f"{NODES} node(s) x {RANKS_PER_NODE} ranks = {NRANKS}")

    config = Config(
        executors=[
            HighThroughputExecutor(
                label="htex",
                # ONE worker. It runs the launcher; the launcher reaches the
                # other nodes. Parsl puts nothing on them.
                max_workers_per_node=1,
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
            exe="/abs/path/to/binary",
            paramfile="/abs/path/to/params",
            nranks=NRANKS,
            ranks_per_node=RANKS_PER_NODE,
            nthreads=NTHREADS,
            rundir=WORK,
            launcher=LAUNCHER,
            outputs=[File(os.path.join(WORK, "result.dat"))],
        )
        print(sim.result())
    finally:
        parsl.clear()


if __name__ == "__main__":
    main()
```

---

## 4. Multiple nodes, multiple ranks, parallel tasks

More instances than one node can hold. Each instance gets its own nodes, so
the instances cannot contend at all.

Resources: 4 nodes, 4 rank slots each. Four tasks of 4 ranks, one node each.

`taskset` is the wrong tool here: it pins within the node a launcher starts on
and says nothing about which nodes the launcher picks. Give each instance a
**host file** naming only its own nodes instead.

`NODELIST` comes from `get_resources`. A `@python_app` slices it and declares
one `File` per instance; each `@bash_app` takes its own as `inputs`, so Parsl
will not start an instance until its host file exists.

```python
import os

import parsl
from parsl import bash_app, python_app
from parsl.config import Config
from parsl.data_provider.files import File
from parsl.executors import HighThroughputExecutor
from parsl.providers import LocalProvider

WORK = "/app/work/run0"

# From get_resources(). nodelist comes back comma-separated; split it here so
# the file never has to parse anything at run time.
NODELIST = ["node001", "node002", "node003", "node004"]
RANKS_PER_NODE = 4
LAUNCHER = "mpirun"

# From the goal.
NTASKS = 4
NODES_PER_TASK = 1

RANKS_PER_TASK = NODES_PER_TASK * RANKS_PER_NODE


@python_app
def write_hostfiles(nodelist, nodes_per_task, ranks_per_node, outputs=()):
    """One host file per instance, each naming only that instance's nodes.

    One output File per instance rather than one for the lot, so a consumer
    depends on exactly its own file instead of on "the splitting finished".
    """
    import os

    written = []
    for i, out in enumerate(outputs):
        mine = nodelist[i * nodes_per_task:(i + 1) * nodes_per_task]
        if len(mine) < nodes_per_task:
            raise RuntimeError(
                f"instance {i} wanted {nodes_per_task} node(s), "
                f"only {len(mine)} left of {len(nodelist)}"
            )
        os.makedirs(os.path.dirname(out.filepath), exist_ok=True)
        with open(out.filepath, "w") as f:
            for host in mine:
                # "<host> slots=<n>" is one spelling; others want "<host>:<n>".
                # Take the format from the domain skill file.
                f.write(f"{host} slots={ranks_per_node}\n")
        written.append(out.filepath)
    return written


@bash_app
def simulate(exe, paramfile, nranks, rundir, launcher,
             inputs=(), outputs=(),
             stdout=parsl.AUTO_LOGNAME, stderr=parsl.AUTO_LOGNAME):
    """One instance on its own nodes.

    inputs[0] is this instance's host file -- a DataFuture, so Parsl will not
    start this app until write_hostfiles has produced it.

    The host file flag is MPI-specific (--hostfile, -f, -machinefile). Take the
    spelling from the domain skill file.
    """
    hostfile = inputs[0].filepath
    product = outputs[0].filepath
    return (
        f"set -eo pipefail && "
        f"cd {rundir} && "
        f"{launcher} -n {nranks} --hostfile {hostfile} {exe} {paramfile} && "
        f"test -s {product}"
    )


def main():
    if NTASKS * NODES_PER_TASK > len(NODELIST):
        raise SystemExit(
            f"need {NTASKS * NODES_PER_TASK} node(s), have {len(NODELIST)}"
        )

    config = Config(
        executors=[
            HighThroughputExecutor(
                label="htex",
                # One worker per concurrent instance. They all sit together --
                # they only run launchers, and the host files decide where the
                # ranks go.
                max_workers_per_node=NTASKS,
                provider=LocalProvider(init_blocks=1, min_blocks=1, max_blocks=1),
            )
        ],
        run_dir=os.path.join(WORK, "runinfo"),
        strategy="none",
        initialize_logging=False,
    )
    parsl.load(config)
    try:
        hostfiles = [
            File(os.path.join(WORK, f"task{i}", "hostfile"))
            for i in range(NTASKS)
        ]
        split = write_hostfiles(
            NODELIST, NODES_PER_TASK, RANKS_PER_NODE, outputs=hostfiles,
        )

        futures = []
        for i in range(NTASKS):
            rundir = os.path.join(WORK, f"task{i}")
            os.makedirs(rundir, exist_ok=True)
            futures.append(
                simulate(
                    exe="/abs/path/to/binary",
                    paramfile=f"/abs/path/to/params_{i}",
                    nranks=RANKS_PER_TASK,
                    rundir=rundir,
                    launcher=LAUNCHER,
                    inputs=[split.outputs[i]],      # this instance's host file
                    outputs=[File(os.path.join(rundir, "result.dat"))],
                )
            )

        failed = []
        for i, fut in enumerate(futures):
            err = fut.exception()
            if err is None:
                print(f"task{i} ok")
            else:
                failed.append(i)
                print(f"task{i} FAILED: {err}")
        if failed:
            raise SystemExit(f"failed: {failed}")
    finally:
        parsl.clear()


if __name__ == "__main__":
    main()
```

---

# Re-invoking the Workflow File as the Rank Program

Some packages are used as a **library imported by the ranks** rather than as a
binary the launcher starts. The ranks then have to be a Python program — and
the one-file rule means that program is `workflow.py` itself, launched again
with a flag saying which half to run.

**Always add an `--mpi` flag.** It is what lets one file be both the driver and
the rank program, and it is the only reliable way to tell them apart.

```python
import argparse
import os
import sys

import parsl
from parsl import bash_app
from parsl.config import Config
from parsl.data_provider.files import File
from parsl.executors import HighThroughputExecutor
from parsl.providers import LocalProvider

WORK = "/app/work/run0"

# From get_resources().
RANKS_PER_NODE = 8
LAUNCHER = "mpirun"

# From the goal.
NTASKS = 2
RANKS_PER_TASK = 4


@bash_app
def simulate(script, infile, nranks, cores, rundir, launcher, python,
             inputs=(), outputs=(),
             stdout=parsl.AUTO_LOGNAME, stderr=parsl.AUTO_LOGNAME):
    """Launch this same file again, as the rank program.

    --mpi is what the child reads to know it is a rank. The interpreter is
    passed as an absolute path: a bare `python` on a worker's thin PATH is not
    necessarily the one running the driver.
    """
    product = outputs[0].filepath
    return (
        f"set -eo pipefail && "
        f"cd {rundir} && "
        f"taskset -c {cores} {launcher} -n {nranks} "
        f"{python} {script} --mpi --infile {infile} && "
        f"test -s {product}"
    )


def run_rank(args):
    """The rank half.

    Imports here, not at module level: the driver half must not import a
    library that initializes MPI when it is not a rank.
    """
    from somepackage import Engine

    engine = Engine()
    engine.run(args.infile)
    engine.close()


def main():
    config = Config(
        executors=[
            HighThroughputExecutor(
                label="htex",
                max_workers_per_node=NTASKS,
                cpu_affinity="none",
                provider=LocalProvider(init_blocks=1, min_blocks=1, max_blocks=1),
            )
        ],
        run_dir=os.path.join(WORK, "runinfo"),
        strategy="none",
        initialize_logging=False,
    )
    parsl.load(config)
    try:
        script = os.path.abspath(__file__)
        futures = []
        for i in range(NTASKS):
            rundir = os.path.join(WORK, f"task{i}")
            os.makedirs(rundir, exist_ok=True)
            first = i * RANKS_PER_TASK
            futures.append(
                simulate(
                    script=script,
                    infile=f"/app/data/input_{i}",
                    nranks=RANKS_PER_TASK,
                    cores=f"{first}-{first + RANKS_PER_TASK - 1}",
                    rundir=rundir,
                    launcher=LAUNCHER,
                    python=sys.executable,
                    outputs=[File(os.path.join(rundir, "result.dat"))],
                )
            )

        failed = []
        for i, fut in enumerate(futures):
            err = fut.exception()
            if err is None:
                print(f"task{i} ok")
            else:
                failed.append(i)
                print(f"task{i} FAILED: {err}")
        if failed:
            raise SystemExit(f"failed: {failed}")
    finally:
        parsl.clear()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mpi", action="store_true")
    parser.add_argument("--infile")
    args, _ = parser.parse_known_args()

    if args.mpi:
        if not args.infile:
            raise SystemExit("--mpi requires --infile")
        run_rank(args)
    else:
        main()
```

Four things this pattern depends on:

**The flag, not the environment.** Do not try to infer "am I a rank?" by
looking for variables a launcher sets. They appear in processes that were
never launched by one — a driver that imports an MPI-linked library
initializes MPI itself and sets them — and they can be missing in processes
that were. A flag you pass is the only thing that cannot be wrong. Launchers
pass argv straight through to every rank, so nothing has to be forwarded.

**`parse_known_args`, not `parse_args`.** Some launchers append arguments of
their own, and strict parsing makes the rank die on an unrecognized flag.

**Rank-only imports stay inside `run_rank`.** A module-level import of the
MPI-linked library runs in the driver too, which makes the driver an MPI
process and the launcher a nested one — refused by some MPIs, broken by
others.

**Without the flag check the file forks itself forever.** The child re-enters
`main()`, loads Parsl and launches more copies. Check the flag first in
`__main__`, before anything else.

---

# Configuring Parsl

## The driver never asks for resources

The resources already exist when the driver starts; `get_resources` is how you
find out what they are, and the numbers go into the file as constants. Use
`LocalProvider` with one fixed block and let the launcher inside a
`@bash_app` reach any other nodes itself.

```python
provider=LocalProvider(init_blocks=1, min_blocks=1, max_blocks=1)
```

Fixed topology: `strategy="none"` plus equal `min`/`max`/`init` blocks means
no scaling in or out, which is what a run of known shape wants. Do not use a
scheduler provider in a generated workflow, and do not write a submit script.

## `max_workers_per_node` is the concurrency knob

It is the number of apps that may run at once. Three situations, three
answers:

| Situation | Value |
|---|---|
| One app that launches its own ranks | `1` |
| K launcher apps running at once | `K`, plus disjoint cores (one node) or host files (many) |
| K independent single-process chains | the DAG's peak concurrency, not necessarily K |

For the third, count the DAG's real peak: a stage that fans out to two
consumers of the same future makes one chain want two slots at once. Running
under the peak only queues, but a cheap deliverable queueing behind an
expensive one is the usual reason to raise it.

**Size from a variable.** When the instance count comes from the goal,
`max_workers_per_node` comes from the same variable. A hardcoded `2` silently
serializes a ten-instance run.

## `cores_per_worker` is not a reservation

It is a **divisor** for the worker count, not cores handed to an app. On 128
visible cores `cores_per_worker=8` gives 16 workers. Set above the visible
core count it gives **zero** workers and the DAG hangs with no error. To size
concurrency use `max_workers_per_node`; to give an app a core range use
`taskset` in the command string.

Set `cpu_affinity="none"` when `taskset` or the launcher does the pinning, so
Parsl's own affinity does not fight it.

## Keep the executor process-based

Do not swap `HighThroughputExecutor` for `ThreadPoolExecutor` to save startup
cost when apps touch **global library state**. A library with a module-level
scene, context or registry — most visualization packages — cannot have two
instances manipulating it in one interpreter. Separate worker processes keep
concurrent instances independent; threads share the interpreter and corrupt
each other. Threads are fine for apps that are pure functions over their
arguments.

## Pin `run_dir`

```python
run_dir=os.path.join(WORK, "runinfo")
```

Left at its default, the per-app logs land in `runinfo/` relative to the
process CWD — a different tree from the server's, so the first place to look
after a failure is then missing.

## `initialize_logging=False`

The default is `True`, which writes a DEBUG-level `parsl.log` including the
scaling strategy every 5 seconds — thousands of lines of `more slots than
tasks` in a long run. That belongs to development, not to a run whose output
is a scientific artifact.

**Turn it back on for one run when a DAG hangs or fails in a way the result
does not explain.** `parsl.log` is the only record of task submission and
dependency resolution: whether an app was ever submitted, which executor it
went to, what it was waiting on, and the full traceback of a failed app body.
Remove the line, reproduce, read the log, put the line back.

A middle setting keeps the submission trail and every failure while dropping
the polling noise:

```python
import logging
parsl.load(config)
logging.getLogger("parsl").setLevel(logging.INFO)
```

## Lifecycle

```python
parsl.load(config)          # once, at the top of main()
...                         # submit every app
final.result()              # resolve
parsl.clear()               # in a finally, to release the workers
```

`parsl.load()` twice without `parsl.clear()` raises `NoDataFlowKernelError`.

## The full skeleton

```python
config = Config(
    executors=[
        HighThroughputExecutor(
            label="htex",
            max_workers_per_node=NTASKS,     # same variable as the DAG
            cpu_affinity="none",             # when taskset does the pinning
            provider=LocalProvider(init_blocks=1, min_blocks=1, max_blocks=1),
        )
    ],
    run_dir=os.path.join(WORK, "runinfo"),
    strategy="none",
    initialize_logging=False,
)
```

Do not add keyword arguments beyond these — an unrecognized one is a
`TypeError` at load.

---

# Files Between Apps

## Declare products, do not name them in strings

A path passed from one app to another as a plain string is invisible to the
DataFlowKernel: it cannot order the apps, cannot stage the file, and cannot
tell you that the producer never wrote it. Declare it instead.

**Producer** declares what it writes:

```python
sim = simulate(..., outputs=[File("/app/work/run0/result.dat")])
```

Inside the app body, read the path off the `File` object, so the command
writes exactly what Parsl is tracking:

```python
@bash_app
def simulate(exe, rundir, outputs=()):
    product = outputs[0].filepath
    return f"cd {rundir} && {exe} > {product}"
```

**Consumer** depends on it:

```python
analyze(inputs=sim.outputs)
```

```python
@python_app
def analyze(inputs=()):
    product = inputs[0].filepath      # guaranteed to exist when this starts
```

`sim.outputs` is a list of `DataFuture`. Passing it as `inputs` is what creates
the edge, and it is why the consumer needs no `os.path.exists` check — a
hand-written existence check is a symptom of string wiring.

## Several products, several consumers

One output per consumer lets each depend on only its own file:

```python
hostfiles = [File(f"{WORK}/task{i}/hostfile") for i in range(NTASKS)]
split = write_hostfiles(..., outputs=hostfiles)

for i in range(NTASKS):
    simulate(..., inputs=[split.outputs[i]])
```

Depending on `split.outputs` as a whole would make every instance wait for
every file, which is correct but needlessly serial.

## Passing values, not files

A `@python_app`'s return value is an `AppFuture`. Pass the future itself as an
argument and Parsl resolves it before the consumer runs:

```python
info = select_params(...)              # returns a dict
sim = simulate(..., info=info)         # body receives the dict, not the future
```

The value must be picklable. Return a small dict of numbers and strings, not
an open file handle or a library object.

## Build the DAG for N, resolve once

```python
def build_instance(i, ranks):
    ...                       # this instance's apps, wired to each other
    return terminal_future     # module-level helper, called from main()

finals = [build_instance(i, RANKS_PER_TASK) for i in range(NTASKS)]
for f in finals:
    print(f.result())
```

Everything that differs between instances — its directory, its input file, its
core range, its host file — is derived from the index inside that function.
Resolving in a loop at the end still submits everything up front, so the
instances overlap; the loop only fixes the printing order.

Where the instance count exceeds what can run at once, keep the DAG the same
and let the worker count throttle it. Parsl queues the excess. Do not
hand-write batches unless the goal asks for it.

---

# Common Pitfalls

| Pitfall | Rule |
|---|---|
| Looking for a tool that runs a code or command string | There isn't one. Use `write_file` + `run_workflow` |
| Splitting a workflow across multiple generated files | One file per workflow; multiple apps inside it |
| Writing a shell or submit script to run the work | Never. The command belongs in a `@bash_app` string |
| `import subprocess` / `os.system` in the generated file | Takes the work out of Parsl — express it as a `@bash_app` |
| A launcher call in the driver body instead of inside a `@bash_app` | Parsl never sees the launch — move it into the app body |
| A launcher name in a docstring or module string | Scanned as a string constant and rejected outside a `@bash_app`. Put it in a `#` comment |
| The workflow file discovering its own resources | It cannot see them reliably and does not need to. Call `get_resources` first and write the numbers in as constants |
| Imports at module level used inside an app body | `NameError` on the worker — it never imported your file. Import inside the body |
| A module-level constant read in an app body | Same `NameError`. Pass it as a parameter. A constant in a DEFAULT argument is fine; one read in the body is not |
| A module-level helper called from an app body | Same `NameError`. Inline it or make it its own app, even if that duplicates code |
| Calling `.result()` inside an app | Deadlocks the worker — only call it in `main()` |
| `.result()` between submissions to force ordering | Serializes the driver and hides the DAG. Submit everything, then resolve once |
| Passing file paths between apps as plain strings | Invisible to the DFK. `outputs=[File(p)]` on the producer, `inputs=producer.outputs` on the consumer |
| `os.path.exists` checks on another app's output | A symptom of string wiring — with `File`/`DataFuture` deps Parsl guarantees the file first |
| Debugging the app named in `DependencyError` | That app never ran. Read the chain back to its root and debug the app in the FIRST traceback |
| Trusting a `BashExitFailure` exit code at face value | It is the last command's code, and MPI aborts are truncated mod 256. Read the app's stderr file |
| Omitting `stdout=`/`stderr=` on a `@bash_app` | The output is discarded and a failure has no explanation at all. Pass `parsl.AUTO_LOGNAME` for both |
| Leaving `run_dir` at its default | Per-app logs land relative to the process CWD, a different tree from the server's. Pin it under the work directory |
| Chaining a pipe under plain `set -e` | `set -e` ignores failure on the LEFT of a pipe, so a dead reader still exits 0. Use `set -eo pipefail` |
| Relying on `set -e` to catch an unset variable | It does not. Add `set -u`, or guard the variable and exit with a distinct code |
| `except OSError: pass` around setup inside an app | Hides the real failure and returns the downstream symptom from somewhere unrelated. Setup must fail loudly |
| `cores_per_worker=N` to "size a slot for N ranks" | It is a DIVISOR for the worker count. Above the visible core count it gives ZERO workers and the DAG hangs silently. Use `max_workers_per_node` |
| `max_workers_per_node=1` for K independent chains | They run one at a time however the DAG is wired. Size workers to the DAG's peak concurrency |
| Two or more launcher apps at once without separation | They all claim rank slots from the front of the same list and overlap. Disjoint `taskset` ranges on one node, per-instance host files across nodes |
| `taskset` to separate instances across nodes | It pins within one node and says nothing about which nodes the launcher picks. Use host files |
| A hardcoded instance count or rank count | Both come from the goal and from `get_resources`. Derive the worker count from the same variable |
| `--map-by ppr:N:node` with N != the available ranks per node | "not enough slots available". 1x8 and 2x4 are both 8 ranks and are not interchangeable |
| Instances x ranks greater than the available slots | Check it in `main()` against the constants and fail with the numbers, rather than letting the launcher report it from inside a worker |
| Many workers with `OMP_NUM_THREADS` unset | Each rank starts one thread per core and they contend. Set it explicitly |
| Using whatever launcher is on `PATH` for a program built against another MPI | That launcher belongs to a different MPI. Ranks then run as N separate one-rank jobs and exit 0. Use the launcher `get_resources` and the domain skill name, and verify the rank count from the program's own output |
| Inferring "am I a rank?" from launcher-set environment variables | They appear in processes never launched by a launcher and can be missing in ones that were. Use an explicit `--mpi` flag |
| Re-invoking the file without a flag guard | The child re-enters `main()` and forks again, without limit. Check the flag first in `__main__` |
| A module-level import of an MPI-linked library in a re-invoking file | The driver becomes an MPI process and the launcher a nested one. Import inside the rank function |
| `parse_args` in a file a launcher re-invokes | Some launchers append arguments of their own. Use `parse_known_args` |
| A `@python_app` that assigns a library-path variable then imports the library | No effect — the loader fixed its search path when the worker started. Use a `@bash_app` that exports then launches |
| `ThreadPoolExecutor` for apps that touch global library state | They share one interpreter and corrupt each other. Use process-based HTEX |
| Leaving a library's global state registered after an app fails | The next task on that worker inherits it. Unregister in a `finally` |
| One app producing both a cheap and an expensive deliverable | A failure in the expensive half takes the cheap one with it. Split them |
| Rendering without `matplotlib.use("Agg")` | Headless nodes have no display; set the backend inside the `@python_app` |
| `rm -rf` on a directory the app also reads or writes | A retry wipes the previous attempt's output before you can read it. `mkdir -p`, and remove only the subdirectory being replaced |
| Simulation app invoked more than once per run | Call it exactly once per workflow invocation — re-invoking on replan silently produces duplicate runs instead of reusing the result |
| Extra kwargs in `HighThroughputExecutor` | `TypeError` at load — copy the config skeleton exactly |
| `strategy` not set to `"none"` | Auto-scaling fights a fixed set of resources |
| Leaving `initialize_logging` at its default | Writes a DEBUG `parsl.log` with 5-second scaling chatter into the run directory. Set `False`; turn it on only to diagnose a hang |
| `parsl.load()` twice without `parsl.clear()` | Raises `NoDataFlowKernelError` |
| `WorkerLost` | The worker process crashed — read stderr for the actual exception |