---
name: systems/parsl
description: >
  Parsl parallel scripting library reference for MAW workflows. Covers the generated-file
  execution model (write_file/run_workflow), @python_app/@bash_app rules,
  HighThroughputExecutor config, common pitfalls, and the exact config skeleton used here.
---

# Parsl — System Skill

Parsl orchestrates workflow steps as asynchronous Python functions. In MAW it runs
every step of a workflow — simulation binaries and Python analysis alike — as apps
scheduled by a Parsl DataFlowKernel.

---

## READ THIS FIRST: You Write One Parsl File, Parsl Runs Everything

The parsl engine is **generated-file only**. You have exactly two execution tools:

| Tool | What it does |
|---|---|
| `write_file(path, content)` | Writes a file under `/app/work/run0/`. The ONE standalone Parsl driver file goes through here, and so does anything that file reads |
| `run_workflow(filename)` | The server launches that driver as `<python> workflow.py` |

These are the only execution tools this server has. There is no tool that takes
a code string or a command string and runs it for you.

**You never execute anything yourself.** No subprocess, no direct CLI call, no bash
script, no PBS script, no `mpirun` typed by you. Every command line in the workflow
is a string *returned by a `@bash_app`*, and Parsl is what runs it.

### The rule

Every unit of work in the generated file is a decorated function:

- **`@bash_app`** — ANY command-line invocation: simulation binaries, MPI runs,
  format conversion, anything you would otherwise type in a shell. The function
  body returns the command **string**. Parsl executes it.
- **`@python_app`** — pure Python work: analysis, plotting, writing summaries.

Both live in the **same file**. A simulation plus its visualization is one file
containing one `@bash_app` and one `@python_app` — not two files, not two tool calls.

### What the file must contain

Unlike the old model, this file **does** build and load its own Parsl runtime:

1. Build a `Config` (see the config sections below)
2. `parsl.load(config)`
3. Define the `@bash_app` / `@python_app` functions
4. Call them, pass futures between them to express dependencies
5. `.result()` to block for completion — in the driver body only, never inside an app
6. `parsl.clear()` at the end

### What `write_file` checks

A Python file is parsed before it is written, so a **syntax error** comes back as
`"status": "rejected"` and nothing reaches disk. Fix it and write again.

Everything structural comes back as `warnings` on a file that *was* written:

- defines no `@bash_app` and no `@python_app`
- never calls `parsl.load(...)`

Warnings do not block `run_workflow`, and that is the trap: a driver that never
loads a config runs, exits 0, and schedules nothing. Treat a warning as a defect
and fix the file before running it.

These are **not** checked for you any more, and are still forbidden:

- importing or calling `subprocess`, `os.system`, `os.popen`
- `mpirun` / `srun` / `mpiexec` **outside** a `@bash_app` body

Nothing will stop you writing them. What happens instead is that the work runs
outside Parsl, the trace records a Parsl run that Parsl never scheduled, and the
result is not reproducible by the engine it claims to use.

---

## Worked Example: a CLI step and a Python step in one file

This is the canonical shape — an 8-rank MPI simulation followed by a Python
visualization of its output, in a single file.

```python
import os
import parsl
from parsl import bash_app, python_app
from parsl.config import Config
from parsl.data_provider.files import File
from parsl.executors import HighThroughputExecutor
from parsl.providers import LocalProvider

RANKS = 8

@bash_app
def run_sim(exe, envfile, paramfile, ranks, workdir,
            inputs=(), outputs=(),
            stdout=parsl.AUTO_LOGNAME, stderr=parsl.AUTO_LOGNAME):
    # Return the COMMAND STRING. Parsl runs it; we never do.
    # Products are declared as outputs=[File(...)]; read their paths off the
    # File objects so the command writes exactly what Parsl is tracking.
    snapshot = outputs[0].filepath
    return (
        # cd to the WORK directory, not to the program's reference tree: a
        # program that writes relative to its cwd must be launched from the
        # directory the run owns and can write to. paramfile is absolute.
        f"source {envfile} && "
        f"cd {workdir} && "
        f"mpirun -n {ranks} {exe} {paramfile}"
    )

@python_app
def visualize(outdir, inputs=()):
    # All imports inside the body -- workers don't share the driver's namespace.
    import os
    import numpy as np
    import matplotlib
    matplotlib.use("Agg")           # headless, no display on a compute node
    import matplotlib.pyplot as plt

    snapshot = inputs[0].filepath   # staged by Parsl, guaranteed to exist here
    os.makedirs(outdir, exist_ok=True)
    # ... read snapshot, bin particles, render ...
    png = os.path.join(outdir, "density_slice.png")
    plt.savefig(png, dpi=150)
    plt.close()
    return png

def main():
    work = "/app/work/run0"
    config = Config(
        executors=[
            HighThroughputExecutor(
                label="wf_htex",
                max_workers_per_node=1,   # exactly one worker -- it runs the mpiexec
                provider=LocalProvider(min_blocks=1, max_blocks=1, init_blocks=1),
            )
        ],
        strategy="none",
        initialize_logging=False,
    )
    parsl.load(config)
    try:
        # Submit the whole DAG without blocking: the data dependency is expressed
        # by handing sim.outputs to the next app, not by calling sim.result().
        sim = run_sim(exe="/path/to/binary", envfile="/path/to/env",
                      paramfile="/path/to/indat.params", ranks=RANKS, workdir=work,
                      outputs=[File(f"{work}/output/snapshot")])

        png = visualize(work, inputs=sim.outputs)

        print(f"wrote {png.result()}")    # the ONLY .result() -- final deliverable
    finally:
        parsl.clear()

if __name__ == "__main__":
    main()
```

