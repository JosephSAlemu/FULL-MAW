---
name: systems/adios
description: >
  Writing an ADIOS2 pipeline for MAW: which of the two server variants is
  running (serial SST or MPI MPMD), the one-file-PER-STAGE contract, the four
  rank layouts, the Stream calls actually used, and the pitfalls table. Load for
  every --engine adios run; the API manual is knowledge/ADIOS.
---

# ADIOS2 — System Skill

ADIOS2 is an I/O and transport library, not a scheduler. It has no task
decorator and launches nothing. A pipeline is several PROGRAMS, one stage file
each, that exchange arrays through ADIOS2 streams while they are alive at the
same time. Load for every `--engine adios` run; the API manual is
`knowledge/ADIOS`.

**This run's engine is ADIOS2. Do not import, use or reference Parsl, PyCOMPSs
or Wilkins.**

# Which variant is running

`--engine adios` starts ONE of two server classes, chosen by `MAW_ADIOS`. They
need DIFFERENT tools. **Determine which you are on before writing anything.**

| | `serial` (THE DEFAULT) | `mpi` |
|---|---|---|
| Server class | `AdiosSstServer` | `AdiosMpmdServer` |
| Engine-only tools | `run_pipeline`, `write_bp`, `read_bp` | `run_mpmd`, `write_stage` |
| Write a stage with | `write_file` | `write_stage` (names it `stage_<name>.py`) |
| Concurrency | separate OS processes, started together | rank blocks of ONE `mpirun` |
| mpi4py in a stage | FORBIDDEN; `comm=` raises | REQUIRED |
| Collective parallel write | no | yes |
| External compiled binary as a stage | no | yes, via `exe` |
| `ranks` on `run_workflow` | ignored | `mpirun -n <ranks>`, every rank runs the same file |

**Serial is the default.** An unset or unrecognised `MAW_ADIOS` starts the serial
server, so assume serial unless `get_resources` says otherwise.

**Detect it:** call `get_resources` and read `adios2_build`.

| `adios2_build` | Variant |
|---|---|
| `serial (PyPI wheel)` (also says `mpi: none`) | serial: `run_pipeline` |
| `MPI (+mpi +python +sst)` (also gives `launcher`, `color_var`) | mpi: `run_mpmd` |

The tool list agrees. **Never reach for a tool the other variant owns** -- it is not registered here.
The sections below say which variant each statement applies to; the Rules and
Stream API apply to both.

# Rules

## Main tools (both variants)

| Tool | What it does |
|---|---|
| `write_file(path, content)` | Writes a file under `/app/work/run0/`: stage files (serial), inputs, anything a stage reads. A syntax error is rejected; structural problems come back as `warnings` on a file that WAS written |
| `run_workflow(filename)` | Runs ONE file to completion. For a single standalone program only; never for stages that must stream to each other |
| `get_resources`, `check_package(pkg)` | Call both before writing; see "Performance and hygiene" |

## ONE FILE PER STAGE, in both variants

A pipeline is NOT one file with functions called in order. Each stage (simulate,
analyze, render, ...) is its own file and its own process. Serial: write each
with `write_file`, launch with `run_pipeline`. MPI: write each with
`write_stage`, launch with `run_mpmd`. The only data that crosses between stages
is what goes through ADIOS2.

## Data between stages is a real round trip

The producing stage does `stream.write(...)`; the consuming stage does
`stream.read(...)` on the same stream name in ITS OWN process. Reusing an
in-memory copy, a pickle or a `.npy` is a pipeline that never used the engine,
and it is the failure this skill most exists to prevent. Both variants warn on a
file with no `import adios2` or no `.write(` / `.read(`; treat every warning as
a defect.

Human-facing output (PNG, summary text) stays a plain file. Streams carry the
numerical arrays between stages.

## Select the SST engine in EVERY stage

