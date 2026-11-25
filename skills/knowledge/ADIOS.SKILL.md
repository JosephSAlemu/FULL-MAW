---
name: knowledge/ADIOS
description: >
  ADIOS2 Python API reference: the Stream high-level API, the Adios/IO/Engine/
  Variable classes, FileReader, SST vs BP, and shape/start/count decomposition.
  Load alongside systems/adios, which carries the MAW execution contract.
---

# ADIOS2 — Python API Reference

The API manual for `systems/adios`. That skill is the contract and wins on any
conflict: which tools, one file per stage, the two variants, rank layouts and
pitfalls are all there, not here.

Signatures were taken from the ADIOS2 Python documentation for the 2.10+
high-level API (`adios2.Stream`), not from an installed copy. The repo pins
`adios2==2.12.1.1001` for the serial wheel. `check_package("adios2")` reports
what is actually installed; when a signature here disagrees with it, the
installed one wins.

`adios2.open()` is gone; `adios2.Stream` replaces it. Iterating a file with
`for x in f` is now `for _ in f.steps()`, for both reading and writing.

# Serial vs MPI build

The serial PyPI wheel raises
`RuntimeError: Cannot use MPI since ADIOS2 was built without MPI support`
on any `comm=` argument or `Adios(comm)`. Every `comm` parameter below is
MPI-build-only; omit it on the serial build. Which build you have is decided in
`systems/adios`.

# Stream (high-level API)

```python
import adios2
from adios2 import Stream, FileReader, Adios
```

## Constructors

```python
adios2.Stream(path, mode, comm=None)
adios2.Stream(io, path, mode, comm=None)   # io from Adios.declare_io
```

| `mode` | Meaning |
|---|---|
| `"w"` | write |
| `"r"` | read, step by step (streaming order) |
| `"rra"` | ReadRandomAccess: any step of any variable at will |

`Stream(path, mode)` uses the default engine (BP file). To pick the engine
(SST), build an `IO`, call `io.set_engine(...)`, and use the `io` form. Use
`with` so the stream closes:

```python
with Stream("out.bp", "w") as s:
    ...
```

Properties: `s.adios` (the `Adios`), `s.io` (the `IO`), `s.engine` (the
`Engine`), `s.mode`.

## Writing

```python
s.write(name, content, shape=[], start=[], count=[], operations=None)
s.write(variable, content)       # variable: adios2.Variable
s.write_attribute(name, content, variable_name="", separator="/")
s.define_variable(name)          # string variables only
```

- A Python scalar (int, float, str) needs no shape/start/count.
- `shape` is the global array, `start` and `count` this writer's slice; see
  "Decomposition rules".
- Written data is published at `end_step()` or the end of the `steps()` loop
  iteration, not at the `write` call.
- `variable_name` ties an attribute to a variable: the attribute is stored as
  `variable_name + separator + name`, e.g. `"temperature/unit"`.
- `operations` attaches compression operators; not covered here.

## Reading

```python
s.read(name, start=[], count=[], block_id=None, step_selection=None,
       defer_read=False)
s.read(variable, start=[], count=[], block_id=None, step_selection=None,
       defer_read=False)
s.read_in_buffer(name, buffer, start=[], count=[], block_id=None,
                 step_selection=None, defer_read=False)
s.read_complete()
s.read_attribute(name, variable_name="", separator="/")
s.read_attribute_string(name, variable_name="", separator="/")
```

- Returns a numpy array. Scalars come back as 0-d numpy arrays; attributes
  written as strings come back as `str`.
- `start` / `count` select a slice of the global array.
- `block_id` (int) is required to read local (non-global) variables.
- `step_selection=[first_step, n_steps]` is for `"rra"` mode; the result gains
  the selected steps stacked in the result (scalars give shape `(n,)`; the
  array in the example above came back with shape `(200,)` for 5 steps of 40).
- `defer_read=True` returns an array that is filled only after
  `read_complete()`.
- `read_in_buffer` fills a preallocated buffer instead of returning an array.

## Steps

```python
s.steps(num_steps=0, *, timeout=-1.0)   # iterator
s.begin_step(*, timeout=-1.0)
s.end_step()
s.current_step()      # step number, from 0; may skip steps in a lossy stream
s.loop_index()        # consecutive loop counter from 0 inside steps()
s.num_steps()         # read mode only: steps in the file/stream
s.step_status()       # adios2.bindings.StepStatus
```

- `steps()` calls `begin_step` / `end_step` for you each iteration.
- Write mode: `num_steps` is mandatory and fixes the iteration count.
- Read mode: with no `num_steps`, iterates as many steps as the engine
  provides (a live stream ends when the writer closes). If `num_steps` exceeds
  what exists, an error is raised.
