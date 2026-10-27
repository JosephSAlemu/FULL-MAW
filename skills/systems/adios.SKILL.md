---
name: systems/adios
description: >
  ADIOS2 reference for MAW workflows. Covers the generated-file execution model
  (write_file/run_workflow/run_mpmd), the staged-pipeline structure, Stream read/write
  round trips, MPMD in-situ streaming over SST, BP format, the
  MPI-enabled build, and how ADIOS2 differs from Parsl/PyCOMPSs.
---

# ADIOS2 — System Skill

ADIOS2 is a high-performance I/O framework from ORNL for scientific
simulations. It moves data between workflow stages through BP files, SST
streams, and in-memory transports.

Load this skill when generating or debugging an ADIOS2 workflow.

---

## READ THIS FIRST: You Write One File, Structured as a Pipeline

The adios engine is **generated-file only**. You have these execution tools:

| Tool | What it does |
|---|---|
| `write_file(path, content)` | Writes a file under `/app/work/run0/` — the ONE standalone workflow file, and anything it reads |
| `run_workflow(filename, ranks)` | Launches it as `<python> workflow.py`, or `mpirun -n <ranks> <python> workflow.py` |
| `run_mpmd(blocks)` | Launches several programs as rank blocks of ONE mpirun. For when a compiled binary must stream to Python stages live |

**You never execute anything yourself.** You do not call the CLI from your
tools, write a bash script, or write a PBS script. You describe the whole
pipeline in one file and hand it to the server.

### ADIOS2 is an I/O library, not a scheduler

This is the key difference from Parsl and PyCOMPSs. ADIOS2 has **no task
decorator** — no `@bash_app`, no `@task`, nothing that wraps a function and
schedules it. What it gives you is high-performance data movement between
stages.

So the generated file is a **staged pipeline**, and what matters is that the
stages are real and that data flows between them through ADIOS2:

- Each stage is a **top-level function**.
- A **`main()`** calls the stages in order.
- Inter-stage numerical data moves through ADIOS2: the producing stage calls
  `stream.write(...)` on an `adios2.Stream(path, "w")`, and the consuming stage
  reads it back with `stream.read(...)` from an `adios2.Stream(path, "r")`.
  **Write it, then actually read it back** — do not keep using the in-memory
  copy, because then no real I/O happened.
- Human-facing outputs (PNG, summary text) stay plain files. BP is for the
  numerical arrays flowing between stages.

### CLI steps

Because ADIOS2 cannot launch programs, a CLI step for an **external compiled
binary** is a stage function that uses `subprocess` internally. **That is allowed
only inside a stage body.** `subprocess` at the top level of the file is rejected
— at that point the file is a flat script rather than a pipeline.

Python stages must never be subprocesses: they go in this same file and talk to
each other through ADIOS2. See "MPMD Mode" below for running the whole pipeline
under a single `mpirun` with zero per-stage subprocesses.

### What `write_file` checks

A Python file is parsed before it is written, so a **syntax error** comes back
as `"status": "rejected"` and nothing reaches disk. Fix it and write again.

Everything structural comes back as `warnings` on a file that *was* written:

- `adios2` never imported
- nothing written to a stream (`.write(` / `.put(`)
- nothing read back (`.read(` / `.get(`)
- no `main()` and no `--stage` dispatch

Warnings do not block the run, and that is the trap this engine is most prone
to: a pipeline that imports adios2 and never calls it runs fine, exits 0, and
has moved no data through ADIOS2 at all. Treat every warning as a defect.

`subprocess` at module top level is no longer refused for you either. Keep it
inside a stage body anyway — at top level the file is a flat script rather than
a pipeline, and nothing about it is reproducible as an ADIOS2 workflow.

---

## Worked Example: simulate → analyze, with a real BP round trip