The `Stream(path, mode)` shortcut opens a BP file. With BP a reader opens a file
the writer has not closed and silently reads whatever is there: no error, no
hang, wrong data. Build an `IO`, call `set_engine("SST")`, and pass it to
`Stream` (see "Stream API"). Both ends must select SST and use the identical
stream name.

## Stages must be alive AT THE SAME TIME

SST is a live rendezvous, so the writer and its readers must run concurrently.
That is what `run_pipeline` / `run_mpmd` are for. **`run_workflow` on a lone
writer does not even fail in the serial build**: it completes, leaves the
`<name>.sst` directory behind, and a later reader picks that up as though it had
been streamed live. The pipeline looks like it worked and nothing was streamed.
The server clears stale `*.sst` before every launch, but that cannot save a
pipeline that was never concurrent.

## Both launchers refuse a single stage

`run_pipeline` needs at least two stages and `run_mpmd` at least two blocks.
With one, nothing streams to anything: use `run_workflow`, or add the consumer.

## Never type a launch yourself

No `subprocess` to start another stage, no `mpirun` in your code, no bash or PBS
script. The server builds the launch.

## Domain detail comes from the domain skill

The simulation binary, its parameters, its output format and the variable names
to stream: take them from the domain skill. This skill only wires stages.

# 1. One node, one rank, two stages

Resources: 1 node, serial build. A producer and a consumer, started together.

## Serial variant

```python
# sim.py  (write_file "sim.py")
import numpy as np
import adios2

STREAM = "/app/work/run0/field_stream"   # identical in the reader

def main():
    ad = adios2.Adios()
    io = ad.declare_io("sim")
    io.set_engine("SST")                 # a stream, not a file
    with adios2.Stream(io, STREAM, "w") as s:   # opened ONCE, outside the loop
        for step in range(5):
            u = np.full((64, 64), float(step), dtype=np.float64)
            s.begin_step()
            s.write("U", u, u.shape, [0, 0], u.shape)
            s.write_attribute("step_id", step)
            s.end_step()

main()
```

```python
# analyze.py  (write_file "analyze.py")
import numpy as np
import adios2
import matplotlib
matplotlib.use("Agg")                    # headless node
import matplotlib.pyplot as plt

STREAM = "/app/work/run0/field_stream"

def main():
    ad = adios2.Adios()
    io = ad.declare_io("ana")
    io.set_engine("SST")
    means = []
    with adios2.Stream(io, STREAM, "r") as s:
        for _ in s.steps():              # ends when the writer closes
            u = s.read("U")              # the real read, from the stream
            means.append(float(np.mean(u)))
    plt.plot(means)
    plt.savefig("/app/work/run0/means.png", dpi=150)   # deliverable: plain file
    print("steps seen:", len(means))

main()
```

Launch (serial only):

```python
run_pipeline(stages=[{"file": "sim.py"},
                     {"file": "analyze.py", "delay": 2}])
```

`steps seen: 5` in the reader's stdout is the evidence the stream carried data.

## MPI variant

One rank per block still goes through `run_mpmd`, and a stage still splits
`COMM_WORLD` on the server's colour. Resources: 1 node, 2 rank slots.

```python
# stage_sim.py  (write_stage stage="sim")
import os
import numpy as np
import adios2
from mpi4py import MPI

STREAM = "/app/work/run0/field_stream"

color = int(os.environ["ADIOS2_STAGE_COLOR"])        # the server assigns it
world = MPI.COMM_WORLD
comm = world.Split(color, world.Get_rank())          # this stage's ranks only

ad = adios2.Adios(comm)
io = ad.declare_io("sim")
io.set_engine("SST")
with adios2.Stream(io, STREAM, "w", comm) as s:
    for step in range(5):
        u = np.full((64, 64), float(step), dtype=np.float64)
        s.begin_step()
        s.write("U", u, u.shape, [0, 0], u.shape)
        s.end_step()
```

`stage_analyze.py` is the mirror: the same three lines for `color`/`comm`,
`Adios(comm)`, `set_engine("SST")`, then
`adios2.Stream(io, STREAM, "r", comm)` and `for _ in s.steps(): s.read("U")`.

