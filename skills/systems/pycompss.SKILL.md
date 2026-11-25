---
name: systems/pycompss description: >
  Writing a PyCOMPSs application for MAW: the execution contract, @task/@binary/
  @mpi, the four rank layouts, the server-generated topology, and why a completed
  run is not a successful one. Load for every --engine pycompss run; the API
  manual is knowledge/PyCOMPSs.
---

# PyCOMPSs — System Skill

PyCOMPSs (BSC) expresses work as `@task`-decorated functions and lets the COMPSs
runtime detect dependencies automatically from task parameters.

**This run's engine is PyCOMPSs. Do not import, use or reference Parsl, ADIOS2
or Wilkins.**

## READ THIS FIRST: You Write ONE File, and Only That File

Two tools carry the work: `write_file(path, content)` writes the ONE standalone
PyCOMPSs driver, and `run_workflow(filename)` launches it as `<python> -m
pycompss ... workflow.py`. **Do not write any other file** — in particular not
`project.xml` or `resources.xml`, which the server generates from the real
allocation before every run. The rest of the tools behave identically on every
engine.

**You never execute anything yourself.** No subprocess, no direct CLI call, no
PBS script, no `mpirun` typed by you.

### The rule

Every unit of work is a decorated function: **`@task`** for pure Python work,
**`@binary(binary=...)` above `@task`** for a command-line program, and
**`@mpi(...)` above `@task`** for an MPI program. Four `@mpi` arguments are
mandatory in practice, and each has caused a `TOTALLY FAILED` task here:
`runner=` an absolute path (not `"mpirun"`), `processes_per_node=` (it defaults
to 1, which reads `processes` as a NODE count), `args=` naming only the real
command-line arguments (every other parameter is appended to argv), and
`fail_by_exit_value=True`.

All of them live in the **same file**. A simulation plus its visualization is
one file with an `@mpi` task and a `@task` — not two files, not two tool calls.

### What `write_file` checks

A Python file is parsed with `ast.parse` before being written, so a **syntax
error** comes back as `"status": "rejected"` and nothing reaches disk.

Structural problems come back as `warnings` on a file that *was* written: no
`@task`, nothing imported from `pycompss`, no `main()`/`__main__` guard, MPI
oversubscription, or `COMPSS_MPIRUN_TYPE` set inside the file. Warnings do not
block `run_workflow`. Fix them anyway.

Not checked and still forbidden: `subprocess`/`os.system`/`os.popen`, and
`@binary`/`@mpi` without `@task` beneath it (COMPSs ignores the decorator and
the function becomes an ordinary one that does nothing). A path resolving
outside the work directory is **rejected**, not relocated; `/app/work/run0` and
`/app/inputs/data` are aliases the server rewrites on write, so those literals
are correct and portable in generated code.

## Runtime Lifecycle: the Launcher Owns It

`run_workflow` runs your file through `<python> -m pycompss`, which starts the
runtime, executes the application inside it, and shuts it down. **Your file must
NOT call `compss_start()` or `compss_stop()`.** Give it a `main()` and an `if
__name__ == "__main__":` guard instead.

`-m pycompss` is the whole ballgame. The same file run as plain `python
workflow.py` **executes sequentially, ignoring every PyCOMPSs import, decorator
and API call.** It does not error, it exits 0, and it produces the right numbers
having parallelised nothing. That is a documented upstream mode, which is why it
is dangerous: a legitimate way to run the file, and not the run you are claiming
to have performed. Check `-m pycompss` is in the `launch_command` every run.

## The Topology Is Not Yours

COMPSs does not read the allocation from PBS but from
`project.xml`/`resources.xml`, and **the server builds both from the allocation
before every run.** You do not write them and do not need the schema. Call
`get_resources` and read:

```
compss_topology.mode             # "master_only" or "multi_node"
compss_topology.total_slots      # the ceiling for @mpi(processes=N)
compss_topology.units_per_host   # what to pass as processes_per_node
compss_topology.probe            # multi_node only: can a worker start there
```

`master_only` is ONE node: the master runs the tasks itself with no ssh and no
remote worker — COMPSs' own single-node topology, and what a laptop, a container
and a single compute node get. `multi_node` declares each host as a worker
reached over ssh, which `probe` reports on.

