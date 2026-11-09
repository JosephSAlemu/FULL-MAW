---
name: knowledge/lcrc
description: >
  Runtime environment knowledge for running MAW on the Argonne LCRC cluster using
  a Python venv. Covers HPC execution model, resource queries, how MPI is provided
  (by a module the domain skill names), MPI launchers, storage paths, and what
  differs from local execution. Load when --env hpc is set or when goal mentions
  LCRC, Argonne, cluster, or HPC.
---

# LCRC — HPC Environment Knowledge Skill

## When to Use This Skill

Loaded automatically when `--env hpc` is set. Also load when the goal mentions
LCRC, Argonne, cluster, or HPC explicitly.

---

## Execution Model

MAW runs as a Python process on an LCRC login or compute node. The MCP server
executes tasks using the local Python venv on that node. When running inside a
PBS job, MPI tasks use a launcher to spread work across allocated ranks.

```
LCRC compute node (inside an allocation)
└── agent process (LangGraph, in project venv)
    └── MCP server subprocess
        └── tasks run in venv
            └── MPI tasks launched via: mpirun -n <ranks> <executable>
```

Activate the project venv before running:

```bash
source /path/to/MCP_Approach/setup_hpc.sh
python academy_agents.py --env hpc --paper 1 --goal "..."
```

---

## MPI Comes From a Module

**No MPI is configured before the run.** `setup_hpc.sh` sets no MPI variables and
no `LD_LIBRARY_PATH`, and the Spack environment contains nothing MPI-linked. This
is deliberate: which MPI a run needs is a property of the tools it runs, not of
the machine, so nothing picks one in advance.

MPI arrives one way only — **a module, loaded by the task that needs it**:

| Case | What provides MPI |
|---|---|
| The domain skill names a module | that module, e.g. `module load lammps` brings its own MPI and compiler |
| The domain skill names none | the cluster's default MPI module |

A module that provides a tool usually provides the MPI that tool was built
against, and says so when it loads. Read the load message: it names the compiler
and MPI versions it requires, and warns that it will unload others.

### Loading it

The module load belongs **in the task's own command string**, in the same
process as the launcher it provides:

```bash
module load <module> && cd /app/work/run0/<subdir> && mpirun -n 4 <program>
```

Two reasons it goes there rather than in a shell beforehand. A module loaded in
the agent's shell is inherited by every task, including ones that want a
different MPI. And a module loaded *after* a shell was set up silently replaces
whatever that shell had, with no warning beyond its own load message.

In a generated workflow file this means the `module load` is part of the command
the engine runs — see the engine skill for the construct.

### Python bindings for MPI

If the ranks run a Python program, its MPI bindings must come from the **same**
MPI as the library it drives. That is straightforward here: load the module
first, then `pip install` the bindings. pip compiles them against whatever
`mpicc` resolves to, which after `module load` is that module's MPI.

So `mpi4py` in `requirements.txt` is correct and expected whenever a task's ranks
are Python. It is only wrong if it was installed *before* the module was loaded,
against a different MPI — in which case reinstall it rather than working around
it:

```bash
module load <module>
which mpicc                 # confirm it is the module's
pip install --no-binary mpi4py --force-reinstall mpi4py
```

`--no-binary` matters: a prebuilt wheel was compiled elsewhere and cannot adapt
to the loaded module.

### Stale MPI variables break the next launcher

A process that has already initialized MPI exports variables describing that
singleton job -- `PMI_SIZE`, `PMI_RANK`, `I_MPI_HYDRA_BOOTSTRAP`, and Open MPI's
`OMPI_MCA_ess=singleton` and `PMIX_*` equivalents. Those are inherited by every
child, and a real launcher started underneath them refuses to initialize: it
believes it is already inside an MPI job. The abort happens in `MPI_Init` and
reaches the shell as exit code **143**.

Two things cause it. A driver that imports MPI bindings at module scope becomes
a singleton MPI process and sets them for everything it spawns -- note that
`os.environ` in that process will NOT show them, because the bindings set them
with C-level `setenv` after the interpreter took its snapshot, so a check inside
Python reports nothing while every child inherits them. And a module loaded
earlier may leave its own behind.

Unset them in the task's own command, immediately before the launcher, rather
than trying to guarantee nothing ever set them:

```bash
env -u PMI_SIZE -u PMI_RANK -u I_MPI_HYDRA_BOOTSTRAP <launcher> -n <ranks> <program>
```

The related rule is to keep MPI bindings out of module scope in any file a
launcher re-invokes -- see the engine skill.

### Reading the exit code

MPI abort codes are truncated mod 256 on the way to the shell, so a failed task's
exit code is not the abort code — decode with `code & 0xFF` before concluding
anything. `Abort(1091215)` arrives as **143**, which is `MPI_Init` failing.

A bare "failed with unix exit code N" says nothing on its own. Read the task's own
stderr first, and when a tool's screen output is suppressed its errors go to its
log file rather than stderr, so check both.

**A nonzero code from a program that already produced its output is often a
cleanup fault, not a failed run.** A segfault while tearing down -- freeing a
communicator after MPI is already down, say -- arrives as exit 11 long after
every output file was written. Check for the expected output before concluding
the step failed, and propagate a failure only when the output is genuinely
absent. The reverse also holds: see the note above about a launcher that did not
take, which exits 0 having done the work N separate times.

---

## Resource Awareness

The explorer must call `get_resources` as its **first tool call**. If `in_pbs`
is `false` in the response, stop immediately and tell the user to start an
interactive allocation before re-running the agent. Do not attempt any compute
task outside an allocation on LCRC.