```python
run_mpmd(blocks=[{"ranks": 1, "file": "stage_sim.py"},
                 {"ranks": 1, "file": "stage_analyze.py"}])
```

which runs `mpirun -n 1 env ADIOS2_STAGE_COLOR=1 <python> stage_sim.py : -n 1
env ADIOS2_STAGE_COLOR=2 <python> stage_analyze.py`.

# 2. One node, more concurrent stages

## Serial variant

Concurrency is more stage files in one `run_pipeline` call, each a separate
process. Two shapes.

A chain, every hop a separate stream with its own name:

```python
run_pipeline(stages=[
    {"file": "sim.py"},                        # writes  chain_a
    {"file": "analyze.py", "delay": 2},        # reads chain_a, writes chain_b
    {"file": "plot.py", "delay": 2},           # reads chain_b, writes the PNG
])
```

Independent pairs, one stream name per pair (`pair0_stream`, `pair1_stream`):

```python
run_pipeline(stages=[
    {"file": "sim_0.py"}, {"file": "ana_0.py", "delay": 1},
    {"file": "sim_1.py"}, {"file": "ana_1.py", "delay": 1},
])
```

Derive each pair's stream name and output directory from the pair index, so two
pairs never share a name or a file.

Rules for `run_pipeline`:

| Key | Meaning |
|---|---|
| `file` | stage file relative to the work directory. Required; must already exist |
| `args` | list of arguments; the stage reads them from `sys.argv` |
| `delay` | seconds to sleep BEFORE starting this stage. Delays ACCUMULATE: in the example above `plot.py` starts about 4 s after `sim.py`. SST tolerates either order; a reader started first spends its timeout in rendezvous |

`timeout` (default 7200 s) is one shared deadline for all stages. A stage whose
peer failed does not exit on its own and is killed only at that deadline, so set
a timeout sized for the real run, not the default.

Output: `<run_id>_<i>.out` / `.err` per stage in the work directory; the result
carries `exit_code` and stdout/stderr tails per stage, and `status` is `failed`
if ANY stage exited non-zero.

What it refuses (`status: "rejected"`, nothing started): fewer than two stages,
a stage that is not an object, a stage with no `file`, or a file that does not
exist.

## MPI variant

Several concurrent stages are more blocks of the same `mpirun`. Resources: 1
node, 8 rank slots; a 4-rank producer, a 2-rank analysis, a 2-rank render. The
block ranks SUM to the allocation:

```python
run_mpmd(blocks=[{"ranks": 4, "file": "stage_sim.py"},
                 {"ranks": 2, "file": "stage_analyze.py"},
                 {"ranks": 2, "file": "stage_render.py"}])    # 4+2+2 = 8
```

Independent pairs are more blocks, with a distinct stream name per pair.

# 3. One node, many ranks

## Serial variant: NOT AVAILABLE

There is no MPI in this build. `ranks` is ignored by `run_workflow`; launching
N copies would make N processes that each believe they are the only writer,
racing on one metadata index, and a run can exit 0 with wrong output. Do not
try `mpirun` yourself and do not pass `comm=`: `adios2.Stream` raises
`Cannot use MPI since ADIOS2 was built without MPI support`.

What to do instead, in order of preference:

1. One `.bp` PER PROCESS (`part_0.bp`, `part_1.bp`, ...), each written by its
   own stage, combined by a later reader.
2. Gather to ONE writer stage, which is the only process that writes the global
   array.
3. If the goal genuinely needs a collective parallel write or many ranks, say
   so and ask for the `mpi` variant (`MAW_ADIOS=mpi`). Do not fake it.

## MPI variant

One multi-rank program per block, each block splitting its own communicator.
Resources: 1 node, 8 rank slots. A decomposed write: each rank writes its slice
of one global array.

