---
name: knowledge/WILKINS
description: >
  Wilkins configuration and API reference: the full YAML schema as tables (every
  key, required or optional, default, which port kinds accept it, and the dead
  keys), the launch forms, the Python and .hx task contracts, the LowFive
  environment variables, the {filename} convention, flow control (io_freq),
  the pywilkins API surface, and the documented error strings with their
  causes. Load alongside systems/wilkins, which carries the MAW contract and
  the rank layouts this manual serves.
---

# Wilkins — Configuration and API Reference

The companion to `systems/wilkins`. That skill states how to author a
workflow and which mistakes to avoid; this one is the manual you consult while
writing the YAML and the task programs.

Nothing here overrides the contract. If a snippet below appears to permit
something `systems/wilkins` forbids -- a `pywilkins` call inside a task, a
`start_proc` key, a `.yml` filename -- the contract wins.

Everything below was read from the Wilkins source (`src/wilkins/workflow.cpp`,
`src/wilkins/wilkins.cpp`, `src/wilkins/dataflow.cpp`, the driver
`examples/lowfive/wilkins-master.py`, `bindings/python/wilkins/utils.py` and
`bindings/python/py-wilkins.cpp`) and the shipped examples. Behaviour that
was read but not run is marked **(code-read)**. Anything not in the source is
not here; where the source is silent, this manual says so.

---

# The YAML schema

The config is YAML, and its filename must end in `yaml`. The only top-level
key is `tasks`, a list of task mappings. Keys not listed below are not read.

```yaml
tasks:
  - func: "./sim.py"
    nprocs: 4
    ...
```

## Task keys

| Key | Required | Default | Type | Meaning |
|---|---|---|---|---|
| `func` | **yes** | -- | string | What to run. A `.py` path is imported as a module; anything else is a Henson `.hx` shared object. With `taskCount > 1` it is rewritten to `<func>-inst<N>` |
| `nprocs` | **yes** | -- | int | MPI ranks this task gets |
| `taskCount` | no | `1` | int >= 1 | Number of instances of this task (an ensemble) |
| `args` | no | `[]` | list of **strings** | Arguments passed to the task. Quote numbers |
| `actions` | no | none | `[module, function]` | A Python callback applied to the task's LowFive VOL object |
| `nwriters` | no | `-1` (all ranks) | int | Number of leading ranks that take part in LowFive operations |
| `inports` | no | none | list of ports | Datasets this task consumes |
| `outports` | no | none | list of ports | Datasets this task produces |

`func` and `nprocs` are read with no existence check: omitting either throws
inside the YAML reader, which prints `YAML parser exception: ...` and exits 1.
`taskCount < 1` prints `Error: task count cannot be smaller than 1` and exits.

### Dead keys

These appear in shipped example configs and are **not parsed**. They are
silently ignored; never write them.

| Key | Seen in | What actually happens |
|---|---|---|
| `start_proc` | most shipped `.yaml` examples | Ignored. Ranks are assigned by declaration order (see Ranks) |
| `cmdline` | some shipped examples | Ignored. Arguments go in `args` |

`ownership` and `io_proc` are **not** YAML keys. `io_proc` exists only as the
value `is_io_proc()` returns, derived from `nwriters`.

## Port keys

A port is one entry of `inports` or `outports`.

| Key | Required | Default | `inports` | `outports` | Meaning |
|---|---|---|---|---|---|
| `filename` | **yes** | -- | yes | yes | HDF5 file name; a glob (`*`, `?`) is allowed |
| `dsets` | no | none | yes | yes | List of datasets (below). A port with no `dsets` creates no channel **(code-read)** |
| `io_freq` | no | `1` | **yes** | no | Flow control: a positive int, or `latest`. Read only on inports |
| `range` | no | `1, 2, 3, ...` | yes | yes | List of ints naming ensemble file suffixes. Only meaningful with `taskCount > 1` |

