---
name: systems/wilkins
description: >
  Writing a Wilkins in situ workflow for MAW: the YAML config IS the workflow,
  the LowFive check that must come first, the four rank layouts (one/many nodes
  x serial/parallel), rank accounting, the task contracts, and the pitfalls
  table. Load for every --engine wilkins run; the config manual is
  knowledge/WILKINS.
---

# Wilkins — System Skill

Wilkins couples tasks in situ. Tasks keep making ordinary HDF5 calls and
LowFive, an HDF5 Virtual Object Layer plugin, redirects the data over MPI
instead of to disk. A Wilkins workflow is therefore not a program that calls a
workflow API: it is a **YAML file** naming tasks and the datasets they produce
and consume, run by Wilkins' Python driver. Load for every `--engine wilkins`
run; the full schema is in `knowledge/WILKINS`.

**This run's engine is Wilkins. Do not import, use or reference Parsl,
PyCOMPSs or ADIOS2.**

Deliverables: the YAML (written with `write_config`) plus one program per task.
The task programs contain **no Wilkins and no LowFive calls** -- plain HDF5 or
plain `h5py`.

# Rules

## Call `check_lowfive` FIRST

LowFive is switched on by two environment variables, not by the config. If
`HDF5_VOL_CONNECTOR` is unset, nothing in Wilkins checks it: HDF5 uses its
native VOL, every task writes a real `.h5` file, and the workflow **completes
successfully having transported nothing in memory, with no error anywhere**.
That is the quietest failure Wilkins has, so it is checked before anything is
written, not observed afterwards.

| Variable | Value |
|---|---|
| `HDF5_VOL_CONNECTOR` | `lowfive under_vol=0;under_info={};` -- quote it in a shell, the unquoted form breaks at the `;` |
| `HDF5_PLUGIN_PATH` | the directory holding `liblowfive.*` |

`spack load wilkins` sets both. If `HDF5_PLUGIN_PATH` is unset the driver dies
with a `KeyError`; if it holds no `liblowfive.*` it raises `RuntimeError: Bad
HDF5_PLUGIN_PATH, lowfive library not found`. The server also refuses to launch
when `check_lowfive` would report a problem, so a failure here is an
environment gap, not something the config can fix.

## Main tools

| Tool | What it does |
|---|---|
| `check_lowfive()` | Reports whether LowFive is the active HDF5 VOL, with evidence. Call it first |
| `write_config(content, filename)` | Writes the YAML, parses it, returns task count, `total_ranks` and warnings. The file IS the workflow |
| `run_workflow(filename, ranks=0)` | Launches the driver on that config. `ranks=0` makes the server compute the total |

There is no tool that runs a code string. The task programs are ordinary files
you write beside the config.

## The config

- The config filename must end in `yaml` -- `wilkins.yaml`. A `.yml` name is
  rejected by the driver (`write_config` will still write it; it will not run).
- The only top-level key is `tasks`. Each task REQUIRES `func` and `nprocs`;
  omit either and the parser throws and the run exits.
- `args` is a list of **strings**. Quote numbers: `args: ["100", "0.5"]`.
- `start_proc` and `cmdline` appear in shipped examples but are **not parsed**.
  Never write them -- ranks are assigned by declaration order (see below).
- `ownership` and `io_proc` are not YAML keys. Do not invent keys; the schema
  is in `knowledge/WILKINS` and is closed.
- Per dataset: `metadata` defaults to **1** (in memory over MPI), `passthru`
  defaults to **0** (write a real file). **Both 0 is a hard error.** Omitting
  both means in-memory, which is what you want almost always.
- Ports are matched into channels by `filename` + `"/"` + dataset `name`, with
  **globs** (`*`, `?`), not regexes. You never declare edges. Write the
  **identical** filename and dataset strings on both sides of a pair: the
  matcher is order-dependent when one side is a glob and the other a literal.
- Set the SAME mode on both sides. On a mismatch the **producer wins** and the
  consumer is silently coerced, with only a warning on stderr.
- An inport nothing produces: with `metadata: 1` it is a hard error naming the
  port; with `passthru: 1` it is NOT an error -- it quietly becomes plain file
  I/O and the consumer reads from disk (or finds nothing).

## Task programs

Dispatch is by the suffix of `func`.

| `func` | What it must be |
|---|---|
| `./task.py` | A Python module the driver **imports**. It must define `main()`; it is called as `main(task_args)` and, on `TypeError`, as `main()`. With no `main` the task **silently does nothing** |
| `./name.hx` | A C/C++/Fortran program built as a Henson-linked, position-independent shared object (`PREFIX ""`, suffix `.hx`, `-pie -Wl,--export-dynamic -Wl,-u,henson_set_contexts,-u,henson_set_namemap`). A **plain MPI binary cannot be this kind of task** |