**`processes=N` is a resource constraint.** Exceed `total_slots` and the task is
never scheduled: COMPSs reports "not enough slots available" or hangs until the
timeout. If N fits, proceed. If it does not and the science can be downscaled,
reduce N and **say so in your summary**, naming the rank count you actually ran.
If it cannot be reduced, report the gap and stop — `reason` and `probe` name the
hosts that could not start a worker, which is an environment problem.
`total_slots: 0` means no task of any kind can run; the run is refused before it
launches, so report it and do not retry.

**Never oversubscribe.** `OMPI_MCA_rmaps_base_oversubscribe=1` or any equivalent
silences the refusal by stacking ranks onto fewer slots than they need: it
wastes the allocation, changes the numbers, and still reports success.
`write_file` warns when it sees this.

`write_xml` exists only as an override for a demonstrably wrong generated
topology, and using it is a last resort you must justify in your summary.

## The Environment Is Not Yours Either

A binary needing `source .../bashrc.<platform>` first has **no in-file place to
put it**: `@mpi(binary=...)` names an executable and COMPSs builds the argv —
there is no shell. The activated Spack environment provides it, and
`launch_env()` sets `COMPSS_MPIRUN_TYPE=openmpi` (the value the 3.4 docs give,
`ompi`, matches no shipped properties file and is silently ignored). **There is
no decorator parameter for this** — COMPSs 3.4 has no `env_script`, so do not go
looking. And **do not set environment variables in the generated file**: the JVM
copied its environment before your module was imported, so the assignment is
dead code that looks load-bearing. A missing variable is an environment gap to
**report**, like a missing binding.

`working_dir=` on `@binary`/`@mpi` is yours, and matters: a program that writes
relative to its cwd must launch from a directory the run owns and can write to.

## `@binary` — Command-Line Programs

The decorated function has a `pass` body. Arguments come from the **function
signature**; their roles are declared in the `@task` decorator.

```python
from pycompss.api.task import task
from pycompss.api.binary import binary
from pycompss.api.parameter import FILE_IN, FILE_OUT_STDOUT, Type

@binary(binary=DUMP_EXE, working_dir=WORK,
        fail_by_exit_value=True)
@task(snapshot=FILE_IN, out_txt={Type: FILE_OUT_STDOUT})
def dump(flag, snapshot, out_txt):
    pass

dump("--no-rank-info", snapshot, "snapshot.txt")
# runs:  <DUMP_EXE> --no-rank-info <snapshot> > snapshot.txt
```

A positional argument is a plain parameter, inserted in signature order; a flag
is a plain string parameter; a stdout redirect is `{Type: FILE_OUT_STDOUT}` on
that parameter. **Prefer the bare form** — `@task(paramfile=FILE_IN)` and
`@task(paramfile={Type: FILE_IN})` are the same thing and the first needs no
`Type` import; the dict form is only needed for the redirect.

**Set `fail_by_exit_value=True` on every `@binary`/`@mpi` task.** The default
returns the exit code as the task's *return value* and the task counts as
completed — so a simulation that dies after `MPI_Init` flows straight through to
a consumer reading a stale file from a previous run.

### The producer MUST declare its outputs as `FILE_OUT`

COMPSs builds the graph from **parameters only**. It cannot see inside a binary.

```python
# WRONG -- no edge. COMPSs may run these at the same time, and the dump reads
# a catalog the simulation has not written yet.
@task(paramfile=FILE_IN)                       # declares NO output
def run_sim(paramfile): pass

@task(out_txt=FILE_OUT)
def dump(out_txt): pass                        # opens CATALOG itself

run_sim(PARAMFILE)
dump("catalog.txt")                            # races the simulation
```

```python
# RIGHT -- the catalog is a parameter, so the edge exists.
@mpi(binary=SIM_EXE, runner=RUNNER, processes=8, processes_per_node=8,
     args="{{paramfile}}",          # catalog is an edge only -- keep it off argv
     fail_by_exit_value=True)
@task(paramfile=FILE_IN, catalog=FILE_OUT)
def run_sim(paramfile, catalog): pass

@binary(binary=DUMP_EXE)
@task(catalog=FILE_IN, out_txt=FILE_OUT)
def dump(catalog, out_txt): pass

run_sim(PARAMFILE, CATALOG)
dump(CATALOG, "catalog.txt")                   # waits for run_sim
```