An `io_freq` that is neither an int nor `latest` prints `ERROR: <value> -- Not
supported flow control policy` and exits 1.

## Dataset keys

A dataset is one entry of a port's `dsets`.

| Key | Required | Default | `inports` | `outports` | Meaning |
|---|---|---|---|---|---|
| `name` | **yes** | -- | yes | yes | HDF5 dataset path, e.g. `/group1/grid`; a glob is allowed |
| `metadata` | no | **`1`** | yes | yes | `1` = transport in memory over MPI |
| `passthru` | no | **`0`** | yes | yes | `1` = write through to a real HDF5 file |
| `zerocopy` | no | `0` | no | **yes** | `1` = share the producer's buffer instead of deep-copying it |

Values are read as integers. Mode rules:

| `metadata` | `passthru` | Result |
|---|---|---|
| 1 | 0 | In-memory transport. The default and the in situ mode |
| 0 | 1 | File mode: written to a real file |
| 1 | 1 | Parses, but **memory wins**: the driver sets memory mode and passthru is ignored |
| 0 | 0 | **Hard error**: `Error: Either metadata or passthru must be enabled. Both cannot be disabled.` and exit 1 |

## Producer/consumer mode conflicts

When both sides of a matched pair are present, the **producer wins**. The
consumer's setting is rewritten and only a warning goes to stderr:

| Situation | Warning text | Effect |
|---|---|---|
| Consumer `passthru: 1`, producer `passthru: 0` | `Warning: Passthru is not enabled at the producer side, switching to metadata for <name>.` | Consumer becomes in-memory |
| Consumer `metadata: 1`, producer `metadata: 0` | `Warning: Metadata is not enabled at the producer side, switching to passthru for <name>.` | Consumer becomes file mode |

## Unmatched ports

A dataset that appears on only one side has no channel.

| Unmatched port | Result |
|---|---|
| Inport with `metadata: 1` | **Hard error**: `ERROR: No matching link found for the inport <file><dset> requesting memory mode. Please use the file mode or specify a matching outport.` Exit 1 |
| Outport with `metadata: 1` | **Hard error**: the same text with `outport` and `matching inport` |
| Inport or outport with `metadata: 0, passthru: 1` | **Not an error.** The dataset is registered for plain file I/O: the task simply reads or writes a file, with no data coming from another task |

Passthru without a partner is the one way a dangling inport does not fail
loudly, so do not use it to test that a match exists.

---

# Port matching

You never declare edges. Wilkins forms a key from each port as
`filename + "/" + dsetname` (so `outfile.h5` and `/group1/grid` give
`outfile.h5//group1/grid`) and matches keys with a glob matcher.

| Rule | Detail |
|---|---|
| Wildcards | `*` (any run, including empty) and `?` (any one character). Not regexes |
| Filename vs dataset | Matched together as one string, not separately |
| Direction | A key with producers and consumers becomes a link; a key with only one side is "unmatched" (above) |
| Many-to-many | If several producers or consumers share a key, links are generated round-robin by instance count **(code-read)** |
| Order | A new key is merged into an earlier key only if the earlier key, read as a pattern, matches it. A pattern key must therefore be declared in an earlier task than a literal it should cover **(code-read)**. Using identical strings on both sides avoids the issue |

Identical strings on both sides always match (`outfile.h5` with
`/group1/grid`; `*.h5` with `*`; and so on).

---

# Ranks

| Fact | Detail |
|---|---|
| `nprocs` | That task's MPI rank count |
| Assignment | Contiguous blocks in YAML declaration order. The first task owns ranks `0 .. n0-1`; each next task starts where the previous ended. **Reordering tasks moves them** |
| Total | The launcher's `-n` must be the sum over tasks of `taskCount x nprocs` |
| Too few | Tail tasks get no ranks. `Wilkins error: Group sizes of producer, consumer, and dataflow exceed total size of world communicator` is printed |
| Too many | Extra ranks idle |
| Communicator | A task's HDF5 and LowFive work use its **task-local** communicator, not `COMM_WORLD` |
| `nwriters: K` | Only the first K ranks of the task take part in LowFive operations; the driver reads this through `is_io_proc()`. `-1` means all ranks |
| Placement | **There are no node-placement controls in the config.** Which node a rank runs on is the launcher's job |