Note what makes this legal: the only `mpirun` in the file is inside the
`@bash_app` body, there is no `subprocess` anywhere, and the driver loads its own
Parsl runtime.

### Expressing dependencies — build the DAG, don't block it

Parsl is asynchronous by design: submitting an app returns immediately and the
DataFlowKernel works out the execution order from the futures you pass around.
Calling `.result()` between submissions throws that away and serializes the
driver by hand.

**The rule: submit every app first, then call `.result()` once, at the end, on the
final deliverable.**

```python
# WRONG -- blocks the driver, hides the DAG from Parsl
sim = run_sim(...)
sim.result()                                  # driver stalls here
info = analyze(work, snap_txt).result()       # ...and again here
png  = visualize(work, info).result()

# RIGHT -- one DAG, submitted up front, resolved once
sim  = run_sim(..., outputs=[File(snap_txt), File(halo_txt)])
info = analyze(work, inputs=sim.outputs)      # PENDING until those files exist
png  = visualize(work, info, inputs=sim.outputs)
print(png.result())                           # the only block in the file
```

Two ways to express a dependency, both non-blocking:

| Dependency | How |
|---|---|
| App B needs App A's **return value** | pass the `AppFuture` itself as an argument; Parsl substitutes the resolved value |
| App B needs a **file** App A wrote | A declares `outputs=[File(path)]`, B takes `inputs=A.outputs` |

`File` objects (`from parsl.data_provider.files import File`) are what make
file-level dependencies visible to Parsl. An app that declares
`outputs=[File(p)]` gets back one `DataFuture` per file on `future.outputs`, and
any app given those as `inputs=` is held in PENDING until the files are produced.
Raw path **strings** are invisible to the DFK — Parsl cannot order tasks it cannot
see, so string-only wiring forces you back into manual `.result()` blocking and
hand-rolled `os.path.exists` checks.

Inside an app body, read paths off the objects (`inputs[0].filepath`,
`outputs[0].filepath`) rather than re-deriving them, so the command writes exactly
the file Parsl is tracking.

#### Naming the files, so the wiring is readable

With more than one product, `inputs=A.outputs` hands the consumer a positional
list and the app has to know which index is which. Create the `File` objects in
`main()` and pass the individual `DataFuture`s by keyword instead — the
dependency is identical to Parsl, and the file each stage consumes is stated
rather than implied:

```python
def main():
    snap_file = File(f"{work}/particles_step{step}.txt")
    halo_file = File(f"{work}/haloproperties_step{step}.txt")

    # Producer declares both products. Order here fixes .outputs order.
    sim = run_sim(exe=exe, envfile=envfile, workdir=work, step=step,
                  outputs=[snap_file, halo_file])

    # Each consumer names the product it needs. Both are DataFutures, so both
    # stay PENDING until the producer has written that file.
    info = analyze(work,
                   snap_file=sim.outputs[0],
                   halo_file=sim.outputs[1],
                   paramfile=paramfile)

    # `info` is an AppFuture returning a dict. Passed as an ordinary argument,
    # Parsl resolves it and the body receives the dict itself -- that is a
    # value dependency, no `inputs=` needed and no `.result()` call.
    png = visualize(work, snap_file=sim.outputs[0], info=info)

    print(png.result())        # the only block in the file
```

The app bodies then take those as normal parameters and read
`snap_file.filepath`. Three stages, two kinds of edge (two files and one return
value), one `.result()`, and every dependency visible to the DataFlowKernel.

Note `sim.outputs[0]` appearing twice: a `DataFuture` can be consumed by any
number of apps, and they run concurrently once it resolves. Declaring the file
once and fanning out is correct; re-declaring the same path in a second
`outputs=` list is not, and makes two apps claim to produce it.

---

## When to Use This Skill

Load when generating or debugging Parsl workflow code. Covers the exact config and API patterns used in this project.

---

## Overview

Parsl wraps Python functions with `@python_app` to run them as managed tasks, and with
`@bash_app` to run command-line programs. Tasks return `AppFuture` objects; call
`.result()` to block and get the value. Workers run in separate processes — imports
must be inside function bodies.

---

## Config — Local (single node)