```python
import os
import numpy as np
import adios2

WORK = "/app/work/run0"
BP = f"{WORK}/particles.bp"


def simulate(paramfile, bp_path):
    """CLI stage: run the simulation binary, then publish its output via BP.

    subprocess is used here because ADIOS2 cannot launch programs. It lives
    inside the stage body, never at module level.
    """
    import subprocess

    subprocess.run(
        ["mpirun", "-n", "8", "/path/to/sim_binary", paramfile],
        check=True,
    )

    arr = np.loadtxt(f"{WORK}/raw_output.txt", dtype=np.float32)
    with adios2.Stream(bp_path, "w") as s:
        s.write("positions", arr, arr.shape, [0] * arr.ndim, arr.shape)
        s.write_attribute("nparticles", arr.shape[0])
    return bp_path


def analyze(bp_path, outdir):
    """Analysis stage: read the array back out of BP, render it."""
    import matplotlib
    matplotlib.use("Agg")            # headless compute node
    import matplotlib.pyplot as plt

    with adios2.Stream(bp_path, "r") as s:
        for _ in s.steps():
            positions = s.read("positions")

    os.makedirs(outdir, exist_ok=True)
    plt.hist2d(positions[:, 0], positions[:, 1], bins=256)
    png = os.path.join(outdir, "density.png")
    plt.savefig(png, dpi=150)
    plt.close()
    return png


def main():
    os.makedirs(WORK, exist_ok=True)
    simulate("/path/to/indat.params", BP)
    png = analyze(BP, WORK)
    print(f"wrote {png}")


if __name__ == "__main__":
    main()
```

What makes this legal: named stages driven by `main()`, `subprocess` confined
to a stage body, and `positions` genuinely written to BP and read back out
rather than passed in memory.

---

## MPMD Mode: One `mpirun`, Ranks Provisioned Per Stage

The pipeline above is sequential: each stage runs to completion before the next
starts, and data lands in a `.bp` file in between. For true **in-situ**
streaming — producer and consumer running *concurrently*, data moving through
memory over SST — run every stage as a rank block of a single `mpirun`.

There are two ways to do that. Pick by whether an external compiled binary has
to be one of the ranks.

There are two ways to do that. Pick by whether an external compiled binary has
to be one of the ranks. **Both are chosen at the tool call, not declared in the
file** — the server no longer reads `MPMD_LAYOUT` or `MPI_RANKS` out of your
code, so those top-level constants do nothing.

| | `run_mpmd(blocks=...)` | `run_workflow(ranks=N)` |
|---|---|---|
| Launch | `mpirun -n 8 sim : -n 4 python wf.py --stage analyze` | `mpirun -n 12 python wf.py` |
| Programs | several, one rank block each | one, N copies of this file |
| External binary as a rank | **yes** | no |
| Stage selection | `--stage` argument | `rank < N` role test |
| Use when | a simulation binary streams to Python analysis | every stage is Python |

### `run_mpmd` — different programs, one launch

This is the gray-scott colon form. Pass the blocks to the tool; the server
builds the command line.

```python
run_mpmd(blocks=[
    {"ranks": 8, "exe": "/lcrc/project/PEDAL/HACC/HACC_go/improv.cpu/mpi/bin/hacc_tpm",
     "args": ["./params/indat.params"]},
    {"ranks": 4, "stage": "analyze"},
    {"ranks": 1, "stage": "render"},
])
```

produces

```bash
mpirun -n 8 /.../hacc_tpm ./params/indat.params \
     : -n 4 python workflow.py --stage analyze \
     : -n 1 python workflow.py --stage render
```

13 ranks, one job, three programs. The simulation and the analysis are alive at
the same time, so SST streams between them instead of the analysis waiting on a
finished file.

- A block with `"exe"` launches that program with `"args"`.
- A block with `"stage"` re-enters **this same file** as
  `python workflow.py --stage <name>`, so Python stages are rank blocks rather
  than subprocesses.
- `run_mpmd` reports `launch_mode: "mpmd:13:3blocks"` and the exact
  `launch_command` it used.
- At least two blocks. With one, nothing streams to anything — use
  `run_workflow`.

Because the file is re-entered once per Python block, it must dispatch on
`--stage`:

