---
name: systems/pycompss
description: >
  PyCOMPSs reference for MAW workflows. Covers the generated-file execution model
  (write_file/run_workflow), the inherited MCP tool surface, @task/@binary/@mpi decorators,
  how the server supplies the topology and the environment, why a completed run is not a
  successful one, and a complete HACC cosmology example.
---

# PyCOMPSs — System Skill

PyCOMPSs is a task-based parallel programming model from BSC (Barcelona
Supercomputing Center). Work is expressed as `@task`-decorated functions and
scheduled by the COMPSs runtime, which detects dependencies automatically from
task parameters.

Load this skill when generating or debugging PyCOMPSs workflow code.

---

## READ THIS FIRST: You Write ONE File, and Only That File

The pycompss engine is **generated-file only**. Two tools carry the work:

| Tool | What it does |
|---|---|
| `write_file(path, content)` | Writes the ONE standalone PyCOMPSs driver |
| `run_workflow(filename)` | Launches it as `<python> -m pycompss ... workflow.py` |

**Do not write any other file.** In particular, do not write `project.xml` or
`resources.xml` — the server generates the topology from the real allocation
before every run. See *The Topology Is Not Yours*.

Seven more tools are inherited and behave identically on every engine:
`read_file`, `list_files`, `check_package`, `install_package`, `get_status`,
`get_resources`, `cleanup`. See **MCP Server Behavior** for what each actually
guarantees — several do more than their names suggest, and knowing that saves a
dozen calls per run.

**You never execute anything yourself.** No subprocess, no direct CLI call, no
PBS script, no `mpirun` typed by you.

### The rule

Every unit of work in the generated file is a decorated function:

- **`@task`** — pure Python work: analysis, filtering, plotting, summaries.
- **`@binary(binary="...")` stacked above `@task`** — a command-line program.
- **`@mpi(...)` above `@task`** — an MPI program on N ranks. Four arguments are
  mandatory in practice, and each has caused a `TOTALLY FAILED` task here:
  `runner=` an absolute path (not `"mpirun"`), `processes_per_node=` (it
  defaults to 1, which reads `processes` as a NODE count), `args=` naming only
  the real command-line arguments (every other parameter is appended to argv),
  and `fail_by_exit_value=True`.

All of them live in the **same file**. A simulation plus its visualization is one
file containing an `@mpi` task and a `@task` — not two files, not two tool calls.

### What `write_file` checks

A Python file is parsed with `ast.parse` before being written, so a **syntax
error** comes back as `"status": "rejected"` and nothing reaches disk.

Structural problems come back as `warnings` on a file that *was* written: no
`@task`, nothing imported from `pycompss`, no `main()`/`__main__` guard, MPI
oversubscription, or `COMPSS_MPIRUN_TYPE` set inside the file. Warnings do not
block `run_workflow`. Fix them anyway.

Not checked, and still forbidden:

- importing or calling `subprocess`, `os.system`, `os.popen`
- `@binary`/`@mpi` without `@task` beneath it — COMPSs ignores the decorator
  and the function becomes an ordinary one that does nothing

A path that resolves outside the work directory is **rejected**, not relocated.
`/app/work/run0` and `/app/data` are aliases the server rewrites on write, so
writing those literal strings into generated code is correct and portable.

---

## Runtime Lifecycle: the Launcher Owns It

`run_workflow` runs your file through `<python> -m pycompss`, which starts the
runtime, executes the application inside it, and shuts it down.

**Your file must NOT call `compss_start()` or `compss_stop()`.** Give it a
`main()` and an `if __name__ == "__main__":` guard instead.

### `-m pycompss` is the whole ballgame

Running the same file as plain `python workflow.py` **executes it sequentially,
ignoring every PyCOMPSs import, decorator and API call.** It does not error. It
exits 0. It produces the right numbers, slowly, having parallelised nothing.
This is a documented upstream mode, which is exactly why it is dangerous: it is
a legitimate way to run the file, and it is not the run you are claiming to have
performed. `run_workflow` returns the exact `launch_command` — check that
`-m pycompss` is in it on every run.

---

## The Topology Is Not Yours

COMPSs does not read the allocation from PBS. It reads it from
`project.xml`/`resources.xml`, and reaches the hosts named there over SSH. **The
server builds both files from `PBS_NODEFILE` plus a live SSH probe before every
run.** You do not write them, and you do not need to understand the schema.

What you do need is one number. Call `get_resources` and read:

```
compss_topology.total_slots      # the ceiling for @mpi(processes=N)
compss_topology.units_per_host   # what to pass as processes_per_node
compss_topology.mode             # "single_node" or "multi_node"
compss_topology.probe            # per-host: can a worker actually start there
```

**`processes=N` is a resource constraint.** If N exceeds `total_slots`, the task
is never scheduled — COMPSs reports "not enough slots available", or simply
hangs until the timeout. Three legitimate responses, and one that is not:

- N fits → proceed.
- N does not fit and the science can be downscaled → reduce N and **say so in
  your summary**, naming the rank count you actually ran.
- N does not fit and cannot be reduced → report the gap and stop. The plan's
  `reason` and `probe` fields say which hosts could not start a worker and
  why; that is an environment problem, not a workflow problem.
- **`total_slots: 0` means no task of any kind can run.** Report it and stop —
  do not write a workflow, and do not retry. The `probe` field holds the
  diagnosis (usually SSH, or `JAVA_HOME` not reaching the worker).