`get_resources` reports what this server can see:

| Field | Meaning |
|---|---|
| `in_pbs` | whether an allocation exists |
| `nnodes` | how many nodes are available |
| `ntasks` | total rank slots across all of them |
| `ranks_per_node` | rank slots on each node |
| `nodelist` | the node names |
| `launcher` | the launcher to call |

Those numbers go into a generated workflow file **as constants**, written from
what the tool returned. The workflow file itself discovers nothing.

**The allocation is a fixed budget.** Concurrent tasks × ranks each must not
exceed `ntasks`. Asking for more does not queue — every launcher started from the
same place claims slots from the front of the same list, so two concurrent
launchers both take the first ranks and land on the same cores. Either confine
each to its own cores (`taskset -c`, one disjoint range per task) or run fewer at
once. When a goal asks for more work than the allocation holds, reduce the ranks
per task or run in batches; tell the user what the allocation would need to run it
all at once.

---

## Running MPI Commands

Use `mpirun`/`mpiexec` on LCRC. Do not use `srun` — that is specific to
SLURM-based clusters.

```bash
# After the module is loaded, its own launcher is on PATH
module load <module> && mpirun -n <ranks> <program> <args>
```

You never type these commands into a shell yourself — they go into the generated
workflow file, and the engine runs them. How depends on the engine:

- **Parsl**: a `@bash_app` whose body returns the command string.
- **PyCOMPSs**: an `@mpi(binary=..., runner="mpirun", processes=N)` task —
  `processes=` carries the rank count, so no launcher text is written.
- **ADIOS2**: a stage function that calls `subprocess` internally, since ADIOS2
  has no launcher of its own.

Size the rank count from `get_resources` and the goal rather than hardcoding it.

**Verify the rank count from the program's own output.** A launcher that did not
take produces N independent one-rank jobs that overwrite each other's output and
exit 0 — the exit code says nothing. Most MPI programs report their task count
and decomposition in their log; read it.

---

## Parsl Config

For Parsl config, load the `systems/parsl` skill — the config shapes for one node
or many, where per-app logs land, and how concurrent launchers divide an
allocation are documented there.

---

## Paths

| Alias | Resolves to |
|---|---|
| `/app/` | repo root on disk (`MAW_REPO_ROOT`) |
| `/app/data/` | input data files (`MAW_DATA_DIR`) |
| `/app/work/run0/` | working directory for all run output (`MAW_WORK_DIR`) |

The MCP server rewrites these prefixes when it writes or reads a file, so write
`/app/`-rooted paths and never a machine-specific absolute path. Anything
written outside the working directory is refused.

---

### The working directory

**Every command runs from `/app/work/run0/`, and every file it writes goes
there.** That directory is the one the run owns: the MCP server confines writes
to it, and it is what gets archived as the run's output.

This matters most for a program that writes its output **relative to the
directory it was started in**. Such a program must be launched from
`/app/work/run0/` — a reference or sample directory elsewhere on the filesystem
is usually read-only, and the run stops with a permission error the moment the
program tries to create its first output folder. Read inputs from wherever they
live; write from `/app/work/run0/`.

A `cd` in a command string should therefore be to `/app/work/run0/`, or to a
subdirectory of it, and essentially never to a shared or reference tree. If a
program needs a configuration or parameter file that lives elsewhere, pass it
by absolute path rather than moving the working directory to reach it.

With several concurrent tasks, each gets its own subdirectory. Two tasks writing
relative paths from the same directory overwrite each other's output, and because
each one succeeds, nothing reports it.

On a shared filesystem this is not a style preference. A project directory you
can read is often one you cannot write: `/lcrc/project/<project>/` holds shared
reference and sample trees owned by other people, and a program launched from
one of those will fail on its first write no matter how correct the rest of the
command is.

---

## Venv Setup (Do Once on Login Node)

Login nodes have internet access; compute nodes may not. Build the venv there:

```bash
python3 -m venv /lcrc/project/<project>/venv
source /lcrc/project/<project>/venv/bin/activate
pip install -r requirements.txt
```

Then activate it in your job script before launching MAW.

Anything that compiles against MPI — `mpi4py`, a package with MPI-linked
extensions — must be pip-installed **after** the relevant module is loaded, so
the compiler it finds is that module's. Installing first and loading later gives
bindings built against something else.

---

## Compute Node Notes

- No display server — headless rendering required:
  - `LIBGL_ALWAYS_SOFTWARE=1`
  - `PYOPENGL_PLATFORM=osmesa`
  - Set these **before** importing the rendering library, which picks its
    backend at import time
- Set `OMP_NUM_THREADS` explicitly for anything running more than one process at
  a time. Unset, a numerical library commonly starts one thread per visible core
  *per process*, so the threads contend instead of computing.
- Do not use `module load` for Python — use the project venv
- Do use `module load` for a tool the domain skill names, and for the MPI that
  comes with it
- Login nodes are for building the venv and short probes only. Reproduce a
  failure there if it helps; run the work inside an allocation.

---

## Stack Rules

- All Python packages installed via pip into the project venv. That includes
  packages that drive an MPI-linked tool, and includes `mpi4py` — install them
  after loading the module, so they are built against its MPI.
- MPI itself is never pip-installed and is never configured by a setup script.
  It comes from a module: the one the domain skill names, or the cluster default.
- Do not recommend conda, `module load anaconda3`, or conda-forge packages