- Do not use `steps()` in `"rra"` mode.
- `begin_step` / `end_step` in read mode release the current step on streaming
  engines and do nothing on file engines.
- `num_steps()` counts steps of the file; each variable has its own count,
  `s.inquire_variable(n).steps()`.

## Inspection

```python
s.available_variables()                       # {name: {info key: value}}
s.available_attributes(varname="", separator="/")
s.inquire_variable(name)                      # Variable or None
s.inquire_attribute(name, variable_name="", separator="/")  # or None
s.all_blocks_info(name)                       # per-step block info; costly
s.minmax(name, step=None, block_info_list=[]) # (min, max)
s.set_parameters(**kwargs)                    # replaces earlier parameters
s.get_metadata()                              # serialized metadata (bytes)
s.close()
```

- `available_variables()` info values are strings with the keys
  `AvailableStepsCount`, `Type`, `Max`, `Min`, `SingleValue`, `Shape`; convert
  with `int(info["AvailableStepsCount"])`. Read mode only, as is
  `available_attributes`.
- `all_blocks_info` memory grows with steps times blocks; avoid on large data.
- `minmax` in `"r"` mode is the current step's (passing `step` is an error);
  in `"rra"` mode it is the global min/max, or that of `step` if given.
- `close()` is unnecessary inside `with`, required otherwise.

## Examples

Write (MPI build; drop `comm` on serial and use `[0]`/`data.shape`):

```python
from mpi4py import MPI
import numpy as np
from adios2 import Stream

comm = MPI.COMM_WORLD
rank, size = comm.Get_rank(), comm.Get_size()
nx = 10
data = np.zeros(nx)

with Stream("cfd.bp", "w", comm) as s:
    for _ in s.steps(5):
        if rank == 0 and s.current_step() == 0:
            s.write("nproc", size)
        s.write("temperature", data, [size * nx], [rank * nx], [nx])
        s.write_attribute("temperature/unit", "K")
```

Step-by-step read:

```python
with Stream("cfd.bp", "r") as s:
    for _ in s.steps():
        t = s.read("temperature")
        unit = s.read_attribute("temperature/unit")
```

# Adios and IO

```python
adios2.Adios(config_file=None, comm=None)
ad.declare_io(name)       # IO; declaring the same name twice raises
ad.at_io(name)            # previously declared IO
ad.flush_all()
ad.remove_io(name)
ad.remove_all_ios()
```

`config_file` loads an XML runtime configuration (one-line mention; not
needed for MAW). Operators exist (`define_operator`, `inquire_operator`) and
are not covered.

## IO

```python
io.set_engine(name)                       # e.g. "SST"
io.set_parameters(parameters)             # dict; replaces earlier ones
io.set_parameter(key, value)              # both strings
io.parameters()                           # dict str -> str
io.add_transport(kind, parameters={})     # returns a transport index
io.open(name, mode, comm=None)            # returns an Engine
io.define_variable(name, content=None, shape=[], start=[], count=[],
                   is_constant_dims=False)
io.inquire_variable(name)                 # Variable or None
io.define_attribute(name, content=None, variable_name="", separator="/")
io.inquire_attribute(name, variable_name="", separator="/")
io.available_variables()                  # read mode only
io.available_attributes(varname="", separator="/")
io.engine_type()
io.remove_variable(name)
io.remove_attribute(name)
io.remove_all_variables()
io.remove_all_attributes()
io.flush_all()
io.adios()
```

- `io.open` takes an `adios2.Mode` value (`Mode.Write`, `Mode.Read`,
  `Mode.ReadRandomAccess`) in `mode`.
- `define_variable` is called once per variable, outside the step loop; a
  second definition of the same name raises "already defined". `shape`,
  `start`, `count` follow "Decomposition rules"; `content` may be a numpy array
  used to infer the dtype.
- `add_transport` cannot take the key `Transport` or `transport` in
  `parameters`, and `kind` must be supported by the engine.
- `available_variables(keys=...)` can restrict the info keys returned
  (case-insensitive), e.g. `["Name"]`.

Lower-level write:

```python
ad = adios2.Adios()
io = ad.declare_io("w")
var = io.define_variable("U", data, [N], [0], [N])
e = io.open("out.bp", adios2.Mode.Write)
for step in range(nsteps):
    e.begin_step()
    e.put(var, data)
    e.end_step()
e.close()
```

# Engine

Returned by `io.open(...)`.

```python
e.begin_step(*args, **kwargs)
e.end_step()
e.put(variable, content, mode=Mode.Deferred)
e.get(variable, content=None, mode=Mode.Sync)
e.perform_puts()
e.perform_gets()
e.close(transport_index=-1)
e.current_step()
e.steps()                    # number of available steps
e.flush(transport_index=-1)
e.get_metadata()
e.blocks_info(name, step)
e.all_blocks_info(name)
```