```python
from parsl.config import Config
from parsl.executors import HighThroughputExecutor
from parsl.providers import LocalProvider
import parsl

config = Config(
    executors=[
        HighThroughputExecutor(
            label="local_htex",
            max_workers_per_node=1,
            provider=LocalProvider(
                min_blocks=1,
                max_blocks=1,
                init_blocks=1,
            ),
        )
    ],
    strategy="none",
    initialize_logging=False,
)
parsl.load(config)
```

**`max_workers` is gone** -- it was removed from `HighThroughputExecutor` and
passing it raises `TypeError`. Its replacement is **`max_workers_per_node`,
which is valid and which you should use** (see the sizing section below). An
earlier version of this skill forbade both as if they were the same parameter;
they are not, and forbidding the second one is what left the only correct way
to size a worker pool unavailable.

**Set `initialize_logging=False`.** The default is `True`, which writes a
DEBUG-level `parsl.log` under the run directory -- including the scaling
strategy every 5 seconds, thousands of lines of `more slots than tasks` in a
long run. That belongs to development, not to a run whose output is a
scientific artifact, so keep it out of the generated file:

```python
config = Config(
    executors=[...],
    strategy="none",
    initialize_logging=False,
)
```

**When a DAG hangs or fails in a way the result does not explain, turn it back
on for one run.** `parsl.log` is the only record of task submission and
dependency resolution: whether an app was ever submitted, which executor it
went to, what it was waiting on, and the full traceback of a failed app body.
Without it a hang has no diagnosable trace from the outside. Remove the line,
reproduce, read the log, put the line back.

A middle setting exists if the file is wanted permanently but the chatter is
not -- leave the default and raise the level once after load, which keeps the
submission trail, the state transitions and every failure while dropping the
polling noise:

```python
import logging
parsl.load(config)
logging.getLogger("parsl").setLevel(logging.INFO)
```

### Sizing the worker for an MPI `@bash_app`

A `@bash_app` that launches `mpiexec -np N` runs **inside one worker slot**, and
that slot's `mpiexec` draws its ranks from `$PBS_NODEFILE`. So what you need is
**exactly one worker**, not a worker that somehow holds N cores.

```python
HighThroughputExecutor(
    label="htex",
    max_workers_per_node=1,       # exactly one worker: the thing that runs mpiexec
    provider=LocalProvider(min_blocks=1, max_blocks=1, init_blocks=1),
)
```

Leave `cores_per_worker` at its default of `1.0`.

**`cores_per_worker` is a divisor, not a reservation.** Parsl computes the
worker count as:

```
worker_count = min(max_workers_per_node, mem_slots, floor(cores_on_node / cores_per_worker))
```

so raising it *reduces* how many workers are spawned and reserves nothing. On a
128-core node, `cores_per_worker=8` yields **16 workers**, not one 8-core slot.
Worse, if it ever exceeds the cores the process can see, the floor division
gives **zero workers**: the pool starts, registers, dispatches nothing, and the
DAG hangs with no error anywhere.

Setting it to the rank count -- which an earlier version of this skill told you
to do -- is therefore wrong in both directions and was the cause of a real
hang. `max_workers_per_node=1` is the parameter that means "one slot".

If you want the worker's threads pinned to a core range, that is
`cpu_affinity="block"`, and it is unrelated to either of the above.

## Config — HPC (inside existing PBS job)

Scale workers to the number of allocated nodes. Do NOT submit new PBS jobs from within the agent.

```python
from parsl.config import Config
from parsl.executors import HighThroughputExecutor
from parsl.providers import LocalProvider
import parsl, os

n_workers = int(os.environ.get("PBS_NUM_NODES", 1))

config = Config(
    executors=[
        HighThroughputExecutor(
            label="htex_lcrc",
            max_workers_per_node=1, # one worker; mpiexec inside it uses $PBS_NODEFILE
            provider=LocalProvider(
                min_blocks=n_workers,
                max_blocks=n_workers,
                init_blocks=n_workers,
            ),
        )
    ],
    strategy="none",
    initialize_logging=False,
)
parsl.load(config)
```

`max_workers_per_node=1` gives exactly one worker, which is what a `@bash_app`
running `mpiexec -np N` needs: the ranks come from `$PBS_NODEFILE`, not from the
worker slot. Do not reach for `cores_per_worker` here — see "Sizing the worker
for an MPI `@bash_app`" above for why it is the wrong knob.

---

## @python_app Rules

```python
@python_app
def my_step(arg1, arg2):
    import os      # ALL imports inside function body
    import shutil  # workers don't share main namespace
    return result  # must be picklable
```

- All imports go inside the function body
- No closures over mutable outer state
- `.result()` only in `main()`, never inside another app
- Return values must be picklable (strings, ints, simple dicts)

---

## Nothing outside the function body exists on the worker

This applies to **both** decorators, and it is the single most common way a
generated file fails.

An app body runs in a worker process that has **never imported your file**. It
gets the function and its arguments and nothing else. So a name the body reads
but does not receive is not defined there:

- **Imports** -- `import numpy as np` at the top of the file is invisible.
  Import inside the body. (Stated above for `@python_app`; it is equally true
  for `@bash_app`.)
- **Module-level constants** -- a `NNODES = 2` at the top of the file is
  invisible. **Pass it as a parameter.**
- **Helper functions** -- a plain `def` at module level that the body calls is
  invisible. Inline it, or make it an app of its own.

```python
NNODES = 2
NRANKS = 4

# WRONG -- nranks arrives as an argument, NNODES does not
@bash_app
def run_sim(exe, nranks, ntotranks):
    return f"echo 'NNODES={NNODES}' && mpirun -np {ntotranks} ..."
#                         ^^^^^^ NameError: name 'NNODES' is not defined

# RIGHT -- every value the body reads is a parameter
@bash_app
def run_sim(exe, nnodes, nranks, ntotranks):
    return f"echo 'NNODES={nnodes}' && mpirun -np {ntotranks} ..."

run_sim(exe=EXE, nnodes=NNODES, nranks=NRANKS, ntotranks=NNODES * NRANKS)
```

**A default argument is fine**, because defaults are evaluated in the driver
when the `def` is executed and travel with the call:

```python
@bash_app
def run_sim(exe, nranks=NRANKS):      # OK -- NRANKS resolved at def time
    ...
```

That is why a file can reference a constant in a default and still fail on the
same constant used inside the body — the two look alike and behave completely
differently.

**What the failure looks like:** the task fails within seconds with
`NameError: name 'X' is not defined`, the traceback points at the line in your
file, and every downstream task goes to `dep_fail`. Nothing the app was meant
to launch ever runs. The `NameError` and its traceback are in
`task_logs/NNNN/task_NNNN_<app>.stderr`, and -- if `initialize_logging` was
left on for the run -- in `runinfo/NNN/parsl.log` as well.

---

## @bash_app Rules

Example of a bash app
```python
@bash_app
def echo(
    name: str,
    stdout=parsl.AUTO_LOGNAME  # Requests Parsl to return the stdout
):
    return f'echo "Hello, {name}!"'

future = echo('user')
future.result() # block until task has completed

with open(future.stdout, 'r') as f:
    print(f.read())
```

- All imports go inside the function body
- No closures over mutable outer state
- `.result()` only in `main()`, never inside another app
Inputs and Outputs
- Bash Apps can use the same kinds of inputs as Python Apps, but only communicate results with Files.
- Bash Apps, unlike Python Apps, can also return the content printed to the Standard Output and Error.
- If the Bash app exits with Unix exit code 0, then the AppFuture will complete. If the Bash app exits with any other code, Parsl will treat this as a failure, and the AppFuture will instead contain an BashExitFailure exception. The Unix exit code can be accessed through the exitcode attribute of that BashExitFailure.

---

## python app example



```python
@bash_app
def echo(
    name: str,
    stdout=parsl.AUTO_LOGNAME  # Requests Parsl to return the stdout
):
    return f'echo "Hello, {name}!"'

future = echo('user')
future.result() # block until task has completed

with open(future.stdout, 'r') as f:
    print(f.read())
```

- All imports go inside the function body
- No closures over mutable outer state
- `.result()` only in `main()`, never inside another app
- Return values must be picklable (strings, ints, simple dicts)

---

### Full example: HACC cosmology (simulation + analysis + visualization)

Three apps in **one generated file**: a `@bash_app` that runs the HACC N-body
simulation on 8 MPI ranks, a `@python_app` that picks the target halo from the
halo catalog, and a second `@bash_app`/`@python_app` pair that extracts just the
particles in the slice and renders it.

There is no `.sh` and no `.pbs` file anywhere — the whole `mpiexec` invocation is a
string returned by `run_hacc`. HACC's own halo finder writes the FOF/SOD catalog
during the run, so the analysis app parses that catalog rather than reimplementing
FOF/SOD.

#### Why the data moves the way it does — read before copying

`GenericIOPrint` is the only available reader: `pygio` is not built, and
`import subprocess` keeps the work out of Parsl entirely, so every read of GenericIO data has
to be a CLI call inside a `@bash_app`. That tool has exactly three flags
(`--no-rank-info`, `--no-data`, `--show-map`) — **no column selection and no
binary output** — so a text dump is forced. What is *not* forced is dumping
everything and filtering in Python.

Measured on the 64³ sample (`step_624`, 262,144 particles):

| Approach | Intermediate | Parse | Array | RSS |
|---|---|---|---|---|
| dump all 10 columns, parse 7 as float64, slice in Python | 27.2 MB text | 0.98 s | 14.7 MB | 132 MB |
| stream through `awk`, keep x,y in the slice, parse float32 | 0.5 MB text | 0.009 s | 0.16 MB | 26 MB |

Same image, ~100x less intermediate data. The 4 Mpc/h slice keeps 20,283 of
262,144 particles, so filtering after the parse pays the full cost for 8% of the
rows. This sample is a toy; the naive path scales to a ~1.7 TB text file at 1024³.