- **Never oversubscribe.** Setting `OMPI_MCA_rmaps_base_oversubscribe=1` or any
  equivalent silences the refusal by stacking ranks onto fewer slots than they
  need. It wastes the rest of the allocation, changes the numbers, and still
  reports success. `write_file` warns when it sees this.

`write_xml` still exists, as an override for a demonstrably wrong generated
topology. Using it is a last resort you must justify in your summary.

---

## The Environment Is Not Yours Either

A simulation binary that needs `source .../bashrc.<platform>` before it runs has
**no in-file place to put it**, because `@mpi(binary=...)` names an executable
and COMPSs builds the argv — there is no shell. That is handled outside the
workflow: `setup_hpc.sh` sources the platform environment, and the server's
`launch_env()` passes it to the runtime and sets `COMPSS_MPIRUN_TYPE`.

**There is no decorator parameter for this.** COMPSs 3.4 has no `env_script`,
`env_file` or equivalent — the complete decorator label set has been checked and
contains nothing of the kind. Do not go looking for one.

So:

- **Do not set environment variables in the generated file.** By the time your
  module is imported, the JVM has already copied its environment; an
  `os.environ` assignment arrives too late to reach the runtime or the `mpirun`
  it spawns. It is dead code that looks load-bearing.
- If a variable the binary needs is missing, that is an environment gap to
  **report**, exactly like a missing binding.

`working_dir=` on `@binary`/`@mpi` is yours, and matters: a program that writes
relative to its cwd must launch from a directory the run owns and can write to.

---

## `@binary` — Command-Line Programs

The decorated function has a `pass` body. Arguments come from the **function
signature**; their roles are declared in the `@task` decorator.

```python
from pycompss.api.task import task
from pycompss.api.binary import binary
from pycompss.api.parameter import FILE_IN, FILE_OUT_STDOUT, Type

@binary(binary="/path/to/GenericIOPrint", working_dir=WORK,
        fail_by_exit_value=True)
@task(gio_file=FILE_IN, out_txt={Type: FILE_OUT_STDOUT})
def gio_print(flag, gio_file, out_txt):
    pass

gio_print("--no-rank-info", catalog, "catalog.txt")
# runs:  GenericIOPrint --no-rank-info <catalog> > catalog.txt
```

**Prefer the bare form.** `@task(paramfile=FILE_IN)` and
`@task(paramfile={Type: FILE_IN})` are the same thing; the first needs no `Type`
import. The dict form is only needed for `FILE_OUT_STDOUT`, as above.

| Need | How |
|---|---|
| Positional argument | A plain function parameter; inserted in signature order |
| A flag | A plain string parameter, e.g. `gio_print("--no-rank-info", ...)` |
| Redirect stdout to a file | `{Type: FILE_OUT_STDOUT}` on that parameter |

Both `FILE_OUT_STDOUT` and the longer `{Type: FILE_OUT, StdIOStream: STDOUT}`
form work in 3.4; use whichever reads better.

**Set `fail_by_exit_value=True` on every `@binary`/`@mpi` task.** The default
returns the exit code as the task's *return value* and the task counts as
completed — so a simulation that dies after `MPI_Init` flows straight through to
a consumer reading a stale file from a previous run.

### The producer MUST declare its outputs as `FILE_OUT`

COMPSs builds the graph from **parameters only**. It cannot see inside a binary.

```python
# WRONG -- no edge. COMPSs may run these at the same time, and the dump reads
# a catalog the simulation has not written yet.
CATALOG = "/some/path/m000p-624.haloproperties"

@mpi(binary=HACC_TPM, runner=RUNNER, processes=8, processes_per_node=8,
     args="{{paramfile}}", fail_by_exit_value=True)
@task(paramfile=FILE_IN)                       # declares NO output
def run_sim(paramfile): pass

@binary(binary="GenericIOPrint")
@task(out_txt=FILE_OUT)
def dump(out_txt): pass                        # opens CATALOG itself

run_sim("indat.params")
dump("catalog.txt")        # races the simulation
```

```python
# RIGHT -- the catalog is a parameter, so the edge exists.
@mpi(binary=HACC_TPM, runner=RUNNER, processes=8, processes_per_node=8,
     args="{{paramfile}}",          # catalog is an edge only -- keep it off argv
     fail_by_exit_value=True)
@task(paramfile=FILE_IN, catalog=FILE_OUT)
def run_sim(paramfile, catalog): pass

@binary(binary="GenericIOPrint")
@task(catalog=FILE_IN, out_txt=FILE_OUT)
def dump(catalog, out_txt): pass

run_sim("indat.params", CATALOG)
dump(CATALOG, "catalog.txt")     # waits for run_sim
```

If a binary writes to a path it chooses itself, name that exact path as the
producer's `FILE_OUT` anyway. The parameter creates the ordering; COMPSs does
not need to have written the file to track it.

### But every parameter also lands on the command line — use `args=`

This is the trap that follows directly from the rule above, and it is fatal.

For `@binary`/`@mpi`, **every function parameter becomes a positional
command-line argument**, in signature order, unless it is a stdin/stdout
redirect. That includes the `FILE_OUT` parameters you added purely to create
dependency edges. So this:

```python
@mpi(binary=HACC_TPM, runner=RUNNER, processes=8, processes_per_node=8)
@task(snapshot=FILE_OUT, catalog=FILE_OUT)
def run_hacc(paramfile, flag, rundir, snapshot, catalog):
    pass
```

