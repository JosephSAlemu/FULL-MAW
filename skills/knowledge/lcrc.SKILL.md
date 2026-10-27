---
name: knowledge/lcrc
description: >
  Runtime environment knowledge for running MAW on the Argonne LCRC cluster using
  a Python venv. Covers HPC execution model, PBS resource variables, mpirun usage,
  storage paths, and what differs from local execution. Load when --env hpc is set
  or when goal mentions LCRC, Argonne, cluster, or HPC.
---

# LCRC — HPC Environment Knowledge Skill

## When to Use This Skill

Loaded automatically when `--env hpc` is set. Also load when the goal mentions
LCRC, Argonne, cluster, or HPC explicitly.

---

## Execution Model

MAW runs as a Python process on an LCRC login or compute node. The MCP server
executes tasks using the local Python venv on that node. When running inside a
PBS job, MPI tasks use `mpirun` to spread work across available ranks.

```
LCRC compute node (inside PBS job)
└── agent process (LangGraph, in project venv)
    └── MCP server subprocess (parsl_server.py)
        └── tasks run in venv
            └── MPI tasks launched via: mpirun -np $PBS_NP <executable>
```

Activate the project venv and set MPI library paths before running:

```bash
source /path/to/MCP_Approach/setup_hpc.sh
source /path/to/MCP_Approach/venv3/bin/activate
python agent_mcp.py --env hpc --paper 1 --goal "..."
```

`setup_hpc.sh` sets `LD_LIBRARY_PATH` to include the Intel MPI shared libraries
(`libmpi.so.12`, `libfabric.so.1`) required by the LAMMPS Python bindings. Without
it, `from lammps import lammps` fails with a missing shared library error.

No conda, no `module load anaconda3`. The venv is the execution environment.

---

## Resource Awareness

The explorer must call `get_resources` as its **first tool call**. If `in_pbs`
is `false` in the response, stop immediately and tell the user to start a PBS
interactive job before re-running the agent. Do not attempt any compute task
outside a PBS allocation on LCRC.

The server reads these PBS variables:

| Variable | Meaning |
|---|---|
| `PBS_JOBID` | Set when running inside a PBS job |
| `PBS_NUM_NODES` | Number of allocated nodes |
| `PBS_NP` | Total MPI ranks across all nodes (nodes × ppn) |
| `PBS_NUM_PPN` | Processors per node |
| `PBS_NODEFILE` | Path to file listing allocated hostnames (one per slot) |

If these are not set (login node or non-PBS context), the server falls back to:
`nnodes=1`, `ntasks=1`.

---

## Running MPI Commands

Use `mpirun` on LCRC/PBS. Do not use `srun` — that is specific to SLURM-based clusters.

```bash
# LAMMPS across all allocated ranks
mpirun -np $PBS_NP lmp -in script.in

# Python MPI program
mpirun -np $PBS_NP python3 my_parallel_script.py

# Explicit rank count
mpirun -np 32 lmp -in script.in
```

You never type these commands into a shell yourself — they go into the
generated workflow file, and the engine runs them. How depends on the engine:

- **Parsl**: a `@bash_app` whose body returns the command string.
- **PyCOMPSs**: an `@mpi(binary=..., runner="mpirun", processes=N)` task —
  `processes=` carries the rank count, so no `mpirun` text is written.
- **ADIOS2**: a stage function that calls `subprocess` internally, since ADIOS2
  has no launcher of its own.

Use `$PBS_NP` (via `get_resources`) to size the rank count rather than
hardcoding it.

---

## Parsl Config

For Parsl config, load the `systems/parsl` skill — the HPC PBS config (scaled to `PBS_NUM_NODES`) is documented there.

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

---

## Compute Node Notes

- No display server — headless rendering required:
  - `LIBGL_ALWAYS_SOFTWARE=1`
  - `PYOPENGL_PLATFORM=osmesa`
- Do not use `module load` for Python — use the project venv
- Load system modules only for libraries not available via pip (e.g. vendor MPI, CUDA)

---

## Available Clusters

| Cluster | Hardware | Notes |
|---|---|---|
| Bebop | CPU, Intel Xeon | General compute |
| Swing | GPU, NVIDIA A100 | GPU workloads |
| Improv | CPU | General compute |

---

## Stack Rules

- All Python packages installed via pip into the project venv
- MPI-capable executables (LAMMPS, etc.) must be built with MPI support enabled
- Do not recommend conda, `module load anaconda3`, or conda-forge packages