Three rules follow, and the example below applies all of them:

1. **Filter in the stream, not in Python.** Pipe `GenericIOPrint` straight into
   `awk` and emit only the rows and columns the renderer needs. Never write the
   full dump to disk as an intermediate.
2. **Only the columns you use.** The snapshot carries
   `x,y,z,vx,vy,vz,phi,id,mask,status`; a density slice needs `x,y` (with `z` used
   for the cut, not kept). Do not parse velocities and potential to discard them.
3. **float32, and `np.array(f.read().split())` over `np.loadtxt`.** The data is
   f32 on disk, so float64 doubles memory for no precision. On the filtered slice,
   `split()` + `np.array` measured 0.009 s against 0.647 s for `np.loadtxt` — 70x.

The slice cut needs the halo's `z`, which comes from the catalog, so the DAG is
necessarily: simulate + dump the (small, 680 KB) catalog → pick the halo in Python
→ dump the particles already filtered to that halo's slice → render.

```python
import os
import parsl
from parsl import bash_app, python_app
from parsl.config import Config
from parsl.data_provider.files import File
from parsl.executors import HighThroughputExecutor
from parsl.providers import LocalProvider

NRANKS = 8          # MPI ranks for HACC; also sizes the HTEX worker slot
NTHREADS = 4        # OMP threads per rank
THICKNESS = 4.0     # slice thickness in Mpc/h
NBINS = 256


@bash_app
def run_hacc(exe, envfile, paramfile, giop, workdir, srcdir, step,
             nnodes=1, nranks=NRANKS, ndepth=4, nthreads=NTHREADS,
             inputs=(), outputs=(),
             stdout=parsl.AUTO_LOGNAME, stderr=parsl.AUTO_LOGNAME):
    """Simulation: HACC on 8 MPI ranks, then dump ONLY the halo catalog.

    Returns the COMMAND STRING -- Parsl runs it, we never do. The rank/thread
    arithmetic happens in Python so the string carries no shell variables.

    The particle snapshot is deliberately NOT dumped here: it is 27 MB of text
    for this toy run and we need less than 1 MB of it. It gets dumped in
    extract_slice, after the halo z is known, already filtered.

    Always read the MASTER file (m000p-<step>.haloproperties), never a per-rank
    shard: GenericIOPrint reads across all 8 ranks for you.

    outputs[0] = halo catalog text dump (~680 KB, dumped whole -- it is small)
    """
    ntotranks = nnodes * nranks
    # srcdir = where the program keeps its own trees; workdir = where we run.
    halo = f"{workdir}/analysis/haloproperties/step_{step}/m000p-{step}.haloproperties"
    halo_txt = outputs[0].filepath
    return (
        f"set -e && "
        # The work directory, not the sample/reference tree -- see above.
        f"cd {workdir} && "
        f"source {envfile} && "
        f"echo 'NNODES={nnodes} NTOTRANKS={ntotranks} "
        f"NRANKS={nranks} NDEPTH={ndepth} NTHREADS={nthreads}' && "
        f"mpiexec -np {ntotranks} "
        f"--map-by ppr:{nranks}:node:PE={nthreads} "
        f"--bind-to core "
        f"-x OMP_NUM_THREADS={nthreads} "
        f"{exe} {paramfile} && "
        f"{giop} --no-rank-info {halo} > {halo_txt}"
    )


@python_app
def select_halo(outdir, paramfile, inputs=()):
    """Analysis: read run config, pick the most massive SOD halo.

    Returns a small picklable dict -- the slice center and the cosmology the
    renderer needs. inputs[0] is the catalog DataFuture from run_hacc.
    """
    import os
    import numpy as np

    halo_txt = inputs[0].filepath
    os.makedirs(outdir, exist_ok=True)

    # --- RL, NP, Omega_m from HACC's own indat.params (this run is downscaled,
    #     so never hardcode values from the paper)
    pv = {}
    with open(paramfile) as f:
        for ln in f:
            s = ln.strip()
            if s and not s.startswith("#"):
                parts = s.split()
                if len(parts) >= 2:
                    pv[parts[0]] = parts[1]
    RL = float(pv["RL"])
    NP = float(pv["NP"])
    hub = float(pv["HUBBLE"])
    omega_m = (float(pv["OMEGA_CDM"])
               + float(pv["DEUT"]) / (hub * hub)       # Omega_b from Omega_b h^2
               + float(pv.get("OMEGA_NU", "0.0")))
    rho_crit0 = 2.77536627e11                          # h^2 Msun / Mpc^3
    mp = omega_m * rho_crit0 * (RL / NP) ** 3          # mass is NOT a column

    # --- catalog: parse the '#' header for column order, never assume indices.
    #     Only ~700 KB, so reading it whole is fine.
    header, rows = None, []
    with open(halo_txt) as f:
        for ln in f:
            s = ln.strip()
            if not s:
                continue
            if s.startswith("#"):
                cols = s.lstrip("#").strip().split()
                if "sod_halo_mass" in cols:
                    header = cols
                continue
            if header is not None:
                parts = s.split()
                if len(parts) >= len(header):
                    rows.append(parts[:len(header)])
    if header is None:
        raise RuntimeError("no halo catalog header line found")
    cat = np.array(rows, dtype=np.float32)
    col = {name: i for i, name in enumerate(header)}

    # most massive halo = max sod_halo_mass (M_200c); sod_halo_count == -101
    # marks halos SOD was never computed for, so drop them before the argmax
    valid = np.flatnonzero(cat[:, col["sod_halo_count"]] != -101)
    best = valid[np.argmax(cat[valid, col["sod_halo_mass"]])]

    info = {
        "RL": RL, "NP": NP, "omega_m": omega_m, "mp": mp,
        "m200c": float(cat[best, col["sod_halo_mass"]]),
        "r200c": float(cat[best, col["sod_halo_radius"]]),
        "hx": float(cat[best, col["fof_halo_center_x"]]),
        "hy": float(cat[best, col["fof_halo_center_y"]]),
        "hz": float(cat[best, col["fof_halo_center_z"]]),   # slice center
        "nhalos": int(cat.shape[0]), "nsod": int(valid.size),
    }

    with open(os.path.join(outdir, "most_massive_halo.txt"), "w") as f:
        f.write(f"M_200c = {info['m200c']:.6e} Msun/h\n")
        f.write(f"R_200c = {info['r200c']:.6e} Mpc/h\n")
        f.write(f"center = ({info['hx']:.6f}, {info['hy']:.6f}, "
                f"{info['hz']:.6f}) Mpc/h\n")
    return info


@bash_app
def extract_slice(giop, workdir, step, info, thickness=THICKNESS,
                  inputs=(), outputs=(),
                  stdout=parsl.AUTO_LOGNAME, stderr=parsl.AUTO_LOGNAME):
    """Pipe the snapshot through awk: keep x,y for particles in the z-slice only.

    `info` is select_halo's AppFuture; Parsl resolves it to the dict before this
    app runs, so the halo z is available to build the command string.

    GenericIOPrint cannot select columns, but its stdout can be filtered before
    it ever reaches disk. awk does the periodic-z cut and prints 2 of 10 columns:
    27 MB of text becomes ~0.5 MB, and nothing large is ever written or parsed.
    Note the doubled braces -- {{ }} -- which is how an awk program survives an
    f-string.

    outputs[0] = two-column x,y text for the particles inside the slice
    """
    snap = f"{workdir}/output/full_snapshots/step_{step}/m000p.full.mpicosmo.{step}"
    out = outputs[0].filepath
    hz = info["hz"]
    rl = info["RL"]
    half = thickness / 2.0
    return (
        # pipefail, because `set -e` alone ignores a failure on the LEFT of a
        # pipe: if the reader dies, awk still exits 0 and the step looks fine.
        f"set -eo pipefail && "
        f"{giop} --no-rank-info {snap} | "
        f"awk -v hz={hz} -v half={half} -v rl={rl} "
        f"'!/^#/ && NF>=7 {{ dz=$3-hz; if(dz<0) dz=-dz; "
        f"if(rl-dz<dz) dz=rl-dz; "          # periodic distance in z
        f"if(dz<=half) print $1\"\\t\"$2 }}' > {out}"
    )


@python_app
def render(outdir, info, thickness=THICKNESS, nbins=NBINS, inputs=()):
    """Visualization: mass-weighted 2D histogram of the pre-filtered slice."""
    import os
    import numpy as np
    import matplotlib
    matplotlib.use("Agg")            # headless compute node, no display
    import matplotlib.pyplot as plt
    from matplotlib.colors import LogNorm

    slice_txt = inputs[0].filepath
    os.makedirs(outdir, exist_ok=True)

    RL, mp = info["RL"], info["mp"]
    hx, hy, hz, r200c = info["hx"], info["hy"], info["hz"], info["r200c"]

    # Already filtered to the slice and to 2 columns, so this is a flat read.
    # split() + np.array beats np.loadtxt by ~70x and stays float32 end to end.
    with open(slice_txt) as f:
        xy = np.array(f.read().split(), dtype=np.float32).reshape(-1, 2)
    x, y = np.mod(xy[:, 0], RL), np.mod(xy[:, 1], RL)

    edges = np.linspace(0.0, RL, nbins + 1, dtype=np.float32)
    counts, _, _ = np.histogram2d(x, y, bins=[edges, edges])
    cell = RL / nbins
    # every particle has the same mass, so histogram the counts and scale once
    # instead of passing a weights array as long as x
    sigma = (counts * (mp / (cell * cell))).astype(np.float32)

    np.save(os.path.join(outdir, "density_slice.npy"), sigma)

    pos = sigma[sigma > 0]
    fig, ax = plt.subplots(figsize=(7, 6))
    im = ax.imshow(sigma.T, origin="lower", extent=[0, RL, 0, RL],
                   norm=LogNorm(vmin=pos.min() if pos.size else 1e-8,
                                vmax=sigma.max() if sigma.max() > 0 else 1.0),
                   cmap="inferno")
    ax.add_patch(plt.Circle((hx, hy), r200c, fill=False, ec="cyan", lw=1.4))
    ax.plot(hx, hy, "+", color="cyan", ms=12, mew=1.5)
    ax.set_xlabel("x [Mpc/h]")
    ax.set_ylabel("y [Mpc/h]")
    ax.set_title(f"DM density, {thickness:g} Mpc/h slice at z = {hz:.2f} Mpc/h")
    fig.colorbar(im, ax=ax, label=r"$\Sigma$ [$h^{-1}M_\odot$ / (Mpc/h)$^2$]")
    png = os.path.join(outdir, "dm_density_slice.png")
    fig.savefig(png, dpi=150, bbox_inches="tight")
    plt.close(fig)

    with open(os.path.join(outdir, "summary.txt"), "w") as f:
        f.write("HACC dark-matter density slice\n")
        f.write(f"box RL             = {RL} Mpc/h\n")
        f.write(f"NP (1d)            = {int(info['NP'])}\n")
        f.write(f"Omega_m            = {info['omega_m']:.6f}\n")
        f.write(f"particle mass mp   = {mp:.6e} Msun/h\n")
        f.write(f"halos in catalog   = {info['nhalos']} ({info['nsod']} with SOD)\n")
        f.write(f"most massive M_200c= {info['m200c']:.6e} Msun/h\n")
        f.write(f"most massive R_200c= {info['r200c']:.6e} Mpc/h\n")
        f.write(f"halo center        = ({hx:.4f}, {hy:.4f}, {hz:.4f}) Mpc/h\n")
        f.write(f"slice center z     = {hz:.4f} Mpc/h, thickness {thickness:g} Mpc/h\n")
        f.write(f"particles in slice = {x.size}\n")
        f.write(f"grid               = {nbins} x {nbins}\n")
        f.write(f"image              = {png}\n")
    return png


def main():
    work = "/app/work/run0"
    # Inputs are read from wherever they live; the run happens in work/run0.
    srcdir = "/path/to/the/programs/own/sample/tree"   # read-only, inputs only
    hacc = "/lcrc/project/PEDAL/jalemu/HACC/HACC_go"
    giop = f"{hacc}/improv.cpu/frontend/bin/GenericIOPrint"
    step = 624

    config = Config(
        executors=[
            HighThroughputExecutor(
                label="hacc_htex",
                # the mpiexec -np 8 app runs inside ONE worker slot -- size the
                # slot to the rank count or the ranks fight over a single core
                max_workers_per_node=1,
                provider=LocalProvider(min_blocks=1, max_blocks=1, init_blocks=1),
            )
        ],
        strategy="none",
        initialize_logging=False,
    )
    parsl.load(config)
    try:
        os.makedirs(work, exist_ok=True)

        # Submit the whole DAG without blocking. Each app is wired to the next by
        # a future or a DataFuture, so Parsl -- not the driver -- orders them.
        sim = run_hacc(
            exe=f"{hacc}/improv.cpu/mpi/bin/hacc_tpm",
            envfile=f"{hacc}/env/bashrc.improv.cpu",
            paramfile="./params/indat.params",
            giop=giop, workdir=work, srcdir=srcdir, step=step, nranks=NRANKS,
            outputs=[File(f"{work}/haloproperties_step{step}.txt")],
        )

        info = select_halo(work, f"{srcdir}/params/indat.params",
                           inputs=sim.outputs)

        sl = extract_slice(giop, work, step, info,         # AppFuture -> resolved dict
                           outputs=[File(f"{work}/slice_xy_step{step}.txt")])

        png = render(work, info, inputs=sl.outputs)

        # the only .result() in the file -- resolves the whole DAG
        print(f"wrote {png.result()}")
    finally:
        parsl.clear()


if __name__ == "__main__":
    main()
```