builds

```
mpirun -np 8 hacc_tpm <paramfile> <flag> <rundir> <snapshot> <catalog>
```

— five arguments to a program that takes two. It dies immediately, COMPSs
retries, and the run looks like a hang.

**`args=` names the parameters that reach argv; everything else is tracked for
dependencies only.** Keep the edges and fix the command line:

```python
@mpi(binary=HACC_TPM, runner=RUNNER,
     processes=8, processes_per_node=8,
     args="{{paramfile}} {{flag}}",        # <-- ONLY these two reach argv
     flags="--map-by ppr:8:node --bind-to core",
     working_dir=RUNDIR, fail_by_exit_value=True)
@task(paramfile=FILE_IN, snapshot=FILE_OUT, catalog=FILE_OUT)
def run_hacc(paramfile, flag, rundir, snapshot, catalog):
    pass
```

Rule of thumb: **if a `@binary`/`@mpi` task has any parameter that is not a real
command-line argument, it needs `args=`.** A task whose parameters are exactly
its arguments does not.

### A `FILE_OUT` is a sandbox path, not the path you asked for

COMPSs hands the task body a *renamed* file in the worker's sandbox
(`/tmp/COMPSsWorker/<uuid>/<host>/sandBox/job_N/...`) and copies it to the
declared name afterwards. Two consequences:

- **Never assume the name.** Write to the parameter you were given; do not
  rebuild the path from a constant, and do not append an extension. `np.save`
  appends `.npy` unless the name already ends in it, so it silently writes
  beside the tracked file — use `with open(path, "wb") as fh: np.save(fh, arr)`.
  `fig.savefig` infers the format from the extension, so pass `format="png"`.
- **Replacing the file can break the copy-back.** `shutil.copy2(src, dest)` onto
  a `FILE_OUT` has been observed to leave the declared path missing after the
  run. If the product already exists on the shared filesystem, prefer passing
  that path as a plain `FILE_IN` to the consumer instead of re-exporting it
  through a `FILE_OUT`: one fewer tracked file, one fewer thing to go wrong.

---

## `@mpi` — MPI Programs

Copy this shape. All four decorator arguments below are load-bearing, and
omitting any one of them has produced a `TOTALLY FAILED` task on this cluster.

```python
import os

RUNNER = os.environ.get("COMPSS_MPIRUN", "mpirun")   # absolute, not a bare name

@mpi(binary=HACC_TPM,
     runner=RUNNER,                         # 1. absolute, else exit 127
     processes=8, processes_per_node=8,     # 2. ppn defaults to 1 -> N NODES
     args="{{paramfile}} {{flag}}",         # 3. ONLY these reach argv
     working_dir=RUNDIR,                    # 4. where the binary writes
     fail_by_exit_value=True)
@task(paramfile=FILE_IN, snapshot=FILE_OUT, catalog=FILE_OUT)
def run_hacc(paramfile, flag, rundir, snapshot, catalog):
    pass
```

**Why `args=` is there.** Every task parameter becomes a positional
command-line argument. `rundir`, `snapshot` and `catalog` exist only to create
dependency edges, so without `args=` naming the two real arguments, the binary
is invoked as

```
hacc_tpm <paramfile> -n <rundir> <snapshot> <catalog>
```

— five arguments to a program that takes two. It exits immediately, COMPSs
resubmits twice and reports `TOTALLY FAILED`. See *But every parameter also
lands on the command line* above for the full rule.

**If an `@mpi` or `@binary` task has any parameter that is not a real
command-line argument, it needs `args=`.**

`processes=N` is how rank count is expressed — you never type `mpirun -n 8`.
COMPSs assembles `<runner> -np N <flags> <binary> <args>` itself, so a
multi-node MPI run needs **no shell, no wrapper and no `subprocess`**.

**`runner=` must be an absolute path.** A bare `"mpirun"` is resolved off the
*worker's* `PATH`, which is only whatever the environment script built, and a
miss there is `bash: mpirun: command not found`, exit 127, three resubmissions
and a `TOTALLY FAILED` task. The environment exports the resolved launcher, so
read it:

```python
import os
RUNNER = os.environ.get("COMPSS_MPIRUN", "mpirun")   # absolute when exported
```

Decorator arguments are evaluated in the master, where that variable is set, so
a module-level constant like this is fine. Use `.get` with a fallback, never
`os.environ["..."]` — a missing key raises at import, before any task is
submitted, which looks exactly like "nothing ran".

### `processes_per_node` IS NOT OPTIONAL

This is the single most expensive mistake available on this engine. In COMPSs
3.4, `mpi.py` assigns `processes` to the task's **node** reservation and
defaults `processes_per_node` to **1**:

```python
if "processes" in self.kwargs:
    kwargs["computing_nodes"] = self.kwargs["processes"]
...
if "processes_per_node" in self.kwargs:
    kwargs["processes_per_node"] = self.kwargs["processes_per_node"]
else:
    kwargs["processes_per_node"] = 1        # <-- the default that bites
```

So `@mpi(processes=8)` with no `processes_per_node` reserves **8 nodes at one
rank each**. On a 2-node allocation that can never be satisfied, and the task
fails with "not enough slots available" or hangs forever — which looks like a
rank-count problem and is not one.

