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

Nothing here overrides the contract: if a snippet seems to permit something
`systems/parsl` forbids, the contract wins.

Signatures below were read from **parsl 2026.10.19**. Defaults and parameter
names change between releases. `check_package("parsl")` reports what is
actually installed; when a signature here disagrees with that version, the
installed one wins.

---

# API

## Runtime entry points

### `parsl.load(config: Optional[Config] = None) -> DataFlowKernel`
Starts the DataFlowKernel and the executors the config declares. Must be called
before any app is invoked. Pass an explicit `Config`.

### `parsl.clear() -> None`
Shuts the DataFlowKernel down and releases the executors. Call it in a `finally`.

### `parsl.dfk() -> DataFlowKernel`
The currently loaded DataFlowKernel. Rarely needed in a generated driver.

### `parsl.wait_for_current_tasks() -> None`
Blocks until every submitted task has finished. An alternative to resolving the
final future when the driver has no single terminal result.

### `parsl.AUTO_LOGNAME`
Sentinel (`-1`) for `stdout`/`stderr`. Parsl picks a per-task filename under its
`run_dir`, in `task_logs/`, so two tasks cannot collide.

---

## App decorators

### `@python_app(function=None, data_flow_kernel=None, cache=False, executors='all', ignore_for_cache=None)`
Runs the decorated function on a worker (scoping rules: `systems/parsl`).
Returns an `AppFuture` immediately.

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
| `walltime` | seconds | Per-app timeout; exceeding it raises `AppTimeout`. Value rule is in `systems/parsl` |

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

## Files

### `parsl.data_provider.files.File(url: os.PathLike | str)`
A location-independent handle on a file.

| Attribute | For `File("/tmp/x/y.txt")` |
|---|---|
| `.filepath` | `/tmp/x/y.txt` |
| `.filename` | `y.txt` |
| `.scheme` | `file` |

A bare path gets scheme `file`.

---

## Configuration

### `Config(...)`
The keys a generated driver sets; the skeleton itself is in `systems/parsl`.

| Parameter | Default | Meaning |
|---|---|---|
| `executors` | `None` | The executors to start. Required in practice |
| `run_dir` | `'runinfo'` | Where Parsl writes block logs and `task_logs/`; relative to the cwd unless absolute |
| `retries` | `0` | Automatic retries per task |
| `strategy` | `'simple'` | Block scaling; `'none'` pins the block count |
| `initialize_logging` | `True` | Whether Parsl configures logging itself |

### `HighThroughputExecutor(...)`

| Parameter | Default | Meaning |
|---|---|---|
| `label` | `'HighThroughputExecutor'` | Name used by `executors=[...]` on an app |
| `provider` | `None` | Where blocks come from |
| `max_workers_per_node` | `None` | Hard cap on workers per node: the concurrency knob |
| `cores_per_worker` | `1.0` | Divisor for the worker count, not a reservation (see `systems/parsl`) |
| `cpu_affinity` | `'none'` | `'block'` or `'alternating'` pins workers to cores |

### `LocalProvider(nodes_per_block=1, launcher=SingleNodeLauncher(), init_blocks=1, min_blocks=0, max_blocks=1, worker_init='', cmd_timeout=30, parallelism=1)`
Runs blocks on the current machine without submitting anything. `worker_init`
is shell run before each worker starts.

### Launchers

| Launcher | Starts the worker as |
|---|---|
| `SingleNodeLauncher()` | One worker pool on the block's head node |
| `SimpleLauncher()` | One worker, no wrapping |

---

## Exceptions

From `parsl.app.errors`:

| Exception | Raised when |
|---|---|
| `BashAppNoReturn` | A `@bash_app` body returned `None` instead of a command string |
| `BashExitFailure` | The command exited non-zero. `.exitcode` carries the status |
| `AppTimeout` | A `walltime` was exceeded |
| `MissingOutputs` | A declared `outputs` file does not exist after the app finished. A stale file from an earlier run cannot satisfy it the way a `test -s` can |