Files this workflow produces in `/app/work/run0/`:

| File | Written by | Size (64³ sample) | Contents |
|---|---|---|---|
| `haloproperties_step<N>.txt` | `run_hacc` | ~680 KB | full FOF/SOD catalog dump |
| `most_massive_halo.txt` | `select_halo` | <1 KB | selected halo's M_200c, R_200c, center |
| `slice_xy_step<N>.txt` | `extract_slice` | ~0.5 MB | x,y of the particles inside the slice |
| `density_slice.npy` | `render` | 256 KB | projected surface-density grid |
| `dm_density_slice.png` | `render` | — | final rendered image, the scientific output |
| `summary.txt` | `render` | <1 KB | config values used and derived results |

Domain values to get right: mass is **not** a snapshot column (compute
`mp = Omega_m * rho_crit0 * (RL/NP)**3`), the "most massive halo" is max
`sod_halo_mass` with `sod_halo_count == -101` rows excluded, and `RL`/`NP`/`Omega_m`
come from HACC's own `indat.params`, not from a paper.

Structural details worth copying: every inter-app dependency is a `File`/
`DataFuture` or an `AppFuture` passed as an argument, there is exactly one
`.result()` and it is on the final PNG, the executor declares
`max_workers_per_node=1`, and the large intermediate is never materialized — the only full-size data
that moves is `GenericIOPrint`'s stdout, consumed by `awk` in the same pipe.