**Always set `processes_per_node` to `compss_topology.units_per_host`.** With
`processes=8, processes_per_node=4` the task reserves 8 processes across 2
nodes, which is what an 8-rank job on a 2×4 allocation means. 1×8 and 2×4 are
both eight ranks and are not interchangeable.

### Accepted arguments (COMPSs 3.4)

`runner` is mandatory. The rest: `binary`, `processes`, `processes_per_node`,
`working_dir`, `args`, `flags`, `scale_by_cu`, `fail_by_exit_value`.

**`flags` is where rank-placement options go.** It is split on whitespace and
inserted between `-np N` and the binary, so the layout control you would
otherwise reach for a shell to express belongs here:

```python
flags="--map-by ppr:4:node:PE=4 --bind-to core"
```

**MPI + OpenMP**: `processes=` sets the ranks; threads per rank are declared
with `@constraint(computing_units=N)` stacked above `@mpi`, and expressed to the
launcher through `flags` (`PE=N`). The two must agree.

---

## There Is No Pipe. Plan the Data Movement Around That.

A `@binary` task is one program with optional stdin/stdout redirects. No `|`,
no `&&`, no shell. Filtering that a shell pipeline would do has to happen in a
downstream `@task`, with the intermediate on disk in between.

Measured on the 64³ HACC sample (`step_624`, 262,144 particles):

| Path | Intermediate | Parse | Array | RSS |
|---|---|---|---|---|
| dump 10 columns, parse 7 as float64 in a `@task` | 27.2 MB text | 0.98 s | 14.7 MB | 132 MB |
| same dump, streamed line by line, x,y as float32 | 27.2 MB text | ~0.1 s | 0.16 MB | ~30 MB |

The intermediate is unavoidable without a pipe; the parse cost and memory are
not.

1. **Stream the filter.** Read line by line, apply the cut, keep only the
   columns you need. Never `np.loadtxt` the whole file and slice afterwards —
   the slice keeps 20,283 of 262,144 rows, so a full parse pays for 100% of the
   data to use 8%.
2. **float32.** The data is f32 on disk; float64 doubles memory for no
   precision.
3. **`compss_delete_file` the large intermediate** once the filtered product
   exists. Do not `rm -rf` a directory a task also reads from.

**At real scale, abandon the text path.** The dump reaches ~1.7 TB at 1024³.
`GenericIO2Cosmo <mpiioName> <cosmoName> <rank0> [rank1 ...]` writes per-rank
`.cosmo` binaries readable with `np.fromfile`, needs no shell and no redirect,
and is therefore a *better* fit for `@binary` than for a Parsl `@bash_app`.
Confirm the record layout before relying on it.

---

## A Completed Run Is Not a Successful Run

**`run_workflow` reporting `status: completed` and `exit_code: 0` does not mean
the workflow worked.** COMPSs can fail a task while the master still exits 0.
This has happened on this engine. Verify every run:

1. **`list_files`** the work directory and confirm the deliverable exists, with
   a plausible size. Do not assert on the simulation's own output inside the
   workflow — output from a previous run satisfies the check, so a run that
   wrote nothing passes.
2. **Read the attached `logs`.** On failure `run_workflow` tails the newest
   work-directory logs *and* the newest `~/.COMPSs/<app>_NN/jobs/*` files, which
   is where a failed task's real error lives. Read those before calling
   `read_file` on anything.
3. **Check `launch_command` contains `-m pycompss`.**

If the deliverable is missing but the run "completed", the job logs are the only
place that says why.

---

## Worked Example: HACC cosmology

One file: an `@mpi` task running HACC, two `@binary` dumps, and `@task`
functions that pick the halo and render the slice.

Three domain facts the structure depends on:

- **The sample tree is read-only.** HACC resolves output paths relative to its
  cwd, so running in the shipped `SampleRun_go` fails with
  `mkdir: cannot create directory './output': Permission denied`. It gets a
  writable copy under the work directory, with `params/` staged in.
- **Use the FRONTEND `GenericIOPrint`**, not `improv.cpu/mpi/bin`. The
  MPI-linked build aborts in `MPI_Init` when run without `mpirun`, which is
  exactly how COMPSs runs a plain `@binary`. The frontend build still reads
  across all rank shards — that is what `--no-rank-info` does.
- **Mass is not a column.** Compute
  `mp = Omega_m * rho_crit0 * (RL/NP)**3`, with `RL`/`NP`/`Omega_m` parsed from
  this run's own `indat.params` — never hardcoded from a paper, and never
  transcribed into module constants.