---

# Ensembles: `taskCount`

`taskCount: N` creates N instances of a task, each a separate set of `nprocs`
ranks.

| Effect | Detail |
|---|---|
| Function names | Instance `m` (from 0) has `func` rewritten to `<func>-inst<m>` |
| Filenames | Port `filename` is split at its **FIRST dot**; instance `m` gets `<before>-inst<range[m]><after>`. `range` defaults to `1, 2, 3, ...` (so file suffixes start at 1 while function suffixes start at 0) **(code-read)** |
| Dotless filename | `substr` throws an uncaught `std::out_of_range`; the config load crashes rather than printing a YAML error **(code-read)** |
| Executables | The driver switches to Henson mode for that task and every LATER task, naming each `./<func>.hx`. So an ensemble is a compiled-task feature; a `.py` `func` declared after any `taskCount > 1` task is also looked up as `./<func>.hx` **(code-read)** |
| Rank total | `taskCount x nprocs` per task |

The shipped helper `examples/lowfive/ensembles/generateRunScript.sh` copies
`<func>.hx` to `<func>_<j>.hx`, but the source builds the name with `-inst`.
Which spelling the Henson loader resolves is **unverified**; do not rely on
ensembles without a known-good build.

`range` is a list of ints on a port and sets the suffixes in the filenames
above. It is reset to `1, 2, 3, ...` at the start of each task.

---

# Launch forms

## Driver form (what MAW uses)

```
mpirun -n <total> -l python -u wilkins-master.py config.yaml [driver flags]
```

| Piece | Meaning |
|---|---|
| `-n <total>` | Sum over tasks of `taskCount x nprocs` |
| `-l` | Label each line of output with its rank |
| `python -u` | Unbuffered, so a hung run still shows output |
| `wilkins-master.py` | The driver. `spack load wilkins` places it on `PATH`; the shipped examples keep a copy beside the config |
| `config.yaml` | The workflow. Must end in `yaml`; the driver rejects other names with `ERROR: Not supported configuration file format. Please provide the graph definition in YAML.` |

### Driver flags

They come **after** the config path (the driver removes `argv[1]` before it
parses the rest).

| Flag | Values | Default | Meaning |
|---|---|---|---|
| `-p`, `--passthruSupport` | `0`, `1` | `0` | Passthru support level: `0` none, `1` single iteration |
| `-v`, `--verbosity` | `0`, `1`, `2` | `0` | LowFive log level: none, info, debug |

With `-p 1` the driver calls `wilkins.wait()` before each task body and
`wilkins.commit()` after it, and the `{filename}` callback is not installed.

## MPMD form

Each task is launched directly and builds the Wilkins object itself:

```
mpirun -np 3 -l python ./prod.py : -np 1 python ./con.py
```

Each task constructs `pywilkins.Wilkins(comm_address, "config.yaml")` and
makes the LowFive calls itself. This form is not what MAW generates; it is
here so you recognize the `pywilkins` API.

---

# Task contracts

Dispatch is by the suffix of `func`: a name ending in `.py` is a Python task,
anything else is a Henson task. With `taskCount > 1`, see Ensembles.

## Python task (`func: ./name.py`)

The driver resolves the path with `os.path.abspath`, adds its directory to
`sys.path`, and **imports it as a module**. A missing file raises
`FileNotFoundError: Script not found: <func>`.