> If a binary path is available, prefer it over text entirely. `GenericIO2Cosmo
> <mpiioName> <cosmoName> <rank0> [rank1 ...]` sits in the same `bin/` and writes
> per-rank `.cosmo` binary files readable with `np.fromfile`, skipping the text
> round-trip. It is not exercised end-to-end here — it takes an explicit rank
> list and emits one file per rank, so confirm the record layout before relying
> on it.

## Common Pitfalls

| Pitfall | Rule |
|---|---|
| Looking for a tool that runs a code or command string | There isn't one. Use `write_file` + `run_workflow` |
| Asserting on the simulation's own output (`test -s snapshot`) | Don't. Output from a previous run satisfies it, so a run that wrote nothing passes. Verify afterwards with `list_files`/`read_file`, which can see timestamps and sizes |
| Chaining a pipe under plain `set -e` | `set -e` ignores failure on the LEFT of a pipe, so a dead reader still exits 0. Use `set -eo pipefail` |
| Running a CLI command any way other than `@bash_app` | Every command line must be a string returned by a `@bash_app` |
| `import subprocess` / `os.system` in the generated file | Takes the work out of Parsl — express it as a `@bash_app` |
| `mpirun`/`srun` in the driver body instead of inside a `@bash_app` | Parsl never sees the launch — move it into the app body |
| Writing a `.sh` or `.pbs` file to run the work | Never. The command belongs in a `@bash_app` string |
| Splitting a workflow across multiple generated files | One file per workflow; multiple apps inside it |
| Imports at module level inside @python_app | All imports must be inside the function body |
| Calling `.result()` inside an app | Deadlocks the worker — only call in main() |
| `.result()` between submissions to force ordering | Serializes the driver and hides the DAG. Submit every app first; call `.result()` once, on the final deliverable |
| Passing file paths between apps as plain strings | Invisible to the DFK. Declare `outputs=[File(p)]` on the producer and pass `inputs=producer.outputs` to the consumer |
| Hand-written `os.path.exists` checks on another app's output | A symptom of string wiring — with `File`/`DataFuture` deps Parsl guarantees the file before the app starts |
| A module-level constant used INSIDE an app body | `NameError` on the worker -- it never imported your file. Pass it as a parameter. A constant in a DEFAULT argument is fine; one read in the body is not |
| A module-level helper function called from an app body | Same `NameError`. Inline it or make it its own app |
| `cores_per_worker=N` to "size a slot for N ranks" | It is a DIVISOR for the worker count, not a reservation. On 128 cores it gives 16 workers; above the visible core count it gives ZERO and the DAG hangs silently. Use `max_workers_per_node=1` |
| Leaving `initialize_logging` at its default in a real run | Writes a DEBUG `parsl.log` with 5-second scaling chatter into the run directory. Set `initialize_logging=False`; turn it back on only to diagnose a hang |
| `--map-by ppr:N:node` with N != the allocation's `mpiprocs` | "not enough slots available". The ranks-per-node in the layout must equal what the allocation reserved per node -- 1x8 and 2x4 are both 8 ranks and are not interchangeable |
| `rm -rf` on a directory the app also reads from or writes into | A retry wipes the previous attempt's output before you can read it, and one wrong path deletes the inputs. Create with `mkdir -p` and remove only the specific subdirectory being replaced |
| Extra kwargs in HighThroughputExecutor | Causes TypeError — copy config exactly |
| `strategy` not set to `"none"` | Can cause auto-scaling issues in local mode |
| `parsl.load()` called twice without `parsl.clear()` | Raises NoDataFlowKernelError |
| `WorkerLost` error | Worker process crashed — check stderr for the actual exception |
| Simulation app invoked more than once per run | Call the simulation `@bash_app` exactly once per workflow invocation — re-invoking (e.g. on replan or retry) silently produces duplicate runs instead of reusing the result |
| Rendering without `matplotlib.use("Agg")` | Headless nodes have no display; set the backend inside the `@python_app` |

---

## Lifecycle

```python
parsl.load(config)      # call once at startup
# ... submit tasks ...
future = my_app(args).result()   # blocks until done
parsl.clear()           # call at end of main() to release workers
```

---

## AppFuture API

```python
future = my_app(args)
value = future.result()     # block and get return value
exc   = future.exception()  # returns exception or None
done  = future.done()       # bool, non-blocking
```