```python
"""HACC density-slice workflow (PyCOMPSs).

Ranks and layout come from get_resources()['compss_topology']; the platform
environment and COMPSS_MPIRUN_TYPE come from setup_hpc.sh. Neither is set here.
"""
import os

from pycompss.api.api import compss_wait_on
from pycompss.api.binary import binary
from pycompss.api.mpi import mpi
from pycompss.api.parameter import FILE_IN, FILE_OUT, FILE_OUT_STDOUT, Type
from pycompss.api.task import task

# Absolute launcher, exported by setup_hpc.sh. NOT the bare name "mpirun":
# that is resolved off the worker's PATH and fails with exit 127.
RUNNER = os.environ.get("COMPSS_MPIRUN", "mpirun")

HACC = "/lcrc/project/PEDAL/jalemu/HACC/HACC_go"
HACC_TPM = f"{HACC}/improv.cpu/mpi/bin/hacc_tpm"
GIOP = f"{HACC}/improv.cpu/frontend/bin/GenericIOPrint"   # frontend, see above
SAMPLE = "/lcrc/project/PEDAL/jalemu/HACC/SampleRun_go"

WORK = "/app/work/run0"          # alias; the server rewrites it on write
RUNDIR = f"{WORK}/hacc_run"      # writable copy HACC runs in
STEP = 624
NRANKS = 8                       # == compss_topology.total_slots
PER_NODE = 4                     # == compss_topology.units_per_host
NTHREADS = 4                     # OMP threads per rank
THICKNESS = 4.0
NBINS = 256


@task(returns=str)
def stage_rundir():
    """Give HACC a writable tree with params/ copied in.

    A @task rather than master-side code so the dependency is explicit: its
    return value goes to run_hacc, which is what orders them.

    No FILE_OUT here. An earlier version also copied indat.params to a tracked
    FILE_OUT so the analysis could parse "the exact file this run used" -- but
    copy2 onto a FILE_OUT left the declared path missing after the run, and the
    copy under RUNDIR/params is on GPFS and visible to every task anyway, so
    the consumer takes that path as a plain FILE_IN instead.

    rmtree before copytree, not `if not isdir`: a previous attempt that died
    mid-copy leaves a partial params/ that the isdir check would keep forever.
    """
    import os
    import shutil

    os.makedirs(RUNDIR, exist_ok=True)
    for sub in ("output", "analysis"):
        os.makedirs(os.path.join(RUNDIR, sub), exist_ok=True)
    dst = os.path.join(RUNDIR, "params")
    if os.path.isdir(dst):
        shutil.rmtree(dst)
    shutil.copytree(os.path.join(SAMPLE, "params"), dst)
    return RUNDIR


@mpi(binary=HACC_TPM, runner=RUNNER,                 # absolute, from the env
     processes=NRANKS, processes_per_node=PER_NODE,   # ppn is NOT optional
     args="{{paramfile}} {{flag}}",                   # ONLY these reach argv
     flags=f"--map-by ppr:{PER_NODE}:node:PE={NTHREADS} --bind-to core",
     working_dir=RUNDIR, fail_by_exit_value=True)
@task(paramfile=FILE_IN, snapshot=FILE_OUT, catalog=FILE_OUT)
def run_hacc(paramfile, flag, rundir, snapshot, catalog):
    """hacc_tpm <paramfile> -n

    args= is load-bearing: `rundir`, `snapshot` and `catalog` exist only to
    create dependency edges, and without args= they would ALSO be appended to
    hacc_tpm's command line -- five arguments to a program that takes two.

    `rundir` is stage_rundir's return value. snapshot/catalog are paths HACC
    chooses itself, declared FILE_OUT so the dumps wait for them.
    """
    pass


@binary(binary=GIOP, working_dir=RUNDIR, fail_by_exit_value=True)
@task(gio_file=FILE_IN, out_txt={Type: FILE_OUT_STDOUT})
def gio_print(flag, gio_file, out_txt):
    """GenericIOPrint <flag> <gio_file> > <out_txt>.

    Always the MASTER file, never a per-rank shard.
    """
    pass


@task(returns=dict, halo_txt=FILE_IN, paramfile=FILE_IN)
def select_halo(halo_txt, paramfile, outdir):
    """Parse this run's config, pick the most massive SOD halo."""
    import os

    import numpy as np

    os.makedirs(outdir, exist_ok=True)

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
               + float(pv["DEUT"]) / (hub * hub)     # Omega_b from Omega_b h^2
               + float(pv.get("OMEGA_NU", "0.0")))
    mp = omega_m * 2.77536627e11 * (RL / NP) ** 3    # rho_crit0 h^2 Msun/Mpc^3

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

    # sod_halo_count == -101 marks halos SOD was never computed for; drop them
    # before the argmax. fof_halo_mass would pick a different halo.
    valid = np.flatnonzero(cat[:, col["sod_halo_count"]] != -101)
    if valid.size == 0:
        raise RuntimeError("no halos with SOD")
    best = valid[np.argmax(cat[valid, col["sod_halo_mass"]])]

    info = {
        "RL": RL, "NP": NP, "omega_m": omega_m, "mp": mp,
        "m200c": float(cat[best, col["sod_halo_mass"]]),
        "r200c": float(cat[best, col["sod_halo_radius"]]),
        "hx": float(cat[best, col["fof_halo_center_x"]]),
        "hy": float(cat[best, col["fof_halo_center_y"]]),
        "hz": float(cat[best, col["fof_halo_center_z"]]),
        "nhalos": int(cat.shape[0]), "nsod": int(valid.size),
    }
    with open(os.path.join(outdir, "most_massive_halo.txt"), "w") as f:
        f.write(f"M_200c = {info['m200c']:.6e} Msun/h\n")
        f.write(f"R_200c = {info['r200c']:.6e} Mpc/h\n")
        f.write(f"center = ({info['hx']:.6f}, {info['hy']:.6f}, "
                f"{info['hz']:.6f}) Mpc/h\n")
    return info


@task(returns=str, snap_txt=FILE_IN)
def slice_and_render(snap_txt, info, outdir, thickness, nbins):
    """Filter the dump to the halo z-slice, then render.

    One task, not two: there is no pipe, so splitting them would write a second
    intermediate for nothing. Streamed line by line.
    """
    import os

    import numpy as np
    import matplotlib
    matplotlib.use("Agg")            # headless compute node
    import matplotlib.pyplot as plt
    from matplotlib.colors import LogNorm

    os.makedirs(outdir, exist_ok=True)
    RL, mp = info["RL"], info["mp"]
    hx, hy, hz, r200c = info["hx"], info["hy"], info["hz"], info["r200c"]
    half = thickness / 2.0

    xs, ys = [], []
    with open(snap_txt) as f:
        for ln in f:
            if not ln or ln[0] == "#":
                continue
            parts = ln.split()
            if len(parts) < 3:
                continue
            try:
                zv = float(parts[2])
            except ValueError:
                continue
            dz = abs(zv - hz)
            if RL - dz < dz:                  # periodic distance in z
                dz = RL - dz
            if dz <= half:
                xs.append(parts[0])
                ys.append(parts[1])
    x = np.mod(np.array(xs, dtype=np.float32), RL)
    y = np.mod(np.array(ys, dtype=np.float32), RL)

    edges = np.linspace(0.0, RL, nbins + 1, dtype=np.float32)
    counts, _, _ = np.histogram2d(x, y, bins=[edges, edges])
    cell = RL / nbins
    # every particle has the same mass: histogram counts, scale once
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
        f.write(f"ranks              = {NRANKS} ({PER_NODE}/node)\n")
        f.write(f"halos in catalog   = {info['nhalos']} "
                f"({info['nsod']} with SOD)\n")
        f.write(f"most massive M_200c= {info['m200c']:.6e} Msun/h\n")
        f.write(f"most massive R_200c= {info['r200c']:.6e} Mpc/h\n")
        f.write(f"halo center        = ({hx:.4f}, {hy:.4f}, {hz:.4f}) Mpc/h\n")
        f.write(f"slice center z     = {hz:.4f} Mpc/h, "
                f"thickness {thickness:g} Mpc/h\n")
        f.write(f"particles in slice = {x.size}\n")
        f.write(f"grid               = {nbins} x {nbins}\n")
        f.write(f"image              = {png}\n")
    return png


def main():
    paramfile = f"{RUNDIR}/params/indat.params"
    snapshot = (f"{RUNDIR}/output/full_snapshots/step_{STEP}/"
                f"m000p.full.mpicosmo.{STEP}")
    catalog = (f"{RUNDIR}/analysis/haloproperties/step_{STEP}/"
               f"m000p-{STEP}.haloproperties")
    halo_txt = f"{WORK}/haloproperties_step{STEP}.txt"
    snap_txt = f"{WORK}/particles_step{STEP}.txt"

    # Submissions, not executions. Ordering comes from the parameters below;
    # the only barrier is the single compss_wait_on at the end.
    rundir = stage_rundir()

    # Absolute paramfile, not "./params/indat.params": working_dir governs
    # where HACC WRITES, and an absolute input removes any dependence on how
    # the worker applies it.
    run_hacc(paramfile, "-n", rundir, snapshot, catalog)

    # Different products of run_hacc, so these two run concurrently.
    gio_print("--no-rank-info", catalog, halo_txt)
    gio_print("--no-rank-info", snapshot, snap_txt)

    info = select_halo(halo_txt, paramfile, WORK)
    png = slice_and_render(snap_txt, info, WORK, THICKNESS, NBINS)

    print(f"wrote {compss_wait_on(png)}")


if __name__ == "__main__":
    main()
```