If a binary chooses its own output path, name that exact path as the producer's
`FILE_OUT` anyway: the parameter creates the ordering, and COMPSs does not need
to have written the file to track it.

### But every parameter also lands on the command line — use `args=`

The trap that follows directly from the rule above, and it is fatal. For
`@binary`/`@mpi`, **every function parameter becomes a positional command-line
argument** in signature order, unless it is a stdin/stdout redirect — including
the `FILE_OUT` parameters you added purely to create edges. A task declaring
`(paramfile, flag, rundir, snapshot, catalog)` therefore builds

```
mpirun -np 8 <SIM_EXE> <paramfile> <flag> <rundir> <snapshot> <catalog>
```

— five arguments to a program that takes two. It dies immediately, COMPSs
retries, and the run looks like a hang.

**`args=` names the parameters that reach argv; everything else is tracked for
dependencies only.** Keep the edges and fix the command line:

```python
@mpi(binary=SIM_EXE, runner=RUNNER,
     processes=8, processes_per_node=8,
     args="{{paramfile}} {{flag}}",        # <-- ONLY these two reach argv
     flags="--map-by ppr:8:node --bind-to core",
     working_dir=RUNDIR, fail_by_exit_value=True)
@task(paramfile=FILE_IN, snapshot=FILE_OUT, catalog=FILE_OUT)
def run_sim_full(paramfile, flag, rundir, snapshot, catalog):
    pass
```

**If an `@binary`/`@mpi` task has any parameter that is not a real command-line
argument, it needs `args=`.** One whose parameters are exactly its arguments
does not.

### A `FILE_OUT` is a sandbox path, not the path you asked for

COMPSs hands the task body a *renamed* file in the worker's sandbox
(`/tmp/COMPSsWorker/<uuid>/<host>/sandBox/job_N/...`) and copies it to the
declared name afterwards. Two consequences:

- **Never assume the name.** Write to the parameter you were given; do not
  rebuild the path from a constant or append an extension. `np.save` appends
  `.npy` unless the name already ends in it, writing beside the tracked file —
  use `with open(path, "wb") as fh: np.save(fh, arr)`. `fig.savefig` infers the
  format from the extension, so pass `format="png"`.
- **Replacing the file can break the copy-back.** `shutil.copy2(src, dest)` onto
  a `FILE_OUT` has been observed to leave the declared path missing after the
  run. If the product already exists on the shared filesystem, prefer passing
  that path as a plain `FILE_IN` to the consumer instead of re-exporting it
  through a `FILE_OUT`: one fewer tracked file, one fewer thing to go wrong.

## `@mpi` — MPI Programs

Copy this shape. All four decorator arguments below are load-bearing, and
omitting any one of them has produced a `TOTALLY FAILED` task on this cluster.

```python
import os

RUNNER = os.environ.get("COMPSS_MPIRUN", "mpirun")   # absolute; see below

@mpi(binary=SIM_EXE,
     runner=RUNNER,                         # 1. absolute, else exit 127
     processes=8, processes_per_node=8,     # 2. ppn defaults to 1 -> N NODES
     args="{{paramfile}} {{flag}}",         # 3. ONLY these reach argv
     working_dir=RUNDIR,                    # 4. where the binary writes
     fail_by_exit_value=True)
@task(paramfile=FILE_IN, snapshot=FILE_OUT, catalog=FILE_OUT)
def run_sim_full(paramfile, flag, rundir, snapshot, catalog):
    pass
```

`processes=N` is how rank count is expressed — you never type `mpirun -n 8`.
COMPSs assembles `<runner> -np N <flags> <binary> <args>` itself, so a
multi-node MPI run needs **no shell, no wrapper and no `subprocess`**.

