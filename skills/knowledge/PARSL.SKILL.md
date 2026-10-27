---
name: knowledge/PARSL
description: >
  Parsl Python API reference: the runtime entry points (load/clear/dfk), the
  @python_app/@bash_app/@join_app decorators and their special keyword
  arguments, AppFuture and DataFuture, File, and the Config/executor/provider/
  launcher classes. Load alongside systems/parsl, which carries the MAW
  execution contract (write_file/run_workflow, the @bash_app/@python_app rules,
  and the config the generated driver must build) that this manual serves.
---

# Parsl — Python API Reference

The companion to `systems/parsl`. That skill states the rules every generated
driver must obey; this one is the API manual you consult while writing it.

Nothing here overrides the contract. If a snippet below appears to permit
something `systems/parsl` forbids -- a `subprocess` call, an `mpirun` string
outside a `@bash_app` body, a driver that never loads a config -- the contract
wins. Note that `write_file` no longer refuses these for you: it rejects only a
syntax error and reports the rest as warnings, so a file that takes the work
outside Parsl will be written and will run. The contract still binds.

Signatures below were read from **parsl 2026.10.19**. Defaults and parameter
names change between releases, and the installed version may differ -- a cluster
pinned to 2026.08.10, for instance, is a different release from this one.
`check_package("parsl")` reports what is actually installed; when a signature
here disagrees with that version, the installed one wins.

---

# API

## Runtime entry points

### `parsl.load(config: Optional[Config] = None) -> DataFlowKernel`
Starts the DataFlowKernel and the executors the config declares. Must be called
before any app is invoked. Called with no argument, Parsl uses a built-in
single-node default; the contract requires an explicit `Config`.

### `parsl.clear() -> None`
Shuts the DataFlowKernel down and releases the executors. Call it at the end of
the driver, in a `finally`, so a failing app still tears the runtime down.

### `parsl.dfk() -> DataFlowKernel`
The currently loaded DataFlowKernel. Rarely needed in a generated driver.

### `parsl.wait_for_current_tasks() -> None`
Blocks until every submitted task has finished. An alternative to resolving the
final future when the driver has no single terminal result.

### `parsl.AUTO_LOGNAME`
Sentinel (`-1`) for `stdout`/`stderr`. Parsl picks a per-task filename under its
`run_dir`, in `task_logs/`. Preferred over a hand-chosen path: two tasks cannot
collide, and the file is named for the task that wrote it.

---

## App decorators

### `@python_app(function=None, data_flow_kernel=None, cache=False, executors='all', ignore_for_cache=None)`
Runs the decorated function on a worker. The body must import everything it
uses: a worker process does not share the driver's namespace. Returns an
`AppFuture` immediately.

### `@bash_app(function=None, data_flow_kernel=None, cache=False, executors='all', ignore_for_cache=None)`
The decorated function **returns a command string**, which Parsl runs in a
shell. Its Python body is not the work -- it only composes the string. Returns
an `AppFuture` whose `.result()` is the exit code.

A `@bash_app` that returns `None` raises `BashAppNoReturn`. A non-zero exit
raises `BashExitFailure`.

### `@join_app(function=None, data_flow_kernel=None, cache=False, ignore_for_cache=None)`
For an app whose work is to launch other apps. The body returns a future (or
list of futures) and the join app's own future completes when those do. Use it
when the shape of the sub-DAG depends on a value computed at run time.

### Shared parameters

| Parameter | Meaning |
|---|---|
| `cache=True` | Memoize on the argument list; a repeat call returns the cached result rather than re-running |
| `ignore_for_cache=[...]` | Parameter names excluded from the cache key -- for arguments that change without changing the result |
| `executors=['label']` | Restrict this app to named executors. Default `'all'` |

---

## Special keyword arguments

Accepted by any app, consumed by Parsl rather than passed to the function body.

| Keyword | Type | Effect |
|---|---|---|
| `inputs` | sequence of `File`/futures | Declares input dependencies. A future here is resolved before the app runs, which is how edges are formed |
| `outputs` | sequence of `File` | Declares products. Parsl returns a `DataFuture` per entry on `.outputs` |
| `stdout` | path or `AUTO_LOGNAME` | Where the command's stdout goes (`@bash_app`) |
| `stderr` | path or `AUTO_LOGNAME` | Where its stderr goes. Parsl also writes the executed command string here, between `--> executable follows <--` markers |
| `walltime` | seconds | Per-app timeout; exceeding it raises `AppTimeout` |

Inside the body, read paths off the objects rather than rebuilding them:
`outputs[0].filepath` is the path the command must write, and it is the same
path Parsl will then track.

---

## Futures

### `AppFuture`
Returned by every app call. A `concurrent.futures.Future` plus:

| Member | Meaning |
|---|---|
| `.result()` | Blocks until done, returns the value (exit code for `@bash_app`) |
| `.outputs` | List of `DataFuture`, one per declared output |
| `.stdout` / `.stderr` | Resolved paths of the stream files |
| `.exception()`, `.done()`, `.running()`, `.cancel()`, `.cancelled()` | Standard future protocol |
| `.task_status()`, `.tid` | Task state and id, for diagnostics |

### `DataFuture`
One per declared output, from `.outputs`. Completes when the producing app does.

| Member | Meaning |
|---|---|
| `.filepath` | Full path of the file |
| `.filename` | Basename only |
| `.result()` | Blocks, returns the `File` |