Deliverables in the work directory: `dm_density_slice.png` (the scientific
output), `density_slice.npy`, `summary.txt`, `most_massive_halo.txt`. The two
`.txt` dumps are intermediates — `haloproperties_step<N>.txt` is ~680 KB and
`particles_step<N>.txt` is ~27 MB.

Structural details worth copying: every dependency is a `FILE_IN`/`FILE_OUT`
parameter or a future passed as an argument; exactly one `compss_wait_on`, on
the final PNG; `fail_by_exit_value=True` everywhere; `working_dir` is the
writable copy; `processes_per_node` matches the topology; imports inside task
bodies.

---

## The Four Decorators You Use

Accepted arguments, read from each decorator's own `SUPPORTED_ARGUMENTS` in
COMPSs 3.4. Anything not listed raises at import. Mandatory arguments marked `*`.

| Decorator | Accepted arguments |
|---|---|
| `@task` | parameter directions, `returns`, `priority`, `time_out`, `on_failure` |
| `@binary` | `binary`*, `working_dir`, `args`, `fail_by_exit_value` |
| `@mpi` | `runner`*, `binary`, `processes`, `processes_per_node`, `working_dir`, `args`, `flags`, `scale_by_cu`, `fail_by_exit_value` |
| `@constraint` | `computing_units` — threads per rank, when the binary is hybrid |

Stacking order, outermost first: `@constraint`, then `@mpi` or `@binary`, then
`@task` innermost. Both of the latter require `@task` beneath them.

Parameter types, from `pycompss.api.parameter`: `FILE_IN`, `FILE_OUT`,
`FILE_INOUT` for tracked files, and `FILE_OUT_STDOUT` to redirect a binary's
stdout. `Type` is only needed for the dict form.

COMPSs has other decorators. They are not used here and you should not reach for
them: if a task cannot be expressed with the four above, that is something to
report, not to work around.

---

## Key Differences from Parsl

