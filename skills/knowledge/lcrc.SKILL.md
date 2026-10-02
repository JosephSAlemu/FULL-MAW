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

You never type these commands into a shell yourself — they are handed to the
workflow engine, which runs them. How depends on the engine:

- **Parsl**: there is no `submit_mpi_task`. The command goes inside a `@bash_app`
  in the generated workflow file, which returns it as a string for Parsl to run.
- **Other engines**: use the `submit_mpi_task` MCP tool — it reads `PBS_NP`
  automatically and prepends `mpirun -np N`, so you don't hardcode the rank count.

---

## Parsl Config

For Parsl config, load the `systems/parsl` skill — the HPC PBS config (scaled to `PBS_NUM_NODES`) is documented there.

---

## Storage Paths

| Path | Use for |
|---|---|
| `/lcrc/project/<project>/` | Project repo, venv, results — persistent, backed up |
| `/lcrc/globalscratch/<username>/` | Large intermediate output — shared, may be cleared during maintenance |
| `/scratch` | 15 GB node-local scratch — cleared after job ends, never store venv here |

Store the project repo and venv under `/lcrc/project/`.

**In task code and planner task lists, always use `/app/` paths** — the MCP server
resolves them to the actual repo location automatically. Never write `/lcrc/project/`
or any absolute cluster path in task instructions or Python code.

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
- `mpi4py` is allowed and expected for Python-level MPI programs
- MPI-capable executables (LAMMPS, etc.) must be built with MPI support enabled
- Do not recommend conda, `module load anaconda3`, or conda-forge packages
