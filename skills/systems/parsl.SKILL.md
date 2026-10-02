
---
name: systems/parsl
description: >
  Parsl parallel scripting library reference for MAW workflows. Covers the generated-file
  execution model (write_workflow/run_workflow), @python_app/@bash_app rules,
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
| `write_workflow(python_code, filename)` | Writes ONE standalone Parsl driver file to `/app/work/run0/` |
| `run_workflow(filename)` | The server executes that file |

There is **no `submit_task`, no `submit_shell_task`, no `submit_mpi_task`, and no
`run_lammps`** on this server. They were removed. Do not try to call them.

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

### What is rejected

`write_workflow` validates the file and refuses it if it:

- defines no `@bash_app` and no `@python_app`
- never calls `parsl.load(...)`
- imports or calls `subprocess`, or calls `os.system` / `os.popen`
- contains `mpirun` / `srun` / `mpiexec` **outside** a `@bash_app` body

If it is rejected, read the returned `errors`, fix the file, and call
`write_workflow` again. Do not look for another way to run the command.

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
def run_sim(exe, envfile, paramfile, ranks, rundir,
            inputs=(), outputs=(),
            stdout=parsl.AUTO_LOGNAME, stderr=parsl.AUTO_LOGNAME):
    # Return the COMMAND STRING. Parsl runs it; we never do.
    # Products are declared as outputs=[File(...)]; read their paths off the
    # File objects so the command writes exactly what Parsl is tracking.
    snapshot = outputs[0].filepath
    return (
        f"source {envfile} && "
        f"cd {rundir} && "
        f"mpirun -n {ranks} {exe} {paramfile} && "
        f"test -s {snapshot}"
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
                cores_per_worker=RANKS,   # one worker slot wide enough for the MPI job
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
                      paramfile="/path/to/indat.params", ranks=RANKS, rundir=work,
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
            cores_per_worker=1,
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

**CRITICAL:** Do NOT add `max_workers`, `max_workers_per_node`, or any kwargs not shown. They cause `TypeError` in recent Parsl versions.

`initialize_logging=False` disables Parsl's automatic `parsl.log` file logging. The default (`True`) writes DEBUG-level output -- including per-5s scaling-strategy chatter -- that isn't useful here and just clutters the run directory.

### Sizing the worker for an MPI `@bash_app`

`cores_per_worker=1` is correct only when every app is single-core. A `@bash_app`
that launches `mpiexec -np N` runs **inside one worker slot**, so a 1-core slot
tries to host N ranks: on an HPC cluster that means core oversubscription,
affinity-mask conflicts, and rank-placement failures.

Set `cores_per_worker` to the rank count the MPI app launches:

```python
NRANKS = 8

HighThroughputExecutor(
    label="htex",
    cores_per_worker=NRANKS,      # the mpiexec -np 8 app gets a slot 8 cores wide
    provider=LocalProvider(min_blocks=1, max_blocks=1, init_blocks=1),
)
```

Use the same `NRANKS` constant for `cores_per_worker` and for the `-np` value in
the app, so the two cannot drift apart. If ranks are multithreaded, size the slot
to `nranks * nthreads`. Note this also caps concurrency: a node with 8 usable
cores and `cores_per_worker=8` runs one such app at a time, which is what you want
for a single large MPI job.

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
            cores_per_worker=1,     # raise to the rank count if an app runs mpiexec
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

`cores_per_worker=1` here assumes single-core apps. If any `@bash_app` launches
`mpiexec -np N`, size the slot for it — see "Sizing the worker for an MPI
`@bash_app`" above — otherwise the N ranks land in one 1-core slot.

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
`write_workflow` rejects `import subprocess`, so every read of GenericIO data has
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
def run_hacc(exe, envfile, paramfile, giop, rundir, step,
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
    halo = f"{rundir}/analysis/haloproperties/step_{step}/m000p-{step}.haloproperties"
    halo_txt = outputs[0].filepath
    return (
        f"set -e && "
        f"cd {rundir} && "
        f"source {envfile} && "
        f"echo 'NNODES={nnodes} NTOTRANKS={ntotranks} "
        f"NRANKS={nranks} NDEPTH={ndepth} NTHREADS={nthreads}' && "
        f"mpiexec -np {ntotranks} "
        f"--map-by ppr:{nranks}:node:PE={nthreads} "
        f"--bind-to core "
        f"-x OMP_NUM_THREADS={nthreads} "
        f"{exe} {paramfile} -n && "
        f"{giop} --no-rank-info {halo} > {halo_txt} && "
        f"test -s {halo_txt}"
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
def extract_slice(giop, rundir, step, info, thickness=THICKNESS,
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
    snap = f"{rundir}/output/full_snapshots/step_{step}/m000p.full.mpicosmo.{step}"
    out = outputs[0].filepath
    hz = info["hz"]
    rl = info["RL"]
    half = thickness / 2.0
    return (
        f"set -e && "
        f"{giop} --no-rank-info {snap} | "
        f"awk -v hz={hz} -v half={half} -v rl={rl} "
        f"'!/^#/ && NF>=7 {{ dz=$3-hz; if(dz<0) dz=-dz; "
        f"if(rl-dz<dz) dz=rl-dz; "          # periodic distance in z
        f"if(dz<=half) print $1\"\\t\"$2 }}' > {out} && "
        f"test -s {out}"
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
    rundir = "/lcrc/project/PEDAL/jalemu/HACC/SampleRun_go"
    hacc = "/lcrc/project/PEDAL/jalemu/HACC/HACC_go"
    giop = f"{hacc}/improv.cpu/frontend/bin/GenericIOPrint"
    step = 624

    config = Config(
        executors=[
            HighThroughputExecutor(
                label="hacc_htex",
                # the mpiexec -np 8 app runs inside ONE worker slot -- size the
                # slot to the rank count or the ranks fight over a single core
                cores_per_worker=NRANKS,
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
            giop=giop, rundir=rundir, step=step, nranks=NRANKS,
            outputs=[File(f"{work}/haloproperties_step{step}.txt")],
        )

        info = select_halo(work, f"{rundir}/params/indat.params",
                           inputs=sim.outputs)

        sl = extract_slice(giop, rundir, step, info,       # AppFuture -> resolved dict
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
`.result()` and it is on the final PNG, `cores_per_worker` equals the MPI rank
count, and the large intermediate is never materialized — the only full-size data
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
| Reaching for `submit_task`/`submit_shell_task`/`submit_mpi_task`/`run_lammps` | They do not exist on this server. Use `write_workflow` + `run_workflow` |
| Running a CLI command any way other than `@bash_app` | Every command line must be a string returned by a `@bash_app` |
| `import subprocess` / `os.system` in the generated file | Rejected by `write_workflow` — express it as a `@bash_app` |
| `mpirun`/`srun` in the driver body instead of inside a `@bash_app` | Rejected by `write_workflow` — move it into the app body |
| Writing a `.sh` or `.pbs` file to run the work | Never. The command belongs in a `@bash_app` string |
| Splitting a workflow across multiple generated files | One file per workflow; multiple apps inside it |
| Imports at module level inside @python_app | All imports must be inside the function body |
| Calling `.result()` inside an app | Deadlocks the worker — only call in main() |
| `.result()` between submissions to force ordering | Serializes the driver and hides the DAG. Submit every app first; call `.result()` once, on the final deliverable |
| Passing file paths between apps as plain strings | Invisible to the DFK. Declare `outputs=[File(p)]` on the producer and pass `inputs=producer.outputs` to the consumer |
| Hand-written `os.path.exists` checks on another app's output | A symptom of string wiring — with `File`/`DataFuture` deps Parsl guarantees the file before the app starts |
| `cores_per_worker=1` with an `mpiexec -np N` bash app | N ranks crammed into a 1-core slot: oversubscription and affinity failures. Set `cores_per_worker=N` (or `N*threads`) |
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