Passing `inputs=producer.outputs` is the idiomatic edge: the consumer waits on
the files, not merely on the producer's exit.

---

## `File(url)`

### `parsl.data_provider.files.File(url: os.PathLike | str)`
A location-independent handle on a file.

| Attribute | For `File("/tmp/x/y.txt")` |
|---|---|
| `.filepath` | `/tmp/x/y.txt` |
| `.filename` | `y.txt` |
| `.scheme` | `file` |

A bare path gets scheme `file`. `http://`, `https://`, `ftp://` and `globus://`
URLs are also accepted and are staged by the executor's `storage_access`.

---

## Configuration

### `Config(...)`
Key parameters for a generated driver:

| Parameter | Default | Meaning |
|---|---|---|
| `executors` | `None` | The executors to start. Required in practice |
| `run_dir` | `'runinfo'` | Where Parsl writes block logs, submit scripts and `task_logs/`. Relative to the process cwd unless set absolutely |
| `retries` | `0` | Automatic retries per task |
| `retry_handler` | `None` | `(Exception, TaskRecord) -> float` deciding whether and how long to wait before retrying |
| `strategy` | `'simple'` | Block scaling strategy; `'none'` pins the block count to the provider's settings |
| `strategy_period` | `5` | Seconds between scaling decisions |
| `max_idletime` | `120.0` | Seconds an idle block survives before `'simple'` scales it in |
| `initialize_logging` | `True` | Whether Parsl configures logging itself |
| `app_cache` / `checkpoint_mode` | — | Memoization and checkpointing, when `cache=True` apps are used |

### `HighThroughputExecutor(...)`
The general-purpose executor.

| Parameter | Default | Meaning |
|---|---|---|
| `label` | `'HighThroughputExecutor'` | Name used by `executors=[...]` on an app |
| `provider` | `None` | Where blocks come from |
| `max_workers_per_node` | `None` | Hard cap on workers per node. **This is what expresses "one wide slot"** |
| `cores_per_worker` | `1.0` | Cores each worker claims. Parsl divides detected cores by this to decide the worker count, so a large value alone does **not** give one worker -- pair it with `max_workers_per_node=1` |
| `mem_per_worker` | `None` | GB per worker, as a second cap |
| `cpu_affinity` | `'none'` | `'block'` or `'alternating'` to pin workers to cores |
| `working_dir` | `None` | Worker cwd |
| `worker_debug` | `False` | Verbose worker logs |
| `encrypted` | `True` | Encrypt the worker/interchange channel |

### Providers

#### `LocalProvider(nodes_per_block=1, launcher=SingleNodeLauncher(), init_blocks=1, min_blocks=0, max_blocks=1, worker_init='', cmd_timeout=30, parallelism=1)`
Runs blocks on the current machine. The right provider for a driver that is
already inside an allocation -- it does not submit anything.

#### `PBSProProvider(account=None, queue=None, scheduler_options='', select_options='', worker_init='', nodes_per_block=1, cpus_per_node=1, init_blocks=1, min_blocks=0, max_blocks=1, parallelism=1, launcher=SingleNodeLauncher(), walltime='00:20:00', cmd_timeout=120)`
Submits blocks with `qsub`. The select line is built as:

```
#PBS -l select=${nodes_per_block}:ncpus=${cpus_per_node}${select_options}
```

so `ncpus` comes from `cpus_per_node` and `select_options` is appended after
it. Both default to 1 per chunk. Putting `ncpus=` in `select_options` as well
produces a duplicate in the same chunk and `qsub` rejects the job with
"duplicated resource within a section of a select specification".

| Parameter | Note |
|---|---|
| `account` | The project to charge |
| `cpus_per_node` | Becomes `ncpus` -- cores reserved per node |
| `select_options` | Appended to the chunk, e.g. `"mpiprocs=4"`. A leading colon is added if absent |
| `scheduler_options` | Extra `#PBS` lines, verbatim |
| `worker_init` | Shell run on the node before the worker starts -- modules, venv, environment script |
| `nodes_per_block` | Nodes per block, i.e. the `select` count |
| `init_blocks`/`min_blocks`/`max_blocks` | All equal pins the topology; no scaling |

### Launchers

| Launcher | Starts the worker as |
|---|---|
| `SingleNodeLauncher()` | One worker on the block's head node. Correct when the block's own MPI work is launched from inside an app |
| `SimpleLauncher()` | One worker, no wrapping |
| `MpiExecLauncher()` | Workers via `mpiexec` across the block's nodes |
| `SrunLauncher()` | Workers via `srun`, for Slurm |

Wrapping the worker in an MPI launcher and then launching MPI inside an app
nests two MPI jobs; pick one level.

---

## Exceptions

From `parsl.app.errors`:

| Exception | Raised when |
|---|---|
| `BashAppNoReturn` | A `@bash_app` body returned `None` instead of a command string |
| `BashExitFailure` | The command exited non-zero. `.exitcode` carries the status |
| `AppTimeout` | A `walltime` was exceeded |
| `MissingOutputs` | A declared `outputs` file does not exist after the app finished. **The honest output check** -- it is about files this app declared, so a stale file from an earlier run cannot satisfy it the way a `test -s` in the command string can |
| `AppBadFormatting` | The command string failed to format |
| `BadStdStreamFile` | `stdout`/`stderr` could not be opened |

`parsl.executors.errors.BadStateException` is raised when an executor cannot
start its blocks at all -- a rejected `qsub` surfaces here, with the scheduler's
stderr quoted.