The Henson rule applies to the second row only. A Python task needs no Henson
link and no build step -- prefer Python tasks unless the domain gives you a
compiled `.hx` already.

A consumer's `main` is called **repeatedly**, once per round of data, until the
producers are done. It must be re-entrant: open the file, read, close it, keep
no state in open handles. A pure producer's `main` is called once and writes
every step itself.

A task opens its HDF5 files with plain `h5py.File("name.h5", "r")` /
`"w"`. A parallel task's LowFive communicator is its **task-local**
communicator, not `COMM_WORLD`; do not run collectives over `COMM_WORLD`
inside a task, because ranks of other tasks are in it.

## Ranks and placement, in one paragraph

`nprocs` is that task's MPI rank count. Wilkins starts every task inside ONE
MPI job: ranks are contiguous blocks assigned in **YAML declaration order**
(the first task gets ranks `0 .. n0-1`, the next gets the following `n1`, and
so on), so **reordering tasks moves them**. `nwriters: K` limits LowFive
participation to the first K ranks of a task. There are **no node-placement
controls in the config** -- no host, no node count, no pinning. Which node a
rank lands on is the launcher's job, and `run_workflow` does not pass
launcher placement flags.

# Rank layouts

All four layouts use the same two Python files, written once and shared:

```python
# ./sim.py -- producer: plain h5py, no Wilkins calls, main() defined
import h5py
import numpy as np

def main(task_args=None):
    steps = int(task_args[0]) if task_args else 1
    for step in range(steps):
        with h5py.File("outfile.h5", "w") as f:
            f.create_dataset("/group1/grid", data=np.full((8, 8), float(step)))
```

```python
# ./ana.py -- consumer: re-entrant, called once per file the producer closes
import h5py

def main(task_args=None):
    with h5py.File("outfile.h5", "r") as f:
        grid = f["/group1/grid"][:]
    print("mean", float(grid.mean()))
```

The shared `outfile.h5` name is the channel; the consumer's dataset path must
equal the producer's.

## 1. One node, serial (one rank per task)

One producer and one consumer, one rank each. Total = 1 + 1 = **2**.

```yaml
tasks:
  - func: "./sim.py"
    nprocs: 1
    args: ["5"]
    outports:
      - filename: "outfile.h5"
        dsets:
          - name: /group1/grid
            metadata: 1
            passthru: 0
  - func: "./ana.py"
    nprocs: 1
    inports:
      - filename: "outfile.h5"
        dsets:
          - name: /group1/grid
            metadata: 1
            passthru: 0
```

`run_workflow(filename="wilkins.yaml", ranks=0)` runs
`mpirun -n 2 -l <python> -u wilkins-master.py wilkins.yaml`.

## 2. One node, many ranks, parallel

Raise `nprocs` so the task ranks sum to the node's rank slots. Resources: 1
node, 8 slots; producer 6 ranks, consumer 2. Total = 6 + 2 = **8**.

`nwriters: K` has only the first K ranks of the producer join LowFive; the
other ranks still run the task code but are not LowFive participants. What
their own HDF5 calls do is not documented, and the shipped example that uses
`nwriters` is a compiled task, so treat it as an advanced option. Use it when a
wide simulation should couple through a few ranks, and set it **no larger than
`nprocs`**. Leave it out and every rank participates (default `-1` = all).

```yaml
tasks:
  - func: "./sim.py"
    nprocs: 6
    nwriters: 2
    args: ["5"]
    outports:
      - filename: "outfile.h5"
        dsets:
          - name: /group1/grid
  - func: "./ana.py"
    nprocs: 2
    inports:
      - filename: "outfile.h5"
        dsets:
          - name: /group1/grid
```

With `nprocs > 1` each rank of the task runs the same `main`. Make ranks write
disjoint parts of the data, or have the task write only on its rank 0 -- a
Python task is not handed its local communicator, so do not guess one. If the
task needs rank-aware decomposition, use a compiled `.hx` task: the shipped
compiled examples call `H5Pset_fapl_mpio` on the task-local communicator.

## 3. Many nodes, serial

Serial tasks across several nodes: for example a four-stage chain, one rank per
task, on 2 nodes with 2 slots each. **Nothing in the YAML changes from layout
1 except the task list and therefore the total.** Ranks are contiguous blocks
over one `MPI_COMM_WORLD`; the launcher spreads them over nodes. Total =
1 + 1 + 1 + 1 = **4**.