```python
import argparse
import numpy as np
import adios2
from mpi4py import MPI

STREAM = "halo_stream"


def analyze(comm):
    """Reads the simulation's SST stream, writes halo properties onward."""
    ad = adios2.Adios(comm)                 # the block's own comm, not COMM_WORLD
    io = ad.declare_io("ana")
    io.set_engine("SST")                    # concurrent blocks -> stream, not file
    reader = io.open(STREAM, adios2.bindings.Mode.Read, comm)
    out = adios2.Adios(comm).declare_io("halos")
    out.set_engine("SST")
    writer = out.open("halo_props", adios2.bindings.Mode.Write, comm)
    hvar = None
    while True:
        if reader.begin_step() != adios2.bindings.StepStatus.OK:
            break
        v = reader.inquire_variable("positions") if hasattr(reader, "inquire_variable") \
            else io.inquire_variable("positions")
        n = int(v.shape()[0])
        buf = np.zeros(n, dtype=np.float64)
        v.set_selection([[0], [n]])
        reader.get(v, buf)
        reader.end_step()

        halos = buf * 1.0                    # real FOF/SOD work goes here
        if hvar is None:                     # define ONCE, outside the loop
            hvar = out.define_variable("halo_mass", halos, [halos.size], [0], [halos.size])
        writer.begin_step(); writer.put(hvar, halos); writer.end_step()
    reader.close(); writer.close()


def render(comm):
    """Final block: reads halo properties, writes the PNG deliverable."""
    import matplotlib
    matplotlib.use("Agg")                    # headless compute node
    import matplotlib.pyplot as plt
    ad = adios2.Adios(comm)
    io = ad.declare_io("viz")
    io.set_engine("SST")
    e = io.open("halo_props", adios2.bindings.Mode.Read, comm)
    while True:
        if e.begin_step() != adios2.bindings.StepStatus.OK:
            break
        v = io.inquire_variable("halo_mass")
        n = int(v.shape()[0])
        buf = np.zeros(n, dtype=np.float64)
        v.set_selection([[0], [n]])
        e.get(v, buf); e.end_step()
        plt.hist(buf, bins=128)
    e.close()
    plt.savefig("/app/work/run0/dm_density_slice.png", dpi=150)


STAGES = {"analyze": analyze, "render": render}

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", required=True, choices=sorted(STAGES))
    args = ap.parse_args()
    STAGES[args.stage](MPI.COMM_WORLD)       # this block's ranks only
```

Note what `COMM_WORLD` means here: mpirun gives **each rank block its own
`COMM_WORLD`** under the colon form, so a `--stage` block does not need to
`Split()` — it already has just its own ranks. That is the opposite of
`ranks` mode below, where every rank shares one `COMM_WORLD` and splitting
is mandatory.

### `run_workflow(ranks=N)` — one program, roles by rank

Simpler, and right when every stage is Python. The server launches
`mpirun -n N python workflow.py`; all ranks share `COMM_WORLD`, so each stage
must carve out its own communicator. N is passed to the tool, not declared in
the file.

```python
# launched with run_workflow(filename="workflow.py", ranks=4)

import numpy as np, adios2
from mpi4py import MPI

world = MPI.COMM_WORLD
rank, size = world.Get_rank(), world.Get_size()

NPROD = 2                                   # ranks 0-1 produce, 2-3 consume
role = 0 if rank < NPROD else 1
comm = world.Split(color=role, key=rank)    # each stage gets its own communicator
lrank, lsize = comm.Get_rank(), comm.Get_size()

def simulate(comm):
    ad = adios2.Adios(comm)                 # pass the SPLIT comm, not COMM_WORLD
    io = ad.declare_io("sim")
    io.set_engine("SST")                    # in-memory streaming, no file
    u = np.zeros(4, dtype=np.float64)
    var = io.define_variable("U", u, [lsize*4], [lrank*4], [4])   # define ONCE
    e = io.open("gs_stream", adios2.bindings.Mode.Write, comm)
    for step in range(3):
        u[:] = float(step * 10 + lrank)
        e.begin_step(); e.put(var, u); e.end_step()
    e.close()

def analyze(comm):
    ad = adios2.Adios(comm)
    io = ad.declare_io("ana")
    io.set_engine("SST")
    e = io.open("gs_stream", adios2.bindings.Mode.Read, comm)
    while True:
        if e.begin_step() != adios2.bindings.StepStatus.OK:
            break
        v = io.inquire_variable("U")
        n = int(v.shape()[0])
        out = np.zeros(n, dtype=np.float64)
        v.set_selection([[0], [n]])
        e.get(v, out)
        e.end_step()
        print(f"consumer {lrank}: sum {out.sum()}")
    e.close()

simulate(comm) if role == 0 else analyze(comm)
```

With either declaration, `main()` is not required: rank role or `--stage`
selects the work.

### Sizing the rank budget

Total ranks across all blocks must fit the allocation. Call `get_resources`
first.

- Under `--env hpc` the budget is `ntasks` from `get_resources`.
- Under `--env multi-node` use the **MPI capacity line in your task context**,
  and `get_resources` — the allocation is exactly as wide as what the
  application may use, so the two agree.