| Feature | Parsl | PyCOMPSs |
|---|---|---|
| Python task | `@python_app` | `@task` |
| CLI task | `@bash_app` returning a command string | `@binary` + `@task`, `pass` body |
| MPI task | `@bash_app` returning `mpirun ...` | `@mpi(processes=N)` + `@task` |
| Shell features (pipes, `&&`, `source`) | available in the string | **none** |
| Where the allocation comes from | the app's `mpiexec` inherits `$PBS_NODEFILE` | COMPSs's own `resources.xml`, reached over SSH |
| Program environment | `source envfile &&` in the string | pre-exported by `setup_hpc.sh` |
| Init / shutdown | `parsl.load` / `parsl.clear` | Nothing — the launcher owns both |
| Topology | `Config` in the file | generated by the server |
| Future resolution | `future.result()` | `compss_wait_on(future)` |
| Dependencies | Explicit via futures/`File` | Automatic from task parameters |

That fifth row explains most of the difference in feel. Parsl spans nodes by
getting out of the way; COMPSs spans nodes by being told they exist. Neither is
better, but only one of them needs a topology.

---

## Common Pitfalls

| Pitfall | Rule |
|---|---|
| Writing `project.xml` / `resources.xml` | The server generates them. Read `compss_topology` from `get_resources` instead |
| Writing a `.sh`, `.pbs` or any file other than the workflow `.py` | Two XML files (server) and one Python file (you). Nothing else |
| `@mpi(processes=N)` without `processes_per_node` | It defaults to **1**, so N is read as N NODES. Set it to `units_per_host` |
| A `@binary`/`@mpi` task with edge-only parameters and no `args=` | Every parameter lands on argv. Name the real arguments in `args="{{a}} {{b}}"` |
| `runner="mpirun"` as a bare name | Resolved off the worker's thin PATH; exit 127. Use `os.environ.get("COMPSS_MPIRUN", "mpirun")` |
| Rebuilding a `FILE_OUT` path from a constant, or appending an extension | You get a renamed sandbox path. Write to the parameter you were given |
| `np.save(path, arr)` / `fig.savefig(path)` on a `FILE_OUT` | Both guess at extensions. Use an open file handle, or pass `format="png"` |
| `copy2` onto a `FILE_OUT` to re-export an existing file | Has left the declared path missing. Pass the original path as `FILE_IN` instead |
| `total_slots: 0` in `compss_topology` | No worker can start. Report the gap and stop; do not write a workflow |
| Looking for a decorator parameter that sources an env file | There isn't one. The environment is pre-exported |
| Reaching for a decorator outside the four documented here | Report what you cannot express instead |
| Shell placement flags via a wrapper | `@mpi(flags="--map-by ... --bind-to core")` takes them directly |
| `@mpi(processes=N)` with N > `total_slots` | Never scheduled; reports "not enough slots" or hangs. Fit N to the topology |
| Oversubscribing MPI to make N fit | Wastes the allocation and changes the result while reporting success. Reduce N or report the gap |
| `processes_per_node` ≠ `units_per_host` | 1×8 and 2×4 are both eight ranks and are not interchangeable |
| Setting environment variables in the file | The JVM already copied its environment; the assignment is dead code. It belongs in `setup_hpc.sh` |
| Treating `exit_code: 0` as success | COMPSs can fail a task with the master exiting 0. Verify the deliverable and read the job logs |
| Asserting on the simulation's own output inside the workflow | A previous run's output satisfies it. Verify afterwards with `list_files` |
| `fail_by_exit_value` left at `False` | A nonzero exit returns as a value, the task "completes", consumers read a stale file |
| Running the program in its read-only reference tree | HACC writes relative to cwd. Stage a writable copy under the work directory |
| MPI-linked `GenericIOPrint` under `@binary` | Aborts in `MPI_Init` without `mpirun`. Use the frontend build |
| `subprocess` anywhere in the file | Takes the work out of COMPSs. CLI work is `@binary`/`@mpi` |
| Expecting a pipe, `&&` or `source` in `@binary` | No shell. Filter in a downstream `@task` |
| `@binary`/`@mpi` without `@task` under it | Both required, `@task` innermost |
| A non-`pass` body under `@binary` | Ignored; arguments come from the signature |
| `args=("flag", "value")` as a tuple | `args` is a **string** with `{{name}}` placeholders |
| `compss_start()` / `compss_stop()` in your file | The launcher already owns the runtime |
| No `main()` / `__main__` guard | The launcher runs the file as a program; nothing happens |
| Passing paths as bare strings between tasks | Invisible to the scheduler. Declare `FILE_IN`/`FILE_OUT` |
| A producer with no `FILE_OUT` | No edge — consumers race it |
| `compss_wait_on()` after every submission | Serializes the graph; wait only where a real value is needed |
| `np.loadtxt` on a full GenericIO dump | Parses 100% of the rows to keep 8%. Stream it |
| Hardcoding cosmology into module constants | Parse `indat.params` in the task. Transcribed values drift from the file the run used |
| Rendering without `matplotlib.use("Agg")` | Headless nodes have no display |
| `{Type: FILE_IN}` without importing `Type` | `NameError` at import. Import it, or use the bare `param=FILE_IN` form |
| `install_package("pycompss")` mid-run | Cannot work; see below |
| A `launch_command` without `-m pycompss` | Ran sequentially with every decorator ignored, and exited 0 |
| Hunting the filesystem for binaries, env files or module paths | They are in the domain skill under `skills/use_cases/<domain>/`. If one is missing, report that |