**`runner=` must be an absolute path.** A bare `"mpirun"` resolves off the
*worker's* `PATH` and a miss there is exit 127, three resubmissions and a
`TOTALLY FAILED` task. The server resolves it in the master and reports it as
`mpi_runner` in `get_resources`, and exports the same value as `COMPSS_MPIRUN`
— so read that variable, with `.get` and a fallback, never
`os.environ["..."]`, which raises at import before any task is submitted and
looks exactly like "nothing ran". Decorator arguments are evaluated in the
master, so a module-level constant is fine here. **An empty `mpi_runner` means
the launcher was not found: report it, do not fall back to a bare name.**

### `processes_per_node` IS NOT OPTIONAL

The single most expensive mistake available on this engine. COMPSs 3.4 assigns
`processes` to `computing_nodes` and defaults `processes_per_node` to **1**;
`@task` then divides the first by the second to get the node count. With the
default, no division happens and `processes` IS the node count — so
`@mpi(processes=8)` alone reserves **8 nodes at one rank each**. On a smaller
allocation that can never be satisfied: the task fails with "not enough slots
available" or hangs, which looks like a rank-count problem and is not one.

**Always set `processes_per_node` to `compss_topology.units_per_host`.** With
`processes=8, processes_per_node=4` the task reserves 8 processes across 2
nodes. `processes` must also be a multiple of `processes_per_node`, or the task
raises. 1x8 and 2x4 are both eight ranks and are not interchangeable.

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

## The Four Rank Layouts

Every number below comes from `compss_topology`. `processes` is TOTAL ranks,
`processes_per_node` is ranks per node, so nodes reserved is `processes /
processes_per_node` — and `processes` must be a multiple of it.

**1. One node, many ranks, one task.** `total_slots: 8, units_per_host: 8`:

```python
@mpi(binary=EXE, runner=RUNNER, processes=8, processes_per_node=8,
     args="{{paramfile}}", working_dir=WORK, fail_by_exit_value=True)
@task(paramfile=FILE_IN, product=FILE_OUT)
def sim(paramfile, product): pass
```

**2. One node, many ranks, parallel tasks.** Slice the node and submit all of
them before synchronising — a `compss_wait_on` inside the loop serialises the
graph into one task at a time. COMPSs places each task itself: unlike Parsl
there is no `taskset` and no host file to write, here or in layouts 3 and 4.

```python
@mpi(binary=EXE, runner=RUNNER, processes=4, processes_per_node=4,
     args="{{paramfile}}", fail_by_exit_value=True)
@task(paramfile=FILE_IN, product=FILE_OUT)
def sim(paramfile, product): pass

products = [f"{WORK}/out{i}.dat" for i in range(2)]
for i, params in enumerate(param_files):
    sim(params, products[i])          # 2 x 4 ranks on one 8-slot node
compss_wait_on_file(products)         # ONCE, after the loop
```

**3. Many nodes, many ranks, one task.** Only the numbers change — COMPSs
spreads the ranks across the hosts in `resources.xml` itself. Put placement
options in `flags=` if the program needs them.

```python
@mpi(binary=EXE, runner=RUNNER, processes=16, processes_per_node=8,
     args="{{paramfile}}", fail_by_exit_value=True)
@task(paramfile=FILE_IN, product=FILE_OUT)
def sim(paramfile, product): pass      # 16 ranks over 2 nodes
```

**4. Many nodes, many ranks, parallel tasks.** Layout 2's submit-then-wait shape
with layout 3's numbers, sized so `len(tasks) x processes <= total_slots`.

## There Is No Pipe. Plan the Data Movement Around That.

A `@binary` task is one program with optional stdin/stdout redirects. No `|`, no
`&&`, no shell. Filtering a shell pipeline would do has to happen in a
downstream `@task`, with the intermediate on disk in between. The intermediate
is unavoidable; its cost is not:

1. **Stream the filter.** Read line by line, apply the cut, keep only the
   columns you need. Never parse the whole file and slice afterwards — a full
   parse pays for 100% of the data to use a few percent of it.
2. **Use float32** when the data is f32 on disk; float64 doubles memory for no
   precision.
3. **`compss_delete_file` the large intermediate** once the filtered product
   exists. Do not `rm -rf` a directory a task also reads from.