- Under `--env local` there is no PBS fabric; keep the total small (2–4) or
  stay with a serial pipeline.

### What is checked, and what is not

`run_mpmd` checks only the shape of the blocks you pass it: at least two, each
with an integer `ranks` of 1 or more and either `exe` or `stage`. One block is
refused, because one block streams to nothing.

**Nothing else is checked.** These are on you, and each fails at run time
rather than at the tool call:

- the file reads `--stage` and dispatches on it — without this every stage
  block runs the same default path
- every `"stage"` you name exists as a top-level function in the file
- an SST engine is selected — concurrent blocks sharing a BP file make the
  consumer wait on a file the producer has not closed, which looks like a hang
- under `ranks`, `MPI.COMM_WORLD` is split and the split comm is passed to
  `Adios(comm)` / `io.open(name, mode, comm)`

### MPMD gotchas

| Gotcha | Rule |
|---|---|
| `define_variable` inside the step loop | Raises "variable U already defined". Define once before the loop, reuse the handle |
| Passing `COMM_WORLD` to a stage's `Adios()` under `ranks` | Stages must get their own split comm, or they collide. Under `run_mpmd` each block already has its own `COMM_WORLD` |
| Reading without `set_selection` | A reader rank must declare which slice of the global array it wants |
| `v.Shape()` | The Python binding is lowercase `v.shape()` |
| SST writer with no reader | Blocks on the rendezvous. Both roles must exist in the same launch |
| SST between two threads of one process | Deadlocks. SST expects separate ranks — use `run_mpmd` or `ranks`, not threads |
| A run that times out, then every later run times out | A killed run leaves a stale `<name>.sst` rendezvous file and the next open waits on a peer that is gone. The server clears `*.sst` before every launch, so this should not recur — if it does, the file is being written somewhere outside the work directory |
| `subprocess` to launch the simulation when using `run_mpmd` | Pointless and wrong — give the binary its own `{"ranks": N, "exe": ...}` block so it shares the launch and can stream |
| Total ranks exceeding the allocation | mpirun fails with "not enough slots available". Size from `get_resources` / the MPI capacity line |

---
## Key API

```python
import adios2
import numpy as np

# Stream API (preferred -- this is what the validator looks for)
with adios2.Stream("output.bp", "w") as s:
    s.write("temperature", arr, arr.shape, [0], arr.shape)
    s.write_attribute("units", "kelvin")

with adios2.Stream("output.bp", "r") as s:
    for _ in s.steps():
        data = s.read("temperature")
        units = s.read_attribute("units")

# Lower-level engine API, for streaming (SST) producers
adios = adios2.ADIOS()
io = adios.declare_io("SimOutput")
io.set_engine("SST")
writer = io.open("stream.bp", adios2.Mode.Write)
writer.begin_step()
writer.put(var, data)
writer.end_step()
writer.close()
```

`stream.write(name, array, shape, start, count)` — the last three describe the
global shape and this writer's slice of it, which is what makes parallel writes
compose into one logical array.

---

## Data Formats

- **BP (Binary Pack)**: ADIOS2's native format, optimized for parallel I/O
- **SST (Sustainable Staging Transport)**: real-time streaming between processes
- **DataMan**: in-memory exchange for tightly coupled workflows
- **HDF5**: readable/writable through ADIOS2's HDF5 engine

---

## Key Differences from Parsl/PyCOMPSs

| Feature | Parsl | PyCOMPSs | ADIOS2 |
|---|---|---|---|
| Primary role | Task scheduling | Task scheduling | Data I/O & transport |
| Unit of work | `@python_app` / `@bash_app` | `@task` / `@binary` / `@mpi` | A plain stage function |
| CLI steps | `@bash_app` command string | `@binary`/`@mpi` decorator | `subprocess` inside a stage |
| Inter-stage data | Files / futures | Files / task parameters | BP files, SST streams |
| Streaming | No | No | Yes (SST, DataMan) |
| In-situ analysis | No | No | Yes |

---

## Was ADIOS2 Actually Used?

Every response reports `"engine": "adios"` — that is which server handled the
call, not evidence that any ADIOS2 I/O happened. The server holds no adios2 code
and cannot tell you. Two things can:

1. **The `warnings` on `write_file`.** No import, no `.write(`, no `.read(`
   means the file cannot have moved data through ADIOS2. This is the
   "imported but never used" failure mode, and it is caught here or not at all.