Note what is **not** a pitfall here, unlike under Parsl: a module-level constant
read inside a task body. COMPSs workers import the application module, so
module-level names resolve in task bodies. Decorator arguments are evaluated in
the master, which is why they can be constants too.

**Never write a `.sh` wrapper and run it as `@binary(binary="bash")`.** It is
sometimes proposed as a way to get a shell — for `source`, for a pipe, or to
make `mpirun` inherit `$PBS_NODEFILE`. None of those is necessary:
`@mpi(processes=N, processes_per_node=M, flags=...)` spans nodes on its own, the
environment is pre-exported, and filtering belongs in a downstream `@task`. A
wrapper also makes the task graph record `bash` instead of the program, so the
run is no longer reproducible by the engine it claims to use.

**The deliverables are two XML files and one Python file.** The server writes
the XML; you write the Python. Nothing else.

---

## MCP Server Behavior

`servers/pycompss_server.py` subclasses `servers/server.py`, the same base class
the Parsl and ADIOS2 servers use. It hosts no runtime. Its contributions are the
launch command, the generated topology, the structural warnings, and extended
log collection.

### Inherited tools

| Tool | Worth knowing |
|---|---|
| `write_file` | Confined to the work directory; escaping paths are **rejected**. `/app/` aliases rewritten on write. `.py` is `ast.parse`d — syntax errors reject, structure warns. Returns a `next` hint |
| `read_file` | 200 lines by default, `start_line` for paging. Reports `total_lines` and `truncated` |
| `list_files` | Prunes engine scratch (`__pycache__`, `runinfo`, `.parsl`, `.compss`, `_task_scripts`). 400 entries, 4 levels |
| `check_package` | Probed in a **child process** with the interpreter that runs workflows |
| `install_package` | pip into that interpreter, **900 s timeout**. Pure-Python dependencies only |
| `get_status` | No argument: all runs. With a `run_id`: complete stdout and stderr, not the tail |
| `get_resources` | `in_pbs`, `nnodes`, `ntasks`, `ranks_per_node`, `nodelist`, `launcher`, plus `compss_topology` and `compss_install_dir` |
| `run_workflow` | One subprocess, 7200 s default. Returns `launch_command`, `exit_code`, 6 KB tails, `stdout_file`/`stderr_file`, and `logs` on failure |
| `cleanup` | Clears the run records. **Files on disk are left alone** |

### Engine-specific behaviour

**The topology is generated before every run**, from `PBS_NODEFILE` plus an SSH
probe, and reported under `compss_topology`. `write_xml` remains as an override
and marks that file as yours for the session.

**`COMPSS_MPIRUN_TYPE` is set by `launch_env()`**, defaulting to `ompi`.

**Log collection reaches `~/.COMPSs`.** The inherited `collect_logs` only sees
the work directory; this server also tails the newest
`~/.COMPSs/<app>_NN/jobs/*` files, which is where a failed task's actual error
lives.

**Watch a long run instead of waiting.** Stdout and stderr go to `<run_id>.out`
and `<run_id>.err` in the work directory while the run is in flight, not into a
pipe held until exit. They survive a timeout kill.

### `check_package("pycompss")` and `version: unknown`

The version is reported as `getattr(module, "__version__", "unknown")`, so a
package with no `__version__` comes back as **installed, version unknown**. That
is normal and healthy, not evidence of a partial install.

`installed: True` means the Python package imports under the interpreter that
will run the workflow. It does **not** mean the runtime behind it works — the
pip package installs a large Java distribution as a side effect, and the Python
half can import while that half is missing. A broken runtime shows up as a
failed `run_workflow` with Java errors, not as a failed import.

If it comes back `False`, **report, do not fix**. `install_package("pycompss")`
would attempt the full distribution build inside a 900-second timeout, which it
will not finish, and a half-finished install shadows a good one.

### The launch command

```bash
<python> -m pycompss [--project=...] [--resources=...] \
    --python_propagate_virtual_environment=true workflow.py [args]
```

Extra flags come from `MAW_COMPSS_FLAGS`. There is no variable for the launcher:
`python -m pycompss` is a property of the interpreter in `MAW_PYTHON`, so the
runtime, the master and the tasks agree on one environment by construction.
`--python_propagate_virtual_environment=true` activates the venv on the workers;
without it they would import whatever `pycompss` a fresh login shell finds,
which with a venv install is nothing.

---

## How PyCOMPSs Is Installed Here

A pip install in the project virtualenv, and nothing else — no hand-built
COMPSs, no `COMPSS_HOME` exported by the server, no `runcompss` on `PATH`:

```bash
source setup_hpc.sh
pip install --no-build-isolation -v pycompss
```

`setup_hpc.sh` activates the venv itself and must be sourced **instead of**, not
after, `venv/bin/activate` — activating the venv afterwards restores the
pre-venv `PATH` and drops `JAVA_HOME`'s bin, the compiler and `mpirun`. Since
`python -m pycompss` resolves against whichever interpreter is active, a
half-activated venv is the one reliable way to get a "pycompss is not installed"
failure on a machine where it is installed.

- It needs a JVM available to that environment; `JAVA_HOME` comes from the setup
  script, not from this server
- There is no fallback mode. If the runtime cannot start, the run fails and says
  so, rather than running the file sequentially and reporting a COMPSs run
- **Write the workflow file, and no other file.**