```python
# stage_sim.py: 4 ranks, each owns NLOCAL rows of a (4*NLOCAL, 64) array
import os
import numpy as np
import adios2
from mpi4py import MPI

color = int(os.environ["ADIOS2_STAGE_COLOR"])
world = MPI.COMM_WORLD
comm = world.Split(color, world.Get_rank())
rank, size = comm.Get_rank(), comm.Get_size()

NLOCAL = 16
ad = adios2.Adios(comm)                       # the SPLIT comm, never COMM_WORLD
io = ad.declare_io("sim")
io.set_engine("SST")
with adios2.Stream(io, "/app/work/run0/field_stream", "w", comm) as s:
    for step in range(5):
        local = np.full((NLOCAL, 64), float(step * 10 + rank))
        s.begin_step()
        s.write("U", local, [size * NLOCAL, 64], [rank * NLOCAL, 0], [NLOCAL, 64])
        s.end_step()
```

```python
run_mpmd(blocks=[{"ranks": 4, "file": "stage_sim.py"},
                 {"ranks": 4, "file": "stage_analyze.py"}])    # 4+4 = 8
```

A reader rank asks for its slice with `stream.read("U", start, count)`.

# 4. Many nodes

## Serial variant: NOT AVAILABLE

One node, no MPI, no multi-node launch. Everything in section 3 applies; the
answer is again one `.bp` per process, gather to one writer, or switch variant.

## MPI variant

Resources: 2 nodes, 4 rank slots each. The block ranks sum to
`nnodes x ranks_per_node` from `get_resources`; the server does NOT check the
sum. Over-asking fails inside `mpirun` with "not enough slots available".

```python
# From get_resources().
NODES, RANKS_PER_NODE = 2, 4      # 8 slots in all

run_mpmd(blocks=[{"ranks": 5, "exe": SIM_EXE, "args": ["in.params"]},
                 {"ranks": 2, "file": "stage_analyze.py"},
                 {"ranks": 1, "file": "stage_render.py"}])    # 5+2+1 = 8
```

`SIM_EXE` is `/path/to/sim` here: take the real binary, its arguments and its
required MPI from the domain skill. An `exe` block is launched directly, so the
compiled program must itself open the stream as SST on the same name. A binary
needing the MPI C++ bindings cannot be a block at all if the build is Open MPI 5
(`get_resources` states the constraint). The build is bound to ONE MPI: a domain
whose tools use another cannot use this variant.

# `run_mpmd` and `write_stage` (mpi variant only)

`write_stage(stage, content, overwrite=True)` writes `stage_<name>.py` in the
work directory (name reduced to letters, digits, `_`, `-`) and applies the MPMD
checks to that stage alone. A stage name with no usable characters is rejected.

`run_mpmd(blocks, timeout=7200)` takes at least two blocks, each:

| Key | Meaning |
|---|---|
| `ranks` | integer >= 1. Required |
| `file` | a stage file in the work directory, run as `<python> stage_x.py` |
| `exe` | an external program, launched directly. Use INSTEAD of `file` |
| `args` | optional list of arguments |

It builds ONE command:
`mpirun -n R1 env ADIOS2_STAGE_COLOR=1 <prog1> : -n R2 env ADIOS2_STAGE_COLOR=2 <prog2> : ...`
after loading the adios2 module itself. The result carries `launch_mode`
(`mpmd:<total ranks>:<n>blocks`), `blocks`, `stage_colors` and the exact
command.

**`ADIOS2_STAGE_COLOR`** is the block's index plus one. Every block shares ONE
`MPI_COMM_WORLD`, so every stage must read it and `Split` on it. Two stages with
the same colour merge into one communicator and the writer's rank count then
includes the reader's ranks; a hardcoded colour can collide, and passing
`COMM_WORLD` to ADIOS2 makes a stage write on behalf of every other stage.