2. **The outputs.** A BP dataset is a *directory* (`<name>.bp/` holding
   `data.0`, `md.0`, ...). `list_files` showing no `.bp` tree, or one that is
   empty, means the round trip did not happen whatever the exit code said.

`check_package("adios2")` before you write anything tells you whether the
bindings are importable at all. If they are not, say so and stop — there is no
numpy fallback any more, and a pipeline that quietly swaps BP for `.npy` is not
the workflow that was asked for.

---

## Common Pitfalls

| Pitfall | Rule |
|---|---|
| `subprocess` at module top level | Rejected. Put the CLI call inside a stage function |
| `subprocess` to run another *Python* stage | Never. Python stages live in this file and talk over ADIOS2 |
| Flat script with no functions | Rejected. Named stages, driven by `main()` |
| Writing to BP but never reading back | Rejected. The consumer must `stream.read(...)` it |
| Passing a big array stage-to-stage in memory | Defeats the point of this engine — route it through BP |
| Putting the PNG or summary into BP | BP is for inter-stage numerical arrays; deliverables stay plain files |
| Rendering without `matplotlib.use("Agg")` | Headless nodes have no display |
| Assuming ADIOS2 schedules anything | It does not. It is I/O only; ordering comes from `main()` |

---

## Running with ADIOS2

```bash
python agent_mcp.py --engine adios --paper 1 --goal "..."
```

`adios_server.py` sets the needed env vars itself, so no module loading is
required before running the agent.

### The MPI build (required for MPMD)

**The PyPI `adios2` wheel is a SERIAL build** — `bindings.is_built_with_mpi` is
`False`, `Adios(comm)` raises, and SST cannot stream between ranks. MPMD mode
needs an MPI-enabled build. On Improv this was built by hand:

```bash
module load gcc/14.2.0 openmpi/5.0.7/gcc/14.2.0 cmake/3.27.4

# mpi4py must be built against the SAME MPI, not the PyPI wheel
MPICC=$(which mpicc) pip install --no-binary=mpi4py --force-reinstall --no-cache-dir mpi4py

curl -LO https://github.com/ornladios/ADIOS2/archive/refs/tags/v2.10.2.tar.gz
tar xzf v2.10.2.tar.gz && cd ADIOS2-2.10.2 && mkdir build && cd build
cmake .. -DCMAKE_INSTALL_PREFIX=$HOME/.local/adios2-mpi \
  -DCMAKE_BUILD_TYPE=Release -DADIOS2_USE_MPI=ON -DADIOS2_USE_Python=ON \
  -DPython_EXECUTABLE=<repo>/venv3/bin/python \
  -DADIOS2_USE_Fortran=OFF -DADIOS2_USE_HDF5=OFF -DBUILD_TESTING=OFF \
  -DCMAKE_C_COMPILER=$(which mpicc) -DCMAKE_CXX_COMPILER=$(which mpicxx)
make -j32 && make install
```

Then copy the built package over the wheel's in site-packages. Verify with:

```python
from adios2 import bindings; print(bindings.is_built_with_mpi)   # must be True
```

Two environment traps. **The server no longer handles either of them** — it
hardcodes no library paths at all, and inherits whatever the setup script
sourced before the run exported. If `check_package("adios2")` fails, one of
these is why, and the setup script is where to fix it:

- **`LD_LIBRARY_PATH`** must include `$ADIOS2_HOME/lib64`, or the import fails
  with `libadios2_cxx11_mpi.so.2.10: cannot open shared object file`.
- **`LD_PRELOAD`** must pin gcc-14's `libstdc++.so.6`. `numpy` loads a spack
  gcc-8.5 `libstdc++` first, and whichever lands in the process wins — so
  importing numpy before adios2 fails with
  `version 'GLIBCXX_3.4.32' not found`.

Report either as an environment gap. Do not try to work around it from inside a
workflow file — the loader's search path is fixed before the interpreter starts.

Without the MPI build, serial pipelines still work normally; only `ranks`
MPMD mode is unavailable.

---

## Common Use Cases

- Reading large MD trajectory files in BP format
- Streaming simulation output for in-situ analysis
- Coupled simulations (simulation + analytics pipeline)
- High-performance parallel I/O on HPC file systems
- Paired with LAMMPS via `dump adios` in the LAMMPS input script