---
name: systems/adios
description: >
  ADIOS2 reference for MAW workflows. Covers the generated-file execution model
  (write_workflow/run_workflow), the staged-pipeline structure, Stream read/write
  round trips, MPMD in-situ streaming over SST with MPI_RANKS, BP format, the
  MPI-enabled build, and how ADIOS2 differs from Parsl/PyCOMPSs.
---

# ADIOS2 — System Skill

ADIOS2 is a high-performance I/O framework from ORNL for scientific
simulations. It moves data between workflow stages through BP files, SST
streams, and in-memory transports.

Load this skill when generating or debugging an ADIOS2 workflow.

---

## READ THIS FIRST: You Write One File, Structured as a Pipeline

The adios engine is **generated-file only**. You have exactly two execution
tools:

| Tool | What it does |
|---|---|
| `write_workflow(python_code, filename)` | Writes ONE standalone workflow file to `/app/work/run0/` |
| `run_workflow(filename)` | The server executes that file |

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

### What `write_workflow` rejects

- no stage functions, or no `main()`
- `subprocess`/`os.system`/`os.popen` at module top level
- never opening an `adios2.Stream` while adios2 is installed
- writing without reading back (`adios_writes` or `adios_reads` at zero)

On rejection, read the returned `errors`, fix the file, and call
`write_workflow` again.

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

## MPMD Mode: One `mpirun`, Zero Per-Stage Subprocesses

The pipeline above is sequential: each stage runs to completion before the next
starts, and data lands in a `.bp` file in between. For true **in-situ**
streaming — producer and consumer running *concurrently*, data moving through
memory over SST — declare a rank budget and split the communicator.

Add a top-level `MPI_RANKS = <int>`. The server sees it and launches the file as
`mpirun -n <MPI_RANKS> python workflow.py`, so every stage is a rank of the same
job. `run_workflow` reports `launch_mode: "mpmd:<n>"` instead of `"serial"`.

```python
MPI_RANKS = 4                       # server launches under mpirun -n 4

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

This is the gray-scott pattern: instead of four terminals or an MPMD colon
command line, the roles live in one file and dispatch on rank.

What the validator additionally enforces when `MPI_RANKS` is set:

- `MPI.COMM_WORLD` must be used (rank/size come from it)
- `.Split(` must be called (each stage needs its own communicator)
- the split `comm` must be passed to ADIOS2 — `Adios(comm)` and
  `io.open(name, mode, comm)`, not bare `Adios()`

With `MPI_RANKS` set, `main()` is not required: rank role selects the stage.

### MPMD gotchas

| Gotcha | Rule |
|---|---|
| `define_variable` inside the step loop | Raises "variable U already defined". Define once before the loop, reuse the handle |
| Passing `COMM_WORLD` to a stage's `Adios()` | Stages must get their own split comm, or they collide |
| Reading without `set_selection` | A reader rank must declare which slice of the global array it wants |
| `v.Shape()` | The Python binding is lowercase `v.shape()` |
| SST writer with no reader | Blocks on the rendezvous. Both roles must exist in the same launch |
| SST between two threads of one process | Deadlocks. SST expects separate ranks — use MPMD, not threads |
| A run that times out, then every later run times out | A killed run leaves a stale `<name>.sst` rendezvous file and the next open waits on a peer that is gone. `run_workflow` clears `*.sst` before each MPMD launch, but if you run a file by hand, delete them yourself |

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

## Engine States

Every `write_workflow`/`run_workflow` response reports an `engine` field:

1. **`"adios2"`** — ADIOS2 installed and the file genuinely calls a real API
   (`adios2.Stream`/`open`/`declare_io`/`.write(`/`.read(`). The only state
   meaning real ADIOS2 I/O happened.
2. **`"adios2-unused"`** — ADIOS2 installed, the file mentions it, but no real
   API call was detected. This is the "imported but never used" failure mode:
   the run needs redoing, not reporting as success.
3. **`"adios2-fallback"`** — ADIOS2 isn't installed here. The pipeline still
   runs using numpy file I/O instead of BP — same computational result, no
   ADIOS2. An environment gap, not an agent error.

The orchestrator routes back to you on `"adios2-unused"` for a stage that
should have done real I/O.

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

Two environment traps the server already handles via `TASK_ENV`, worth knowing
if you run a workflow by hand:

- **`LD_LIBRARY_PATH`** must include `$ADIOS2_HOME/lib64`, or the import fails
  with `libadios2_cxx11_mpi.so.2.10: cannot open shared object file`.
- **`LD_PRELOAD`** must pin gcc-14's `libstdc++.so.6`. `numpy` loads a spack
  gcc-8.5 `libstdc++` first, and whichever lands in the process wins — so
  importing numpy before adios2 fails with
  `version 'GLIBCXX_3.4.32' not found`.

Override `ADIOS2_HOME`, `GCC14_LIB`, or `OPENMPI_BIN` if installed elsewhere.

Without the MPI build, serial pipelines still work normally; only `MPI_RANKS`
MPMD mode is unavailable.

---

## Common Use Cases

- Reading large MD trajectory files in BP format
- Streaming simulation output for in-situ analysis
- Coupled simulations (simulation + analytics pipeline)
- High-performance parallel I/O on HPC file systems
- Paired with LAMMPS via `dump adios` in the LAMMPS input script