What it refuses (`status: "rejected"`, nothing launched): fewer than two blocks;
a block that is not an object; `ranks` not an integer >= 1; neither `file` nor
`exe`; a `file` that does not exist (named per block, because `mpirun` would
abort the whole job naming only the first); and a stage file whose text does not
contain `ADIOS2_STAGE_COLOR`. Missing adios2 module on disk raises before launch.

`run_workflow(filename, ranks=N)` still exists here: N copies of ONE file, which
must still split by role. Stages go through `run_mpmd`.

# `write_bp` and `read_bp` (serial variant only)

One-shot BP file access. The SERVER opens the `Stream` and binds it to `stream`;
`python_code` runs inside it.

| Tool | Binds | Use |
|---|---|---|
| `write_bp(name, bp_path, python_code, timeout=1800)` | `stream` open for `"w"` | `stream.write(...)`, `stream.write_attribute(...)` |
| `read_bp(name, bp_path, python_code, timeout=1800)` | `stream` open for `"r"` | `for _ in stream.steps(): stream.read(...)` |

```python
read_bp(name="inspect", bp_path="/app/inputs/data/particles.bp",
        python_code="for _ in stream.steps():\n    print(stream.read('positions').shape)")
```

It REJECTS code that contains `import adios2` or `adios2.Stream(`: opening the
same path twice corrupts it. The result has `stream_used`; false (code never
called `.write(` / `.read(` / `*_attribute(`) comes with a warning, and for a
write that means an empty `.bp` and exit 0. These tools run to completion before
returning, so they are NOT stage exchange: use them to write a result or read an
input file, never to connect two stages.

# Stream API

The small set actually used. The long manual is `knowledge/ADIOS`.

```python
import adios2

# SST: build the IO, select the engine, pass it to Stream.
ad = adios2.Adios()                    # mpi variant: adios2.Adios(comm)
io = ad.declare_io("name")
io.set_engine("SST")
stream = adios2.Stream(io, "stream_name", "w")     # mpi: ..., "w", comm)
```

| Call | Meaning |
|---|---|
| `adios2.Stream(io, name, mode)` | `mode` is `"w"` or `"r"`; use as `with ... as s:` so it closes. SST needs the `io` form |
| `adios2.Stream(path, mode)` | opens a BP FILE. Fine for `write_bp`, `read_bp` and input data; NOT for stage-to-stage streaming |
| `s.write(name, data, shape, start, count)` | `shape` = global array, `start`/`count` = this writer's slice. On one process, `[0]*ndim` and `data.shape` |
| `s.read(name)` | returns the numpy array. `s.read(name, start, count)` reads a slice |
| `s.steps()` | `for _ in s.steps():` loops until the writer closes, one iteration per step |
| `s.begin_step()` / `s.end_step()` | writer side: bracket the writes of one step, balanced inside the loop |
| `s.write_attribute(name, value)` / `s.read_attribute(name)` | small metadata (units, counts) |
| `comm=` / `Adios(comm)` | MPI variant ONLY; raises on serial |

Not covered here: DataMan, the HDF5 engine, block reads and engine parameters.
The server does nothing for them; verify against `knowledge/ADIOS` first.

# Performance and Hygiene

- **Open the Stream ONCE, outside the step loop.** Reopening per step re-runs the
  SST rendezvous each time. One `with` around the loop.
- **Read with `for _ in s.steps():`**, not hand-written `begin_step`/`end_step`
  pairs that can end unbalanced and stall the writer. On the write side, keep one
  balanced `begin_step`/`end_step` per loop iteration.
- **One distinct stream name per pair or hop.** Two pairs sharing a name
  rendezvous with the wrong peer.
- **Do not clear `.sst` yourself.** The server removes stale `*.sst` directories
  under the work directory before every launch (`stale_sst_cleared`). Keep
  stream names inside the work directory so they are covered.
- **`check_package("adios2")` first**, and `check_package("mpi4py")` under MPMD.
  If adios2 does not import, say so and stop: there is no numpy fallback, and a
  pipeline that swaps streams for `.npy` is not the workflow asked for.