| Contract | Detail |
|---|---|
| `main` | The module must define a callable `main`. With none, the import succeeds and the task **silently does nothing** |
| Call | `main(task_args)` where `task_args` is the `args` list of strings. On `TypeError` the driver calls `main()` instead. Declare `def main(task_args=None)` to accept both |
| Re-entry | A consumer's `main` is called once, then **again every time more data is available**, until the producers finish. It must be re-entrant. A pure producer's `main` is called once |
| I/O | Plain HDF5 or plain `h5py`. **No Wilkins and no LowFive calls** |
| Files | Open and close with the filename the YAML declares; LowFive intercepts the calls |
| Communicator | The task's local communicator is not passed to `main`. Do not run collectives over `COMM_WORLD` |
| Build | None. No Henson link is required |

```python
import h5py

def main(task_args=None):
    with h5py.File("outfile.h5", "r") as f:
        data = f["/group1/grid"][:]
    print(data.sum())
```

## `.hx` task (`func: ./name.hx`)

Any non-`.py` `func` is passed to Henson as a puppet with the `args` list. It
must be a shared object built for Henson:

| Requirement | Detail |
|---|---|
| Linked against Henson | Henson is Wilkins' execution model |
| Position-independent | `-fPIC` / `CMAKE_POSITION_INDEPENDENT_CODE ON` |
| Shared object named `./<name>.hx` | `add_library(<name> SHARED ...)` with `PREFIX ""` and `SUFFIX ".hx"` |
| Linker flags (Linux) | `-pie -Wl,--export-dynamic` and `-Wl,-u,henson_set_contexts,-u,henson_set_namemap` |
| Linker flags (macOS) | `-Wl,-u,_henson_set_contexts,-u,_henson_set_namemap` |

A plain MPI binary **cannot** be a `wilkins-master.py` task. This is the rule
for non-`.py` tasks only; Python tasks are imported and need none of it.

```cmake
add_library(sim SHARED sim.cpp)
set_target_properties(sim PROPERTIES PREFIX "" SUFFIX ".hx")
set_target_properties(sim PROPERTIES LINK_FLAGS
  "-pie -Wl,--export-dynamic -Wl,-u,henson_set_contexts,-u,henson_set_namemap")
```

Compiled tasks use plain HDF5 too; the shipped ones set
`H5Pset_fapl_mpio(plist, local_comm, MPI_INFO_NULL)` on their task-local
communicator.

## `actions`

`actions: [module, function]` names a Python callback the driver applies on the
ranks of that task that take part in LowFive.

| Detail | Behaviour |
|---|---|
| Module lookup | Found with `importlib` on `sys.path`. A missing module prints `<module>: No such file exists for the callback actions.` and exits 1 |
| Call | `function(vol, local_rank, pl_con)`; on `TypeError` it falls back to `function(vol, local_rank)` |
| `vol` | The LowFive `DistMetadataVOL` object |
| Shipped helpers | `wilkins.utils.setup_passthru_callbacks(vol, "producer" or "consumer", pl_con)` |

Methods called on `vol` by shipped callbacks: `set_after_dataset_write`,
`serve_all(bool, bool)`, `serve_on_close` (attribute), `set_send_filename`,
`set_keep`, `set_before_file_open`, `get_filenames`, `send_done`. This is only
the set the shipped examples use; the full LowFive API is in the LowFive
repository, not here.

---

# LowFive: the environment

LowFive is enabled by two environment variables, not by the config.

| Variable | Value | If wrong |
|---|---|---|
| `HDF5_VOL_CONNECTOR` | `"lowfive under_vol=0;under_info={};"` | **Unset: nothing checks it.** The run completes on HDF5's native VOL, every task writes a real `.h5`, nothing is transported in memory |
| `HDF5_PLUGIN_PATH` | the directory holding `liblowfive.*` | Unset: the driver raises `KeyError`. No `liblowfive.*` there: `RuntimeError: Bad HDF5_PLUGIN_PATH, lowfive library not found` |

Quote the connector value in a shell: the `;` ends the command otherwise. The
Wilkins README gives it unquoted, which is broken shell.