Where a tool offers a binary output format instead of text, prefer it — it needs
no shell and no redirect, and is therefore a *better* fit for `@binary` than for
a Parsl `@bash_app`. Which tool and which format is domain knowledge: take it
from the domain skill, not from here.

## A Completed Run Is Not a Successful Run

**`status: completed` and `exit_code: 0` do not mean the workflow worked** —
COMPSs can fail a task while the master exits 0, and that has happened here.
Verify every run: `list_files` the work directory and confirm the deliverable
exists at a plausible size (do not assert on it inside the workflow — a previous
run's output satisfies that check); read the attached `logs`, which on failure
include the newest `~/.COMPSs/<app>_NN/jobs/*` where a failed task's real error
lives; and check `launch_command` contains `-m pycompss`.

## The Four Decorators You Use

From each decorator's `SUPPORTED_ARGUMENTS` in COMPSs 3.4; anything not listed
raises at import. Mandatory marked `*`.

| Decorator | Accepted arguments |
|---|---|
| `@task` | parameter directions, `returns`, `priority`, `time_out`, `on_failure` |
| `@binary` | `binary`*, `working_dir`, `args`, `fail_by_exit_value` |
| `@mpi` | `runner`*, `binary`, `processes`, `processes_per_node`, `working_dir`, `args`, `flags`, `scale_by_cu`, `fail_by_exit_value` |
| `@constraint` | `computing_units` — threads per rank, when the binary is hybrid |

Stacking order outermost first: `@constraint`, `@mpi`/`@binary`, `@task`
innermost — both of the latter require `@task` beneath them. Parameter types
come from `pycompss.api.parameter`: `FILE_IN`, `FILE_OUT`, `FILE_INOUT`, and
`FILE_OUT_STDOUT`. COMPSs has other decorators; if a task cannot be expressed
with these four, report that rather than reaching for one.

## Common Pitfalls

| Pitfall | Rule |
|---|---|
| Writing `project.xml` / `resources.xml` | The server generates them. Read `compss_topology` from `get_resources` instead |
| Writing a `.sh`, `.pbs` or any file other than the workflow `.py` | Two XML files (server) and one Python file (you). Nothing else |
| `@mpi(processes=N)` without `processes_per_node`, or set to anything but `units_per_host` | It defaults to **1**, so N is read as N NODES. 1x8 and 2x4 are both eight ranks and are not interchangeable |
| A `@binary`/`@mpi` task with edge-only parameters and no `args=` | Every parameter lands on argv. Name the real arguments in `args="{{a}} {{b}}"` |
| `runner="mpirun"` as a bare name | Resolved off the worker's thin PATH; exit 127. Use `get_resources`' `mpi_runner`, or `os.environ.get("COMPSS_MPIRUN")` |
| Rebuilding a `FILE_OUT` path, appending an extension, or `copy2`-ing onto one | You get a renamed sandbox path. Write to the parameter you were given; `np.save` and `fig.savefig` both guess at extensions, so use an open handle or pass `format=`. To re-export an existing file, pass it as `FILE_IN` |
| `total_slots: 0` in `compss_topology` | No worker can start; the run is refused. Report the gap and stop |
| Looking for a decorator that sources an env file, or one outside the four here | Neither exists. Report what you cannot express instead |
| Shell placement flags via a wrapper | `@mpi(flags="--map-by ... --bind-to core")` takes them directly |
| `@mpi(processes=N)` with N > `total_slots`, or oversubscribing to make it fit | Never scheduled; reports "not enough slots" or hangs. Oversubscribing wastes the allocation and changes the result while reporting success. Reduce N or report the gap |
| Setting environment variables in the file | The JVM already copied its environment; the assignment is dead code. It belongs in the launching environment |
| Treating `exit_code: 0` as success | COMPSs can fail a task with the master exiting 0. Verify the deliverable and read the job logs |
| Asserting on the simulation's own output inside the workflow | A previous run's output satisfies it. Verify afterwards with `list_files` |
| `fail_by_exit_value` left at `False` | A nonzero exit returns as a value, the task "completes", consumers read a stale file |
| Running a program in its read-only reference tree | A program writing relative to cwd needs `working_dir=` under the work directory, and a writable copy of its inputs |
| An MPI-linked helper binary under `@binary` | It aborts in `MPI_Init` without a launcher. Use a serial build, or `@mpi` |
| `subprocess` anywhere in the file | Takes the work out of COMPSs. CLI work is `@binary`/`@mpi` |
| Expecting a pipe, `&&` or `source` in `@binary` | No shell. Filter in a downstream `@task` |
| `@binary`/`@mpi` without `@task` under it, or a non-`pass` body | `@task` is required and innermost; the body is ignored, arguments come from the signature |
| `args=("flag", "value")` as a tuple | `args` is a **string** with `{{name}}` placeholders |
| `compss_start()` / `compss_stop()` in your file | The launcher already owns the runtime |
| No `main()` / `__main__` guard | The launcher runs the file as a program; nothing happens |
| Passing paths as bare strings, or a producer with no `FILE_OUT` | Invisible to the scheduler, so there is no edge and consumers race the producer. Declare `FILE_IN`/`FILE_OUT` |
| `compss_wait_on()` after every submission | Serializes the graph; wait only where a real value is needed |
| Parsing a whole text intermediate to keep a few percent of it | Stream the filter line by line instead |
| Transcribing input-deck values into module constants | Parse the deck in the task; transcribed values drift from the file the run actually used |
| Rendering without `matplotlib.use("Agg")` | Headless nodes have no display |
| `{Type: FILE_IN}` without importing `Type` | `NameError` at import. Import it, or use the bare `param=FILE_IN` form |
| `install_package("pycompss")` mid-run | It would attempt the full distribution build inside a 900s timeout, which it will not finish, and a half-finished install shadows a good one. If `check_package` says False, REPORT it |
| Reading `installed: True` as "the runtime works" | It means only that the Python half imports. The pip package installs a large Java distribution as a side effect, and that half can be missing; a broken runtime shows up as a failed `run_workflow` with Java errors. `version: unknown` is normal |
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

## MCP Server Behavior

`servers/pycompss_server.py` subclasses `servers/server.py`, the same base class
the Parsl and ADIOS2 servers use. It hosts no runtime. Its contributions are the
launch command, the generated topology, the structural warnings, and extended
log collection.

### Engine-specific behaviour

Beyond the shared tools, this server adds four things. **The topology is
generated before every run** and reported under `compss_topology`; a
`multi_node` plan where no host can start a worker is **refused**, not launched.
**`COMPSS_MPIRUN_TYPE` is set by `launch_env()`** to `openmpi`. **Log collection
reaches `~/.COMPSs`** — the inherited `collect_logs` sees only the work
directory, this one also tails the newest `~/.COMPSs/<app>_NN/jobs/*` where a
failed task's real error lives. And **stdout/stderr stream to
`<run_id>.out`/`.err`** while the run is in flight, so a long run can be watched
with `read_file` rather than waited on; they survive a timeout kill.

### The launch command

```bash
<python> -m pycompss [--project=...] [--resources=...] \
    --python_propagate_virtual_environment=true workflow.py [args]
```

Extra flags come from `MAW_COMPSS_FLAGS`. There is no variable for the launcher:
`python -m pycompss` is a property of the interpreter in `MAW_PYTHON`, so the
runtime, the master and the tasks agree on one environment by construction.
`--python_propagate_virtual_environment=true` activates the venv on the workers;
without it they import whatever `pycompss` a fresh login shell finds, which with
a venv install is nothing.

PyCOMPSs here is a pip install in that virtualenv and nothing else — no
hand-built COMPSs, no `COMPSS_HOME` exported by the server, no `runcompss` on
`PATH`. `spack env activate <repo>` puts the venv on `PATH` and sets
`JAVA_HOME`; do **not** follow it with `venv3/bin/activate`, which restores the
pre-venv `PATH` and drops `JAVA_HOME`'s bin, the compiler and `mpirun`. A
half-activated environment is the one reliable way to get "pycompss is not
installed" on a machine where it is. There is no fallback mode: if the runtime
cannot start the run fails and says so, rather than running the file
sequentially and reporting a COMPSs run.
