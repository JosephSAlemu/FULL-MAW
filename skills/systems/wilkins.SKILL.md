---
name: systems/wilkins
description: >
  Wilkins in situ workflows for MAW: the YAML config IS the workflow, the
  LowFive check, the rank layouts, and what stops a coupled workflow degrading
  to file I/O. Load for --engine wilkins; config manual is knowledge/WILKINS.
---

# Wilkins — System Skill

Wilkins couples tasks in situ: data moves in memory over MPI instead of through
files, because LowFive intercepts the tasks' ordinary HDF5 calls. So the task
programs contain **no Wilkins and no LowFive API** — plain HDF5 or plain `h5py`.

Deliverable: a **YAML config** written with `write_config` — that file IS the
workflow — plus each task's program. `run_workflow(filename="wilkins.yaml",
ranks=0)` launches the driver.

## Call `check_lowfive` FIRST

LowFive is switched on by two environment variables, not by the config. With
them unset HDF5 uses its native VOL, every task writes a real `.h5` to disk, and
the workflow **completes having transported nothing in memory, with no error
anywhere**. That is why it has to be checked rather than observed.

| Variable | Value |
|---|---|
| `HDF5_VOL_CONNECTOR` | `lowfive under_vol=0;under_info={};` |
| `HDF5_PLUGIN_PATH` | the directory holding `liblowfive.*` |

`module load wilkins/<version>` (or `spack load wilkins`) sets both. The server
refuses to launch when they are wrong, so a failure here is an environment gap.

## Rules

- The config filename must end in `.yaml` — `.yml` is rejected outright.
- Every task needs `func` and `nprocs`. Both are required and unguarded: omit
  either and the parser throws.
- `metadata: 1` transports a dataset in memory; `passthru: 1` writes it to a
  real file. **Both 0 is a hard error.** Defaults are `metadata: 1`,
  `passthru: 0`, so omitting them means in-memory.
- Set the SAME mode on both sides of a pair. On a mismatch the **producer wins**
  and the consumer is silently coerced, with only a warning.
- Ports are matched into channels by `filename` + dataset `name`, with **globs**
  (`*`, `?`) — not regexes. You never declare edges.
- A `.py` task is imported as a module and **must define `main()`**; without one
  the task silently does nothing. A non-`.py` task must be a Henson-linked,
  position-independent shared object named `./<name>.hx` — a plain MPI binary
  cannot be a task.

## 1. One node, serial (one rank each)

```yaml
tasks:
  - func: "./sim.hx"
    nprocs: 1
    outports:
      - filename: "*.h5"
        dsets: [{name: /group1/grid, metadata: 1, passthru: 0}]
  - func: "./ana.hx"
    nprocs: 1
    inports:
      - filename: "*.h5"
        dsets: [{name: /group1/grid, metadata: 1, passthru: 0}]
```

## 2. One node, many ranks, parallel

Raise `nprocs` per task so the ranks sum to the node's slots. A parallel task
opens HDF5 with its **task-local** communicator, not `COMM_WORLD`. Use
`nwriters: K` to have only the first K ranks expose data, which is how a wide
simulation couples through a few ranks instead of all of them.

```yaml
  - func: "./sim.hx"
    nprocs: 6
    nwriters: 2
```

## 3 & 4. Many nodes, serial and parallel

Nothing in the YAML changes — ranks are contiguous blocks over one
`MPI_COMM_WORLD` and the launcher spreads them; only the total grows. Size
`nprocs` so that total fits the allocation. There are no node-placement controls
in the config at all; binding is the launcher's job.

## Rank accounting — the one number to get right

The launcher's rank count is **the sum over tasks of `taskCount x nprocs`**
(`taskCount` defaults to 1). The server reads it from the config, so pass
`ranks=0`. Too few ranks does not run a smaller workflow — it starves the tasks
at the tail, which never run; extra ranks sit idle.

## Performance and hygiene

| Do | Why |
|---|---|
| State `nprocs` on every task | it is required, and it is what the rank total is computed from |
| Use `metadata: 1` unless you need the file | passthru writes to disk, which is the thing in situ coupling exists to avoid |
| Consumer `io_freq: N` serves every Nth round | an analysis not needing every step otherwise throttles the producer |
| Give each pair a distinct `filename`/`name` glob | the match is the channel; a glob that catches two producers couples the wrong pair |
| Check every inport has a producing outport | in memory mode that is a hard error, but in passthru mode it degrades to plain file I/O with no error and no data |