```
export HDF5_VOL_CONNECTOR="lowfive under_vol=0;under_info={};"
export HDF5_PLUGIN_PATH=/path/to/dir/with/liblowfive
```

`spack load wilkins` sets both. A manual install must set them by hand (LowFive
ships `vol-plugin.sh` that does).

The unset-connector case is the quietest failure Wilkins has: no error, a
successful exit, and `.h5` files on disk that should not exist. Verify before
every run (MAW: `check_lowfive`).

---

# Dynamic consumer filenames: `{filename}`

When a producer writes a different file name each round (`out_0.h5`,
`out_1.h5`, ...), a stateful consumer cannot know the name in advance.

| Detail | Behaviour |
|---|---|
| Trigger | Any element of the consumer's `args` that contains the text `{filename}` |
| Effect | The driver installs a LowFive consumer-filename callback. The name the consumer opens is replaced by the next file name the producer served |
| The argument itself | Passed to the task unchanged. The driver only checks that it is present; it does not substitute the text **(code-read)** |
| Skipped when | `-p 1` is in use |
| Port filename | Use a glob (`"*.h5"`) on both the outport and the inport |

```yaml
  - func: "./ana.hx"
    nprocs: 1
    args: ["{filename}"]
    inports:
      - filename: "*.h5"
        dsets: [{name: /group1/grid}]
```

---

# Flow control: `io_freq`

`io_freq` is set on an **inport**, but the producer enforces it when deciding
what to serve.

| Value | Meaning |
|---|---|
| `1` (default) | The consumer is served every round |
| `N` > 1 | Served every Nth round of the producer |
| `latest` | Served only when the consumer has signalled that it is waiting; rounds in between are skipped so the consumer sees the newest data |
| anything else | `ERROR: <value> -- Not supported flow control policy`, exit 1 |

`0` is read as an int and then used as a modulus by the driver
(`serve_counter % interval`), so it fails at run time. Do not write it.

Without flow control a consumer slower than the producer holds the producer
back. Flow control applies per producer/consumer pair; a producer with two
consumers can give each its own `io_freq`.

---

# `pywilkins` API

`pywilkins` is a pybind11 module (`from wilkins import pywilkins`, or
`import pywilkins` in the MPMD examples). The driver `wilkins-master.py` uses
it itself. **Task code for the driver form must not call it.** It is for the
MPMD form. Communicators cross the boundary as `mpi4py` objects.

## Module functions

| Function | Returns |
|---|---|
| `get_local_comm(wilkins)` | The task-local `mpi4py` communicator |
| `get_intercomms(wilkins)` | List of `mpi4py` intercommunicators for the task. A **collective** call: both sides of each pair must call it |

## `Wilkins(comm_address, config_file)`

`comm_address` is `MPI._addressof(MPI.COMM_WORLD)` (an `int`); `config_file`
must end in `yaml`.