```yaml
tasks:
  - func: "./sim.py"
    nprocs: 1
    outports:
      - filename: "stage0.h5"
        dsets: [{name: /group1/grid}]
  - func: "./filter.py"
    nprocs: 1
    inports:
      - filename: "stage0.h5"
        dsets: [{name: /group1/grid}]
    outports:
      - filename: "stage1.h5"
        dsets: [{name: /group1/grid}]
  - func: "./reduce.py"
    nprocs: 1
    inports:
      - filename: "stage1.h5"
        dsets: [{name: /group1/grid}]
    outports:
      - filename: "stage2.h5"
        dsets: [{name: /group1/grid}]
  - func: "./ana.py"
    nprocs: 1
    inports:
      - filename: "stage2.h5"
        dsets: [{name: /group1/grid}]
```

Every stage reads and writes its OWN distinct filename, so each pair is its
own channel. A middle task is consumer and producer at once.

## 4. Many nodes, many ranks, parallel

Same shape as layout 2 with bigger numbers. Resources: 2 nodes, 8 slots each;
producer 12 ranks (`nwriters: 4`), consumer 4. Total = 12 + 4 = **16**.

```yaml
tasks:
  - func: "./sim.py"
    nprocs: 12
    nwriters: 4
    args: ["5"]
    outports:
      - filename: "outfile.h5"
        dsets:
          - name: /group1/grid
  - func: "./ana.py"
    nprocs: 4
    inports:
      - filename: "outfile.h5"
        dsets:
          - name: /group1/grid
```

**Layouts 3 and 4 change only the totals.** There is no node count, host list
or placement field to set in the config, and the server's launch command
carries no placement flags: the job's allocation and the launcher's own
defaults decide where ranks go. Size `nprocs` so the total fits the slots the
allocation has, and do not try to express nodes in YAML.

# Rank accounting -- the one number to get right

The launcher's `-n` is **the sum over tasks of `taskCount` x `nprocs`**
(`taskCount` defaults to 1).

| Config | Total |
|---|---|
| sim 6, ana 2 | 8 |
| sim 12, ana 4 | 16 |
| sim `taskCount: 2` x 3 ranks, ana 2 | 2 x 3 + 2 = 8 |

Too few ranks does not run a smaller workflow: the tasks at the tail of the
declaration order get no ranks and never run, and the driver prints `Wilkins
error: Group sizes of producer, consumer, and dataflow exceed total size of
world communicator`. Extra ranks idle.

**Check the server's number.** `write_config` reports `total_ranks`, but the
server sums only `nprocs` and defaults a missing `nprocs` to 1. It does not
multiply by `taskCount`. For any config with `taskCount > 1` compute the total
yourself from the rule above. Prefer `taskCount: 1` (omit it) with Python
tasks: see Pitfalls.

# Practices

## State every number

Write `nprocs` on every task, even `1`. It is required, and it is the quantity
the rank total is built from. Take the ranks available from the environment
(the resource tools or the domain skill) and keep the total at or under it.

## Prefer in-memory, one channel per pair

| Do | Why |
|---|---|
| Leave `metadata`/`passthru` at the defaults | `metadata: 1` is in situ transport; `passthru: 1` writes to disk, which is what in situ coupling exists to avoid |
| Give each producer/consumer pair its own filename | The match IS the channel; a glob that catches two producers couples the wrong pair |
| Repeat the same filename and dataset strings on both sides | Avoids the order-dependent glob-versus-literal match |
| Name datasets exactly (`/group1/grid`) | `name: "*"` couples every dataset in the file and hides a typo |
| Have the consumer read the filename the producer wrote | The name inside the task code must equal the YAML `filename` (or its glob) |

## Keep the task programs plain

- Never call LowFive, `pywilkins` or any Wilkins API from a task. The
  `pywilkins` module is for the MPMD form, which this run does not use.
- Close every file you open. LowFive serves a file to the consumer when the
  producer closes it, so an unclosed output stalls the consumer.
- A producer that writes several steps can reuse one name (`outfile.h5`)
  or use one name per step; the consumer's `main` then runs once per file.

## Throttle a slow consumer with `io_freq`

`io_freq` lives on an **inport** only. `io_freq: N` serves that consumer every
Nth round of the producer; `io_freq: latest` serves only when the consumer has
asked, so a slow consumer sees the newest data rather than every round.
Without it a consumer slower than the producer throttles the producer.
`io_freq` is not read on an outport.

```yaml
    inports:
      - filename: "outfile.h5"
        io_freq: 2
        dsets: [{name: /group1/grid}]
```

## Debugging a stuck or silent run

Driver flags go AFTER the config path in the launch command: `-v 1` logs at
info, `-v 2` at debug. Output lines are rank-labelled by `-l`, which is how a
message is attributed to a task. A hung workflow is almost always an inport
no outport matches, or a `.py` task with no `main`. Those are the first two
things to check: re-read the declared filenames and dataset names against the
task code, then confirm each `.py` task defines `main`.

# Authoring order

1. `check_lowfive()`. Stop and report if `lowfive_active` is false.
2. Decide the tasks and which dataset each produces. Write one filename per
   producer/consumer pair and one dataset path per array.
