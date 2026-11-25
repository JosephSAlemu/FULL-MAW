---
name: knowledge/PyCOMPSs
description: >
  PyCOMPSs API reference: @task parameter directions and types, the @binary,
  @mpi, @multinode and @constraint argument lists, the compss_* synchronisation
  and deletion calls, and the python -m pycompss launcher. Load alongside
  systems/pycompss, which carries the MAW execution contract (write_file/
  run_workflow, the rank layouts, the pitfalls and the runtime lifecycle) that
  this manual serves.
---

# PyCOMPSs — API Reference

The companion to `systems/pycompss`. That skill states the rules every generated
driver must obey; this one is the API manual you consult while writing it. If
anything here seems to permit what `systems/pycompss` forbids, the contract
wins.

Signatures were read from the **COMPSs 3.4** Python binding source
(`pycompss.api.*`). The installed runtime may differ;
`check_package("pycompss")` reports whether the package imports (its version
is often `unknown`). Where behaviour on the machine disagrees with this file,
the installed runtime wins.

---

# API

## `@task` — parameter directions and types

```python
from pycompss.api.task import task
from pycompss.api.parameter import FILE_IN, FILE_OUT, FILE_INOUT
```

`@task(name=DIRECTION, ..., returns=..., priority=..., time_out=...,
on_failure=...)`. An unlisted parameter is passed by value as an input. Any
other keyword raises at import.

| Type | Meaning |
|---|---|
| `IN`, `OUT`, `INOUT` | Python object read / produced / modified in place |
| `FILE_IN`, `FILE_OUT`, `FILE_INOUT` | Path to a file read / produced / modified |
| `DIRECTORY_IN`, `DIRECTORY_OUT`, `DIRECTORY_INOUT` | Same, for a directory |
| `COLLECTION_IN`, `COLLECTION_OUT`, `COLLECTION_INOUT` | A list whose elements are tracked individually |
| `FILE_OUT_STDOUT`, `FILE_IN_STDIN`, `FILE_OUT_STDERR` | Redirect a stream to or from the file; used with `@binary`/`@mpi` as `{Type: FILE_OUT_STDOUT}` |
| `CONCURRENT`, `FILE_CONCURRENT`, `COMMUTATIVE`, `FILE_COMMUTATIVE` | Relaxed `INOUT`: tasks may update the same data concurrently or in any order |

Other keywords on `@task`:

| Keyword | Meaning |
|---|---|
| `returns` | Number of return values (`returns=1`) or a type; the call then yields a future to pass on or synchronise |
| `priority` | Boolean; schedule ahead of other ready tasks |
| `time_out` | Seconds before the task is killed |
| `on_failure` | `'RETRY'` (default), `'IGNORE'`, `'CANCEL_SUCCESSORS'` or `'FAIL'` |

Dependencies are derived from these declarations alone. A task's *return value*
is a future: passing it to another task is an edge, no `compss_wait_on` needed.

## `@binary`, `@mpi`, `@multinode`, `@constraint`

Each is stacked above `@task`; `@constraint` goes above the others. The accepted
argument sets (anything else raises at import):

| Decorator | Arguments (`*` mandatory) |
|---|---|
| `@binary` | `binary`*, `working_dir`, `args`, `fail_by_exit_value` |
| `@mpi` | `runner`*, `binary`, `processes`, `processes_per_node`, `working_dir`, `args`, `flags`, `scale_by_cu`, `fail_by_exit_value` |
| `@multinode` | `computing_nodes`, `processes_per_node` |
| `@constraint` | `computing_units` (cores reserved per task); other resource keys exist and are not covered here |

`@mpi`: `processes` is the TOTAL rank count. `@task` divides it by
`processes_per_node` to get the node count, so `processes` must be `>=` and a
multiple of `processes_per_node` or `PyCOMPSsException` is raised. The default
`processes_per_node=1` makes `processes` a node count; the contract's rule to
set it explicitly follows from that.

`@multinode` is a different thing: it reserves nodes for a Python task body and
has **no `binary=`**, so it cannot launch a program (use `@mpi` or `@binary`
for that). Its `computing_nodes` is used as given — it is NOT divided by
`processes_per_node`, which defaults to 1 in the code even where the docs say
otherwise. Pass both explicitly.

`@constraint(computing_units=N)` sets how many cores one task occupies on its
node; it is not a rank count. For `@mpi` it is the threads-per-rank figure, see
`systems/pycompss`.

---

## Synchronisation

Import from `pycompss.api.api`. The main program is sequential code that
submits tasks; these calls bring results back to it.

### `compss_wait_on(*obj, mode='rw')`
Waits for the last version of each object and returns it (one object in, one
out; several in, a list out). Rebind the result:
`obj = compss_wait_on(obj)`. Pass `mode='r'` when the master will not modify it.
A list or dict of futures is synchronised recursively, element by element:

```python
results = [step(i) for i in range(10)]   # each call returns a future
results = compss_wait_on(results)        # one call, after the loop
```

### `compss_wait_on_file(*file_name)`
Waits until the last version of the file(s) has been produced and brought to
the master; returns the name(s). Accepts a path, or a list or tuple of paths.

### `compss_wait_on_directory(*directory_name)`
Same, for directories.

### `compss_open(file_name, mode='r')`
Like `open()`, after synchronising the last version of the file. Use it to read
a task's `FILE_OUT` from the master.

### `compss_barrier(no_more_tasks=False)`
Blocks until every task submitted so far has finished and returns nothing. It
creates no dependency; pass `no_more_tasks=True` on the last barrier.

```python
sim(params, "out.dat")                   # @task(product=FILE_OUT) underneath
compss_wait_on_file("out.dat")           # master needs the file now
with compss_open("out.dat") as fh:
    head = fh.readline()
```

## Deletion

### `compss_delete_file(*file_name)`
Tells the runtime to delete the file(s) everywhere. Asynchronous: it happens
once no pending task still needs them. Returns `True` on success.

### `compss_delete_object(*object)`
Same for the files backing an object. Collections are not supported.

---

## Launching

`python -m pycompss [run|enqueue] <runcompss flags> app.py [app args]`. The
action defaults to `run` and the rest is the `runcompss` flag set passed through
verbatim. `run_workflow` builds this command; the server owns the flags (see
`systems/pycompss`).

The shipped `COMPSS_MPIRUN_TYPE` values are `impi`, `openmpi` and `slurm`; any
other value is silently ignored.

## Topology XML

A `<MasterNode>` holding `<Processor><ComputingUnits>` plus an empty
`<ResourcesList>` is COMPSs' own single-node topology: the master runs tasks
itself. A **bare** `<MasterNode/>` contributes no slots at all. The server
writes both files; this is only for reading one it produced.

## Source rules

The COMPSs docs discourage relative imports in the application; use absolute
imports.