- A deferred `put` copies or reads the buffer later: do not modify or free the
  array before `end_step()` or `perform_puts()`. `perform_puts()` flushes
  pending puts inside a step.
- `get` with `Mode.Deferred` fills `content` only after `perform_gets()` or
  `end_step()`. With `content` omitted, `get` returns the data (sync).
- `begin_step` / `end_step` must be balanced.

# Variable

Obtained from `define_variable` or `inquire_variable`.

```python
v.set_selection([[start...], [count...]])   # slice to read or write
v.set_shape(shape)                          # change global shape
v.set_step_selection([first_step, n_steps]) # rra mode
v.set_block_selection(block_id)
v.shape(step=None)    # lowercase method; step only in rra mode
v.start()
v.count()
v.steps()             # 1 in a stream; total steps in rra mode
v.steps_start()
v.name()
v.type()              # string, e.g. "double"
v.single_value()
v.block_id()
v.selection_size()
v.sizeof()
```

- `set_selection` takes a list of two lists: `[[start], [count]]`, each with
  one entry per dimension.
- `shape`, `start`, `count`, `steps` are methods, not attributes.

## Attribute (from `inquire_attribute`)

`a.data()`, `a.data_string()`, `a.name()`, `a.type()`, `a.single_value()`.

# FileReader

```python
adios2.FileReader(path, comm=None)
```

Equivalent to `Stream(path, "rra")`. No step loop; reads any step directly. It
takes the same read/inspection methods as `Stream`.

```python
with FileReader("cfd.bp") as r:
    vars = r.available_variables()
    n = int(vars["temperature"]["AvailableStepsCount"])
    t = r.read("temperature", step_selection=[0, n])   # shape (n, ...)
    unit = r.read_attribute("temperature/unit")
```

# Engines and formats

## BP

A `.bp` is a BP file or directory written by the default engine. A reader may
open it only reliably after the writer closed it. A reader that opens a `.bp`
the writer has not closed silently reads whatever is there: no error, wrong
data. Use BP for input data, for results written once, and for random access.

## SST

SST is a live producer/consumer stream, selected by name:

```python
ad = adios2.Adios()
io = ad.declare_io("name")
io.set_engine("SST")
s = adios2.Stream(io, "stream_name", "w")   # reader: "r"
```

- Writer and reader must both select SST and use the identical stream name.
- The two sides rendezvous at open, so both programs must be running at the
  same time. A reader that starts first waits in the rendezvous until its
  timeout.
- On disk, `<name>.sst` is a DIRECTORY, not a file. It holds `data.N`,
  `md.N`, `md.idx`, `mmd.0` and `profiling.json`, and is left behind after a
  run. A stale one can be read by a later reader as if it had been streamed.
- On the serial wheel, SST data moves over TCP.
- Read with `for _ in s.steps():`; the loop ends when the writer closes.
- Engine parameters go through `io.set_parameters({...})` or
  `io.set_parameter(key, value)` before opening, with string values. Names
  like `RendezvousReaderCount`, `QueueLimit`, `QueueFullPolicy`,
  `DataTransport` and `OpenTimeoutSecs` exist in ADIOS2's SST documentation
  but were not in the source this manual was written from, so their
  semantics and defaults are NOT confirmed here; check the installed ADIOS2
  documentation before relying on one. The defaults give a working
  rendezvous for one writer and its readers.

# Decomposition rules

For a global array written by several processes (MPI build) or by one:

| Argument | Meaning |
|---|---|
| `shape` | global dimensions, identical on every writer |
| `start` | this writer's offset in each dimension |
| `count` | this writer's local extent in each dimension |

- `shape`, `start` and `count` are lists of the same length, one entry per
  dimension. `start[i] + count[i] <= shape[i]`.
- On a single writer: `shape = list(a.shape)`, `start = [0]*a.ndim`,
  `count = list(a.shape)`.
- The local numpy array must have `a.shape == tuple(count)`.
- Writers must tile the global array without overlap. 1-D example:
  `shape=[size*nx]`, `start=[rank*nx]`, `count=[nx]`.
- A reader may use a different decomposition: `s.read(name, start, count)`
  returns its own slice of the global array.
- Write scalars as a Python int/float with no shape/start/count; they read
  back as 0-d numpy arrays.
- The dtype is taken from the numpy array. Keep it explicit and identical
  across steps (`np.float64`, `np.float32`, `np.int64`); a Python float is
  written as double and a Python int as int64. Convert with `np.asarray(x,
  dtype=...)` first.
- `Variable.set_shape` changes the global shape of a defined variable.
- `s.read(name)` with no selection returns the full global array of the
  current step.