3. Write each task program as a `.py` module with `main()`, using the literal
   filename and dataset path from step 2.
4. Write the YAML with `write_config`. Read the returned `warnings` and fix
   each, remembering the server's `total_ranks` ignores `taskCount`.
5. Work out the rank total yourself: the sum of `taskCount x nprocs`.
6. `run_workflow(filename="wilkins.yaml", ranks=0)`.
7. Verify in-memory transport happened: the channel files (`outfile.h5`) must
   NOT appear on disk, and each consumer must have printed its data.

## One producer, two consumers

A producer's file can feed several consumers; each consumer lists the same
`filename` and the dataset it wants. Declare every consumer's `nprocs` in the
total. Consumers reading different datasets from one file need no coordination:

```yaml
tasks:
  - func: "./sim.py"
    nprocs: 2
    outports:
      - filename: "outfile.h5"
        dsets:
          - name: /group1/grid
          - name: /group1/particles
  - func: "./grid_ana.py"
    nprocs: 1
    inports:
      - filename: "outfile.h5"
        dsets: [{name: /group1/grid}]
  - func: "./part_ana.py"
    nprocs: 1
    inports:
      - filename: "outfile.h5"
        io_freq: 2
        dsets: [{name: /group1/particles}]
```

Total = 2 + 1 + 1 = **4**. Each consumer gets its own `io_freq`.

# Common Pitfalls

| Pitfall | Rule |
|---|---|
| Skipping `check_lowfive` | With `HDF5_VOL_CONNECTOR` unset the run still succeeds, writes real `.h5` files and transports nothing. Call it first, every time |
| Writing a config named `*.yml` | Rejected. The name must end in `yaml` |
| A task missing `func` or `nprocs` | Both are required and unguarded. The parser throws `YAML parser exception: ...` and the run exits |
| Writing `start_proc:` or `cmdline:` | Dead keys, silently ignored. Placement comes from declaration order; do not copy them from examples |
| Inventing a key (`ownership`, `io_proc`, `nodes`, `host`) | Not in the schema. Use only the keys in `knowledge/WILKINS` |
| Unquoted numbers in `args` | `args` is a list of strings. `args: ["100"]`, not `[100]` |
| A `.py` task with no `main()` | Silently does nothing, and consumers then wait forever. Define `main()` |
| A consumer `main` that keeps state between calls | It is called repeatedly, once per data round. Make it re-entrant |
| `.hx`/plain MPI binary as a task | A plain MPI binary cannot be a task. Use a Python task, or a Henson-linked PIE shared object |
| `metadata: 0` and `passthru: 0` on one dataset | Hard error: `Either metadata or passthru must be enabled. Both cannot be disabled.` |
| Different modes on the two sides of a pair | The producer wins and the consumer is silently coerced, with a warning only. Match them |
| `metadata: 1, passthru: 1` expecting both | Parses, but memory wins and passthru is ignored. Choose one |
| An inport with `metadata: 1` and no producer | Hard error naming the port. Fix the filename or dataset name |
| An inport with `passthru: 1` and no producer | NOT an error: it degrades to plain file I/O. Do not rely on it to prove the match |
| Regex in `filename` or `name` | They are globs: `*` and `?` only |
| One side a glob, the other a literal | The pairing depends on which task is declared first. Use identical strings both sides |
| `io_freq` on an outport | Only inports take it. Values: a positive integer, or `latest` |
| Launching with fewer ranks than the sum | The tail tasks starve and never run. Use `ranks=0` |
| Trusting `total_ranks` with `taskCount > 1` | The server does not multiply by `taskCount`. Compute `taskCount x nprocs` per task yourself |
| `taskCount > 1` on a Python task | The driver switches to Henson `./<func>-inst<N>.hx` for that task and every later one. Keep `taskCount` out of Python workflows |
| `taskCount > 1` with a dotless filename | The port filename is split at its FIRST dot; with no dot the config load crashes. Use `name.h5` |
| Reordering tasks after sizing them | Ranks follow declaration order, so reordering moves every task's ranks |
| `nwriters` larger than `nprocs` | Meaningless. It is the number of leading ranks that take part, at most `nprocs` |
| Collectives over `COMM_WORLD` inside a task | A task's communicator is task-local. Other tasks' ranks are in `COMM_WORLD` and the collective hangs |
| Trying to pin a task to a node | No such control exists in the config. Layouts 3 and 4 differ from 1 and 2 only in the totals |
| Leaving a producer's output file open | LowFive serves on close. Close it, or the consumer never gets data |
| Passing driver flags before the config path | They go after it: `... wilkins.yaml -v 1` |
| Reading the output directory for results | In memory mode no `.h5` is written. If one appears, LowFive was not active |