- **Set `matplotlib.use("Agg")`** in any stage that draws; nodes are headless.
- **Evidence the engine was used:** reader stdout reports steps and shapes.
  `"engine": "adios"` in a response names the server, not proof of I/O.

# Common Pitfalls

| Pitfall | Rule |
|---|---|
| Writing one `workflow.py` with stage functions called by `main()` | Wrong for BOTH variants. One file per stage; launch with `run_pipeline` (serial) or `run_mpmd` (mpi) |
| Calling `run_mpmd` or `write_stage` on the serial server (or `run_pipeline`/`write_bp`/`read_bp` on mpi) | Not registered there. Read `adios2_build` from `get_resources` first |
| Assuming the mpi variant | Serial is the default and the fallback for an unset or unrecognised `MAW_ADIOS` |
| `run_workflow` on a lone writer | Serial: completes, leaves `<name>.sst`, a later reader "streams" it. Nothing was live. Use `run_pipeline` |
| One stage passed to `run_pipeline` / `run_mpmd` | Refused: at least two. One stage streams to nothing |
| `Stream(path, mode)` between stages | That is BP: a reader opens a file the writer has not closed and silently reads whatever is there. Select SST via `io.set_engine("SST")` in EVERY stage |
| Writer and reader using different stream names | No rendezvous. Same string on both ends; build it from one constant |
| Reusing the in-memory array instead of reading it back | A pipeline that never used the engine. The consumer must `stream.read` in its own process |
| mpi4py, `comm=` or `MPI.COMM_WORLD` in a serial stage | Forbidden: `Cannot use MPI since ADIOS2 was built without MPI support`. `write_file` warns on it |
| A decomposed write (shape/start/count) from several serial processes into one `.bp` | They race on the metadata index and can exit 0 with wrong output. One `.bp` per process, or gather to one writer. `write_file` warns on `.write(` with shape and start; on a single writer the warning is benign |
| `ranks` on `run_workflow` / `run_pipeline` in the serial build | Ignored. Concurrency is more stage files |
| Passing `COMM_WORLD` to ADIOS2 in an MPMD stage | The stage then writes for every block's ranks. Split on `ADIOS2_STAGE_COLOR` and pass the split communicator |
| A stage that ignores `ADIOS2_STAGE_COLOR` | `run_mpmd` rejects the layout. Hardcoded colours also collide |
| Block ranks summing to more than the allocation | `mpirun` fails with "not enough slots available". Sum them against `nnodes x ranks_per_node` yourself; the server does not |
| `delay` assumed to be absolute from launch | It is slept before THAT stage starts, so delays add up down the list |
| Reopening the Stream every step | Each open re-runs the SST rendezvous. Open once outside the loop |
| `define_variable` inside the step loop (lower-level API) | Raises "already defined". Define once outside the loop, or just use `s.write` |
| `write_bp` with `import adios2` or `adios2.Stream(` in the code | Rejected: the server already opened the stream as `stream` |
| `write_bp` code that never calls `stream.write` | Empty `.bp`, exit 0. Check `stream_used` in the result |
| `read_bp` / `write_bp` used to link two stages | They run to completion; nothing can read while they write. Use `run_pipeline` |
| A hung stage with the default 7200 s timeout | A stage whose peer failed waits for the shared deadline. Size `timeout` for the real run and read each stage's `.err` |
| Reader reports steps while the writer exited non-zero | The reader read a stale `.sst` from an earlier run. Fix the writer and re-run |
| SST between two threads of one process | Deadlocks. Stages are separate processes or rank blocks |
| `adios2` fails to import | Report an environment gap and stop. Do not swap in `.npy` or work around the loader from inside a stage |
| mpi variant: `Cannot use MPI since ADIOS2 was built without MPI support` | The stage got the serial wheel: the +mpi module was not loaded. `run_mpmd` loads it; a stage launched another way will not have it |
| mpi variant: a domain whose tools use a different MPI | This build is bound to ONE MPI; that domain cannot use this engine |
