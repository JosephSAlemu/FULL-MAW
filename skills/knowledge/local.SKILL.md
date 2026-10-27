---
name: knowledge/local
description: >
  Runtime environment knowledge for running MAW on a local machine (single node, no HPC).
  Covers execution constraints, path conventions, and what agents must NOT recommend.
  Load when --env is local (the default) or when no cluster environment is specified.
---

# Local Machine Environment — Knowledge Skill

## When to Use This Skill

Loaded automatically when `--env local` is set (the default). Applies to developer
machines, laptops, and any single-node execution outside of a cluster.

---

## Execution Model

MAW runs as a Python process on the local host. The MCP server executes tasks using
the local Python venv. All tasks run in a single process on a single machine.

```
Host machine (single node)
└── agent process (LangGraph)
    └── MCP server subprocess (parsl_server.py)
        └── tasks run in local venv
```

---

## Constraints — What Agents Must Not Recommend

- No PBS, LSF, or any job scheduler
- No MPI across nodes, no `mpirun`, no `srun`, no `mpi4py`
- No multi-node Parsl configs (no `PBSProProvider`, no `LSFProvider`)
- No GPU unless explicitly confirmed available on the host
- No `module load` commands — packages come from the venv

Use serial or thread-parallel single-process approaches only.

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


---

## Parsl Config

For Parsl config, load the `systems/parsl` skill — the local single-node config is documented there.

---

## Stack Rules

- All packages installed via pip into the local venv
- Never include: `mpi4py`, OpenMPI, MPICH, or any MPI-dependent package
- Headless rendering requires: `LIBGL_ALWAYS_SOFTWARE=1`, `PYOPENGL_PLATFORM=osmesa`