| Method | Meaning |
|---|---|
| `local_comm_size()` | Size of this task |
| `local_comm_rank()` | Rank within this task |
| `workflow_comm_size()` | Size of the whole workflow |
| `workflow_comm_rank()` | Rank within the whole workflow |
| `is_io_proc()` | `1` if this rank performs LowFive operations (`0` for ranks beyond `nwriters`) |
| `my_node(name)` | Whether this rank belongs to the task whose `func` equals `name` |
| `set_lowfive()` | List of `LowFiveProperty`, one per dataset on this task's links |
| `wait()` | Consumer blocks until data is ready (pairs with the producer's `commit`) |
| `commit()` | Producer signals that data is ready |

```python
import pywilkins as w
from mpi4py import MPI

wilkins = w.Wilkins(MPI._addressof(MPI.COMM_WORLD), "wilkins.yaml")
comm = w.get_local_comm(wilkins)
intercomms = w.get_intercomms(wilkins)
wilkins.commit()      # producer;  a consumer calls wilkins.wait()
```

## `Workflow` and `WorkflowNode`

| Class | Members |
|---|---|
| `Workflow()` | `nodes` (list of `WorkflowNode`), `make_wflow_from_yaml(path)` |
| `WorkflowNode()` | `start_proc`, `nprocs`, `taskCount`, `func`, `args`, `actions`, `passthru_files` (all read/write) |

`start_proc` here is computed from declaration order, not read from YAML.

## `LowFiveProperty`

Read/write fields: `filename`, `dset`, `execGroup`, `zerocopy`, `memory`,
`producer`, `consumer`, `prodIndex`, `conIndex`, `flowPolicy`. The driver uses
them to configure the VOL: `memory == 1` selects `set_memory`, otherwise
`set_passthru`.

---

# Error and message reference

All messages go to stderr. "Exit" means the process exits non-zero.

| Message | Cause | Fix |
|---|---|---|
| `YAML parser exception: <detail>` | Malformed YAML, or a required key missing (`func`, `nprocs`, `filename`, a dataset `name`), or a wrong type (a string where an int goes) | Fix the key; check indentation and types |
| `Error: task count cannot be smaller than 1` | `taskCount` below 1 | Use 1 or more |
| `Error: Either metadata or passthru must be enabled. Both cannot be disabled.` | A dataset with `metadata: 0` and `passthru: 0` | Set one to 1 |
| `ERROR: No matching link found for the inport <file><dset> requesting memory mode...` | An inport with `metadata: 1` that no outport matches | Make filename and dataset equal on both sides |
| `ERROR: No matching link found for the outport <file><dset> requesting memory mode...` | An outport with `metadata: 1` that no inport matches | Add the consumer, or drop the port |
| `ERROR: <value> -- Not supported flow control policy` | `io_freq` is not an int or `latest` | Use an int or `latest` |
| `Warning: Passthru is not enabled at the producer side, switching to metadata for <name>.` | Consumer asked for passthru, producer did not | Match the modes (no exit) |
| `Warning: Metadata is not enabled at the producer side, switching to passthru for <name>.` | Consumer asked for memory, producer did not | Match the modes (no exit) |
| `ERROR: Not supported configuration file format. Please provide the graph definition in YAML.` | Config name does not end in `yaml` (`.yml` fails) | Rename to `*.yaml` |
| `ERROR: No name filename provided for the YAML file...` | Empty config path | Pass the config path |
| `Wilkins error: Group sizes of producer, consumer, and dataflow exceed total size of world communicator` | Launched with fewer ranks than the tasks need | Launch with the full sum |
| `KeyError: 'HDF5_PLUGIN_PATH'` | Variable unset | Set it (`spack load wilkins`) |
| `RuntimeError: Bad HDF5_PLUGIN_PATH, lowfive library not found` | No `liblowfive.*` on the path | Point it at the LowFive build/install directory |
| `FileNotFoundError: Script not found: <func>` | A `.py` `func` path does not exist | Fix the path (relative to the launch directory) |
| `<module>: No such file exists for the callback actions.` | `actions` names a module that cannot be found | Put the module on `sys.path` |
| `ValueError: No matching consumer found for filename '<name>'. Available patterns: [...]` | A passthru consumer opened a file that matches no pattern in its consumer list | Match the consumer's filename to its inport |

## Silent outcomes (no message)

| Outcome | Cause |
|---|---|
| Run succeeds, real `.h5` files on disk, nothing transported | `HDF5_VOL_CONNECTOR` unset or not LowFive |
| A `.py` task does nothing | The module defines no `main` |
| A consumer reads nothing | An inport with no matching outport and `passthru: 1` |
| Tail tasks never start | Fewer ranks than the sum of `taskCount x nprocs` |
| `start_proc` or `cmdline` has no effect | They are dead keys |
| Consumer mode differs from what it asked | Producer-wins coercion; only a warning is printed |
