---
name: knowledge/ADIOS
description: >
  ADIOS2 Python API reference: the Stream high-level API, the
  Adios/IO/Variable/Engine/Attribute classes, read modes, and the old-to-new API
  transition. Load alongside systems/adios, which carries the MAW execution
  contract (write_file/run_workflow/run_mpmd, staged pipelines, MPMD) that
  this manual serves.
---

# ADIOS2 — Python API Reference

The companion to `systems/adios`. That skill states the rules every generated
workflow must obey; this one is the API manual you consult while writing it.

Nothing here overrides the contract. If a snippet below appears to permit
something `systems/adios` forbids -- `subprocess` at module top level, a stage
that never reads its data back -- the contract wins. Note that `write_file`
no longer refuses these for you: it rejects only a syntax error and reports the
rest as warnings, so a pipeline that never touches ADIOS2 will be written and
will run. The contract still binds.

## The calls MPMD depends on

`systems/adios` explains when to use `MPMD_LAYOUT` (separate programs, one rank
block each) versus `MPI_RANKS` (one program, roles by rank). Both rest on the
lower-level class API rather than the `Stream` convenience wrapper, because
only it accepts a communicator and lets you select the SST engine:

| Need | Call | Reference section below |
|---|---|---|
| Per-block ADIOS instance | `adios2.Adios(comm)` | ADIOS class |
| Named IO channel | `ad.declare_io("name")` | IO class |
| Stream instead of file | `io.set_engine("SST")` | IO class |
| Open with a communicator | `io.open(name, Mode.Write, comm)` | Engine class |
| Declare the array once | `io.define_variable(...)` | Variable class |
| Per-step publish / consume | `e.begin_step()` / `put` / `get` / `e.end_step()` | Engine class |
| Reader slice selection | `v.set_selection([[start], [count]])` | Variable class |
| Global shape, at read time | `v.shape()` -- lowercase | Variable class |

`Stream(path, "w"/"r")` is the right choice for a **sequential** BP pipeline and
is what the validator looks for there; it does not take a comm, so it is not the
MPMD path.

---

# Python APIs

## Python Example Code

The Python APIs follows closely Python style directives. They rely on numpy and, optionally, on mpi4py, if the underlying ADIOS2 library is compiled with MPI.

For online examples on MyBinder :

- Python-MPI Notebooks

- Python-noMPI Notebooks

## Examples in the ADIOS2 repository

Simple file-based examples
- `examples/hello/helloWorld/hello-world.py`

- `examples/hello/bpReader/bpReaderHeatMap2D.py`

- `examples/hello/bpWriter/bpWriter.py`

Staging examples using staging engines SST and DataMan
- `examples/hello/sstWriter/sstWriter.py`

- `examples/hello/sstReader/sstReader.py`

- `examples/hello/datamanWriter/dataManWriter.py`

- `examples/hello/datamanReader/dataManReader.py`

## Python Write example

```python
from mpi4py import MPI
import numpy as np
from adios2 import Stream

comm = MPI.COMM_WORLD
rank = comm.Get_rank()
size = comm.Get_size()

nx = 10
shape = [size * nx]
start = [rank * nx]
count = [nx]

temperature = np.zeros(nx, dtype=np.double)
pressure = np.ones(nx, dtype=np.double)
delta_time = 0.01
physical_time = 0.0
nsteps = 5

with Stream("cfd.bp", "w", comm) as s:
   # NSteps from application
   for _ in s.steps(nsteps):
      if rank == 0 and s.current_step() == 0:
         # write a Python integer
         s.write("nproc", size)

      # write a Python floating point value
      s.write("physical_time", physical_time)
      # temperature and pressure are numpy arrays
      s.write("temperature", temperature, shape, start, count)
      s.write_attribute("temperature/unit", "K")
      s.write("pressure", pressure, shape, start, count)
      s.write_attribute("pressure/unit", "Pa")
      physical_time += delta_time
```

```text
$ mpirun -n 4 python3 ./adios2-doc-write.py
$ bpls -la cfd.bp
  int64_t  nproc             scalar = 4
  double   physical_time     5*scalar = 0 / 0.04
  double   pressure          5*{40} = 1 / 1
  string   pressure/unit     attr   = "Pa"
  double   temperature       5*{40} = 0 / 0
  string   temperature/unit  attr   = "K"
```

## Python Read “step-by-step” example

```python
import numpy as np
from adios2 import Stream

with Stream("cfd.bp", "r") as s:
    # steps comes from the stream
    for _ in s.steps():

        # track current step
        print(f"Current step is {s.current_step()}")

        # inspect variables in current step
        for name, info in s.available_variables().items():
            print("variable_name: " + name, end=" ")
            for key, value in info.items():
                print("\t" + key + ": " + value, end=" ")
            print()

        if s.current_step() == 0:
            nproc = s.read("nproc")
            print(f"nproc is {nproc} of type {type(nproc)}")

        # read variables return a numpy array with corresponding selection
        physical_time = s.read("physical_time")
        print(f"physical_time is {physical_time} of type {type(physical_time)}")
        temperature = s.read("temperature")
        temp_unit = s.read_attribute("temperature/unit")
        print(f"temperature array size is {temperature.size} of shape {temperature.shape}")
        print(f"temperature unit is {temp_unit} of type {type(temp_unit)}")
        pressure = s.read("pressure")
        press_unit = s.read_attribute("pressure/unit")
        print(f"pressure unit is {press_unit} of type {type(press_unit)}")
        print()
```

```text
$ python3 adios2-doc-read.py
Current step is 0
variable_name: nproc    AvailableStepsCount: 1  Max: 4  Min: 4  Shape:          SingleValue: true       Type: int64_t
variable_name: physical_time    AvailableStepsCount: 1  Max: 0  Min: 0  Shape:          SingleValue: true       Type: double
variable_name: pressure         AvailableStepsCount: 1  Max: 1  Min: 1  Shape: 40       SingleValue: false      Type: double
variable_name: temperature      AvailableStepsCount: 1  Max: 0  Min: 0  Shape: 40       SingleValue: false      Type: double
nproc is 4 of type <class 'numpy.ndarray'>
physical_time is 0.0 of type <class 'numpy.ndarray'>
temperature array size is 40 of shape (40,)
temperature unit is K of type <class 'str'>
pressure unit is Pa of type <class 'str'>

Current step is 1
variable_name: physical_time    AvailableStepsCount: 1  Max: 0.01       Min: 0.01       Shape:          SingleValue: true   Type: double
variable_name: pressure         AvailableStepsCount: 1  Max: 1  Min: 1  Shape: 40       SingleValue: false      Type: double
variable_name: temperature      AvailableStepsCount: 1  Max: 0  Min: 0  Shape: 40       SingleValue: false      Type: double
physical_time is 0.01 of type <class 'numpy.ndarray'>
temperature array size is 40 of shape (40,)
temperature unit is K of type <class 'str'>
pressure unit is Pa of type <class 'str'>

...
```

## Python Read Random Access example

```python
import numpy as np
from adios2 import FileReader

with FileReader("cfd.bp") as s:
    # inspect variables
    vars = s.available_variables()
    for name, info in vars.items():
        print("variable_name: " + name, end=" ")
        for key, value in info.items():
            print("\t" + key + ": " + value, end=" ")
        print()
    print()

    nproc = s.read("nproc")
    print(f"nproc is {nproc} of type {type(nproc)} with ndim {nproc.ndim}")

    # read variables return a numpy array with corresponding selection
    steps = int(vars['physical_time']['AvailableStepsCount'])
    physical_time = s.read("physical_time", step_selection=[0, steps])
    print(
        f"physical_time is {physical_time} of type {type(physical_time)} with "
        f"ndim {physical_time.ndim} shape = {physical_time.shape}"
    )

    steps = int(vars['temperature']['AvailableStepsCount'])
    temperature = s.read("temperature", step_selection=[0, steps])
    temp_unit = s.read_attribute("temperature/unit")
    print(f"temperature array size is {temperature.size} of shape {temperature.shape}")
    print(f"temperature unit is {temp_unit} of type {type(temp_unit)}")

    steps = int(vars['pressure']['AvailableStepsCount'])
    pressure = s.read("pressure", step_selection=[0, steps])
    press_unit = s.read_attribute("pressure/unit")
    print()
```

```text
$ python3 adios2-doc-read-filereader.py
variable_name: nproc    AvailableStepsCount: 1  Max: 4  Min: 4  Shape:          SingleValue: true       Type: int64_t
variable_name: physical_time    AvailableStepsCount: 5  Max: 0.04       Min: 0  Shape:          SingleValue: true       Type: double
variable_name: pressure         AvailableStepsCount: 5  Max: 1  Min: 1  Shape: 40       SingleValue: false      Type: double
variable_name: temperature      AvailableStepsCount: 5  Max: 0  Min: 0  Shape: 40       SingleValue: false      Type: double

nproc is 4 of type <class 'numpy.ndarray'> with ndim 0
physical_time is [0.   0.01 0.02 0.03 0.04] of type <class 'numpy.ndarray'> with ndim 1 shape = (5,)
temperature array size is 200 of shape (200,)
temperature unit is K of type <class 'str'>
```

## adios2 classes for Python

Stream is a high-level class that can perform most of the ADIOS functionality. FileReader is just a convenience class and is the same as Stream with “rra” (ReadRandomAccess) mode. FileReaders do not work with for loops as opposed to Streams that work step-by-step, rather one can access any step of any variable at will. The other classes, Adios, IO, Engine, Variable, Attribute and Operator correspond to the C++ classes. One needs to use them to extend the capabilities of the Stream class (e.g. using an external XML file for runtime configuration, changing the engine for the run, setting up a compression operator for an output variable, etc.)

### classadios2.Stream(path, mode, comm=None)

High level implementation of the Stream class from the core API

#### propertyadios

Adios instance associated to this Stream

#### all_blocks_info(name)

Extracts all available blocks information for a particular variable. This can be an expensive function, memory scales up with metadata: steps and blocks per step

Args:
name (str): variable name

Returns:
list of dictionaries with information of each step

#### available_attributes(varname='', separator='/')

Returns a 2-level dictionary with attribute information. Read mode only.

Parameters
variable_name
If varname is set, attributes assigned to that variable are returned. The keys returned are attribute names with the prefix of varname + separator removed.

separator
concatenation string between variable_name and attribute e.g. varname + separator + name (“var/attr”) Not used if varname is empty

Returns
attributes dictionary
key
attribute name

value
attribute information dictionary

#### available_variables()

Returns a 2-level dictionary with variable information. Read mode only.

Returns
variables dictionary
key
variable name

value
variable information dictionary

#### begin_step(*, timeout=-1.0)

Write mode: declare the starting of an output step. Pass data in stream.write() and stream.write_attribute(). All data will be published in end_step().

Read mode: in streaming mode releases the current step (no effect in file based engines)

#### close()

Closes stream, thus becoming unreachable. Not required if using open in a with-as statement. Required in all other cases per-open to avoid resource leaks.

#### current_step()

Inspect current step of the stream. The steps run from 0.

Note that in a real stream, steps from the producer may be missed if the consumer is slow and the producer is told to discard steps when no one is reading them in time. You may see non-consecutive numbers from this function call in this case.

Use loop_index() to get a loop counter in a for … .steps() loop.

Returns
current step

#### define_variable(name)

Define new variable without specifying its type and content. This only works for string output variables

#### end_step()

Write mode: declaring the end of an output step. All data passed in stream.write() and all attributes passed in stream.write_attribute() will be published for consumers.

Read mode: in streaming mode releases the current step (no effect in file based engines)

#### propertyengine

Engine instance associated to this Stream

#### get_metadata()

Get metadata of an open file in a serialized form that can be sent to other processes and used to open the file again by avoiding reading the metadata from disk.

#### inquire_attribute(name, variable_name='', separator='/')

Inquire an attribute

Parameters
name
attribute name

variable_name
if attribute is associated with a variable

separator
concatenation string between variable_name and attribute e.g. variable_name + separator + name (“var/attr”) Not used if variable_name is empty

Returns
The attribute if it is defined, otherwise None

#### inquire_variable(name)

Inquire a variable

Parameters
name
variable name

Returns
The variable if it is defined, otherwise None

#### propertyio

IO instance associated to this Stream

#### loop_index()

Inspect the loop counter when using for-in loops. This function returns consecutive numbers from 0.

Returns
the loop counter

#### minmax(name, step: int | None = None, block_info_list: list = [])→ tuple

Get min/max value for variable: - ‘r’ mode: min/max in the current step

Setting ‘step’ input will cause error.

‘rra’ mode:
the global all-step min/max if ‘step’ is not given,

the given step’s min/max

If ‘bis’ is given as the result of previous all_blocks_info() call, it will be used instead of querying again.

Parameters
:
name – Variable name

step – Which step. It will cause error in ‘r’ mode if set.

bis – Result of previous all_blocks_info() call.

Return a tuple of min/max values

#### propertymode

Selected open mode

#### num_steps()

READ MODE ONLY. Return the number of steps available. Note that this is the steps of a file/stream. Each variable has its own steps, which needs to inspected with var=stream.inquire_variable() and then with var.steps()

#### read(variable: Variable, start=[], count=[], block_id=None, step_selection=None, defer_read: bool = False)

read(name: str, start=[], count=[], block_id=None, step_selection=None, defer_read: bool = False)
Read a variable. Random access read allowed to select steps.

Parameters
variable
adios2.Variable object to be read Use variable.set_selection(), set_block_selection(), set_step_selection() to prepare a read

start
variable offset dimensions

count
variable local dimensions from offset

block_id
(int) Required for reading local variables, local array, and local value.

step_selection
(list): On the form of [start, count].

defer_read
False: read now and blocking wait for completion (Sync mode) True: defer reading all requests until read_complete().

The returned numpy array will be filled with data only after calling read_complete().

Returns
array
resulting array from selection

#### read_attribute(name, variable_name='', separator='/')

Reads a numpy based attribute

Parameters
name
attribute name

variable_name
if attribute is associated with a variable

separator
concatenation string between variable_name and attribute e.g. variable_name + separator + name (var/attr) Not used if variable_name is empty

Returns
array
resulting array attribute data

#### read_attribute_string(name, variable_name='', separator='/')

Reads a numpy based attribute

Parameters
name
attribute name

variable_name
if attribute is associated with a variable

separator
concatenation string between variable_name and attribute e.g. variable_name + separator + name (var/attr) Not used if variable_name is empty

Returns
array
resulting array attribute data

#### read_complete()

Complete reading all deferred read requests. The returned numpy arrays of each read(…, defer_read=True) will be filled with data after this call.

#### read_in_buffer(variable: Variable, buffer, start=[], count=[], block_id=None, step_selection=None, defer_read: bool = False)

read_in_buffer(name: str, buffer, start=[], count=[], block_id=None, step_selection=None, defer_read: bool = False)
Read a variable into a preallocated buffer. Random access read allowed to select steps.

Parameters
variable
adios2.Variable object to be read Use variable.set_selection(), set_block_selection(), set_step_selection() to prepare a read

buffer
the pre-allocated buffer that will hold the read data (numpy, cupy, torch.Tensor arrays)

start
variable offset dimensions

count
variable local dimensions from offset

block_id
(int) Required for reading local variables, local array, and local value.

step_selection
(list): On the form of [start, count].

defer_read
False: read now and blocking wait for completion (Sync mode) True: defer reading all requests until read_complete().

The returned numpy array will be filled with data only after calling read_complete().

#### set_parameters(**kwargs)

Sets parameters using a dictionary. Removes any previous parameter.

Parameters
parameters
input key/value parameters

value
parameter value

#### step_status()

Inspect the stream status. Return adios2.bindings.StepStatus

#### steps(num_steps=0, *, timeout=-1.0)

Returns an interator that can be use to itererate throught the steps. In each iteration begin_step() and end_step() will be internally called.

Write Mode: num_steps is a mandatory argument and should specify the number of steps.

Read Mode: if num_steps is not specified there will be as much iterations as provided by the actual engine. If num_steps is given and there is not that many steps in a file/stream, an error will occur.

IMPORTANT NOTE: Do not use with ReadRandomAccess mode.

#### write(variable: Variable, content)

write(name: str, content, shape=[], start=[], count=[], operations=None)
Writes a variable. Note that the content will be available for consumption only at the end of the for loop or in the end_step() call.

Parameters
variable
adios2.Variable object to be written Use variable.set_selection(), set_shape(), add_operation_string() to prepare a write

content
variable data values

#### write_attribute(name, content, variable_name='', separator='/')

writes a self-describing single value array (numpy) variable

Parameters
name
attribute name

array
attribute numpy array data

variable_name
if attribute is associated with a variable

separator
concatenation string between variable_name and attribute e.g. variable_name + separator + name (“var/attr”) Not used if variable_name is empty

### classadios2.FileReader(path: str, comm=None)

High level implementation of the FileReader class for read Random access mode

### classadios2.Adios(config_file=None, comm=None)

High level representation of the ADIOS class in the adios2.bindings

#### at_io(name)

Inquire IO instance

Args:
name (str): IO instance name

Returns:
IO: IO instance

#### declare_io(name)

Declare IO instance

Args:
name (str): IO instance name

#### define_operator(name, kind, parameters={})

Add an operation (operator).

Args:
name (str): name of the operator. kind (str): name type of the operation. params (dict): parameters as a form of a dict for the operation.

#### flush_all()

Flush all IO instances

#### propertyimpl

Bindings implementation of the class

#### inquire_operator(name)

Add an operation (operator).

Args:
name (str): name of the operator.

Return:
Operator: requested operator

#### remove_all_ios()

Remove all IO instance

#### remove_io(name)

Remove IO instance

Args:
name (str): IO instance name

### classadios2.IO(impl, name, adiosobj)

High level representation of the IO class in the adios2.bindings

#### add_transport(kind, parameters={})

Adds a transport and its parameters to current IO. Must be supported by current engine type.

Parameters
kind
must be a supported transport type for current engine.

parameters
acceptable parameters for a particular transport CAN’T use the keywords “Transport” or “transport” in key

Returns
transport_index
handler to added transport

#### adios()

Adios instance associated to this IO

#### available_attributes(varname='', separator='/')

Returns a 2-level dictionary with attribute information. Read mode only.

Parameters
variable_name
If varname is set, attributes assigned to that variable are returned. The keys returned are attribute names with the prefix of varname + separator removed.

separator
concatenation string between variable_name and attribute e.g. varname + separator + name (“var/attr”) Not used if varname is empty

Returns
attributes dictionary
key
attribute name

value
attribute information dictionary

#### available_variables()

Returns a 2-level dictionary with variable information. Read mode only.

Parameters
keys
list of variable information keys to be extracted (case insensitive) keys=[‘AvailableStepsCount’,’Type’,’Max’,’Min’,’SingleValue’,’Shape’] keys=[‘Name’] returns only the variable names as 1st-level keys leave empty to return all possible keys

Returns
variables dictionary
key
variable name

value
variable information dictionary

#### define_attribute(name, content=None, variable_name='', separator='/')

Define an Attribute

Parameters
name
attribute name

content
attribute numpy array data

variable_name
if attribute is associated with a variable

separator
concatenation string between variable_name and attribute e.g. variable_name + separator + name (“var/attr”) Not used if variable_name is empty

#### define_derived_variable(name, expression, etype=None)

Define a derived variable with an expression

Parameters
name
name as it appears in variable list in the output

expression
expression string using other variable names, operators and functions

type
DerivedVarType.StatsOnly : store only the metadata of the derived variable DerivedVarType.ExpressionString : store only the definition, nothing else DerivedVarType.StoreData : store as a complete variable (data and metadata)

#### define_variable(name, content=None, shape=[], start=[], count=[], is_constant_dims=False)

writes a variable

Parameters
name
variable name

content
variable data values

shape
variable global MPI dimensions.

start
variable offset for current MPI rank.

count
variable dimension for current MPI rank.

isConstantDims
Whether dimensions are constant

#### engine_type()

Return engine type

#### flush_all()

Flush all engines attached to this IO instance

#### propertyimpl

Bindings implementation of the class

#### inquire_attribute(name, variable_name='', separator='/')

Inquire an Attribute

Parameters
name
attribute name

variable_name
if attribute is associated with a variable

separator
concatenation string between variable_name and attribute e.g. variable_name + separator + name (“var/attr”) Not used if variable_name is empty

#### inquire_variable(name)

Inquire a variable

Parameters
name
variable name

Returns
The variable if it is defined, otherwise None

#### open(name, mode, comm=None)

Open an engine

Parameters
name
Engine name

mode
engine mode

comm
MPI communicator, optional

#### open_with_metadata(name, metadata: bytes)

Open an engine with metadata already in memory

Parameters
name
Engine name

metadata
file metadata retrieved by FileReader.get_metadata()

#### parameters()

Return parameter associated to this io instance

Return:
dict: str->str parameters

#### remove_all_attributes()

Remove all define attributes

#### remove_all_variables()

Remove all variables in the IO instance

#### remove_attribute(name)

Remove an Attribute

Parameters
name
attribute name

#### remove_variable(name)

Remove a variable

Parameters
name
Variable name

#### set_engine(name)

Set engine for this IO instance

Args:
name (str): name of engine

#### set_parameter(key, value)

Set a parameter for this IO instance

Args:
key (str): value (str):

#### set_parameters(parameters)

Set parameters for this IO instance

Args:
parameters (dict):

### classadios2.Engine(implementation)

High level representation of the Engine class in the adios2.bindings

#### all_blocks_info(name)

Returns a list of BlocksInfo for a all steps

Args:
name (str): variable name

Returns:
list of BlockInfos

#### begin_step(*args, **kwargs)

Start step

#### between_step_pairs()

End step

#### blocks_info(name, step)

Returns a BlocksInfo for a given step

Args:
name (str): variable name step (int): step in question

Returns:
list of dicts describing each BlockInfo

#### close(transport_index=-1)

Close an transports of the engine

Args:
transportIndex (int): if -1 close all transports

#### current_step()

Returns the current step

#### end_step()

End step

#### flush(transport_index=-1)

Flush all transports attached to this Engine instance

#### get(variable, content=None, mode=Mode.Sync)

Gets the content of a variable

Parameters
name
variable name

content
output variable data values

mode
when to perform the communication, (Deferred or Asynchronous).

Returns
Content of the variable when the content argument is omitted.

#### get_metadata()

Get metadata of an open file in a serialized form that can be sent to other processes and used to open the file again by avoiding reading the metadata from disk.

#### propertyimpl

Bindings implementation of the class

#### lock_reader_selections()

Locks the data selection for read

#### lock_writer_definitions()

Locks the data selection for write

#### perform_data_write()

Perform the transport data writes

#### perform_gets()

Perform the gets calls

#### perform_puts()

Perform the puts calls

#### put(variable, content, mode=Mode.Deferred)

Puts content in a variable

Parameters
name
variable name

content
variable data values

mode
when to perform the communication, (Deferred or Asynchronous).

#### steps()

Returns number of available steps

### classadios2.Variable(implementation)

High level representation of the Variable class in the adios2.bindings

#### add_operation(operation, params={})

Add an operation (operator).

Args:
name (Operator): name of the operation. params (dict): parameters as a form of a dict for the operation.

#### add_operation_string(name, params={})

Add an operation (operator) as a string

Args:
name (str): name of the operation. params (dict): parameters as a form of a dict for the operation.

#### block_id()

BlockID of this variable.

Returns:
int: BlockID of this variable.

#### count()

Current selected count for this variable.

Returns:
int: Current selected count.

#### get_accuracy()

Get the accuracy of the variable (of its last read)

Returns:
adios2.bindings.Accuracy struct (with error, norm and relative fields).

#### propertyimpl

Bindings implementation of the class

#### name()

Name of the Variable

Returns:
str: Name of the Variable.

#### operations()

Current operations (operators) assigned to this Variable.

Returns:
list(Operators): operators assigned.

#### remove_operations()

Remove operations (operators) assigned to this Variable.

#### selection_size()

Current selection size selected for this variable.

Returns:
int: Current selection size selected.

#### set_accuracy(error, norm, relative)

Set Accuracy for (remote) reading for this variable

Args:
error: floating point value norm: floating point value relative: True or False

#### set_block_selection(block_id)

Set BlockID for this variable.

Args:
block_id (int): Selected BlockID.

#### set_selection(selection)

Set selection for this variable.

Args:
selection (list): list of the shape [[start], [count]], note that start and count can contain more than one element.

#### set_shape(shape)

Set Shape (dimensions) for this variable

Args:
shape (list): desired shape (dimensions).

#### set_step_selection(step_selection)

Set current step selection (For RRA or ReadRandomAccess)

Args:
step_selection (list): On the form of [start, count].

#### shape(step=None)

Get the shape assigned to the given step for this variable.

Args:
step (int): Desired step. Only in ReadRandomAccess mode

Returns:
list: shape of the specified step in the form of [start, count].

#### shape_id()

Get the ShapeID assigned to this variable.

Returns:
int: ShapeID assigned to this variable.

#### single_value()

Check if this variable is a single value.

Returns:
bool: true if this variable is a single value.

#### sizeof()

Size in bytes of the contents of the variable.

Returns:
int: size in bytes of the contents.

#### start()

The current selected start of the variable.

Returns:
int: The current selected start of the variable.

#### steps()

The number of steps of the variable. This is always 1 in a stream. In ReadRandomAccess mode, this function returns the total number of steps available, which can be used when selecting steps for read.

Returns:
int: The avaialble steps of the variable.

#### steps_start()

The avaliable start step, this is needed variables can start at any time step. This is for ReadRandomAccess.

Returns:
int: the starting step of for this Variable.

#### store_stats_only(mode)

Select writing mode for variable (write data or just stats)

#### type()

Type of the Variable

Returns:
str: Type of the Variable.

### classadios2.Attribute(io, name, *args, **kwargs)

High level representation of the Attribute class in the adios2.bindings

#### data()

Content of the Attribute

Returns:
Content of the Attribute as a non string.

#### data_string()

Content of the Attribute

Returns:
Content of the Attribute as a str.

#### propertyimpl

Bindings implementation of the class

#### name()

Name of the Attribute

Returns:
Name of the Attribute as a str.

#### single_value()

True if the attribute is a single value, False if it is an array

Returns:
True or False.

#### type()

Type of the Attribute

Returns:
Type of the Attribute as a str.

### classadios2.Operator(implementation, name)

High level representation of the Attribute class in the adios2.bindings

#### get_parameters()

Get parameters associated to this Operator

Returns:
dict: parameters

#### propertyimpl

Bindings implementation of the class

#### set_parameter(key, value)

Set parameter associated to this Operator

Args:
str: key str: value

## Python bindings to C++

Note

The bindings to the C++ functions is the basis of the native Python API described before. It is still accessible to users who used the “Full Python API” pre-2.10. In order to make old scripts working with 2.10 and later versions, change the import line in the python script.

```python
import adios2.bindings as adios2
```
The full Python APIs follows very closely the full C++11 API interface. All of its functionality is now in the native API as well, so its use is discouraged for future scripts.

## Examples using the Python bindings in the ADIOS2 repository

Simple file-based examples
- `examples/hello/helloWorld/hello-world-bindings.py`

- `examples/hello/bpReader/bpReaderHeatMap2D-bindings.py`

- `examples/hello/bpWriter/bpWriter-bindings.py`

Staging examples using staging engines SST and DataMan
- `examples/hello/sstWriter/sstWriter-bindings.py`

- `examples/hello/sstReader/sstReader-bindings.py`

## ADIOS class

### classadios2.bindings.ADIOS

#### AtIO

returns an IO object previously defined IO object with DeclareIO, throws an exception if not found

#### DeclareIO

spawn IO object component returning a IO object with a unique name, throws an exception if IO with the same name is declared twice

#### DefineOperator

#### FlushAll

flushes all engines in all spawned IO objects

#### InquireOperator

#### RemoveAllIOs

DANGER ZONE: remove all IOs in current ADIOS object, creates dangling objects to parameters, variable, attributes, engines created with removed IO

#### RemoveIO

DANGER ZONE: remove a particular IO by name, creates dangling objects to parameters, variable, attributes, engines created with removed IO

## IO class

### classadios2.bindings.IO

#### AddTransport

#### AvailableAttributes

#### AvailableVariables

#### DefineAttribute

#### DefineDerivedVariable

#### DefineVariable

#### EngineType

#### FlushAll

#### InquireAttribute

#### InquireVariable

#### Open

#### Parameters

#### RemoveAllAttributes

#### RemoveAllVariables

#### RemoveAttribute

#### RemoveVariable

#### SetEngine

#### SetParameter

#### SetParameters

## Variable class

### classadios2.bindings.Variable

#### AddOperation

#### BlockID

#### Count

#### GetAccuracy

#### GetAccuracyRequested

#### Name

#### Operations

#### RemoveOperations

#### SelectionSize

#### SetAccuracy

#### SetBlockSelection

#### SetSelection

#### SetShape

#### SetStepSelection

#### Shape

#### ShapeID

#### SingleValue

#### Sizeof

#### Start

#### Steps

#### StepsStart

#### StoreStatsOnly

#### Type

## Attribute class

### classadios2.bindings.Attribute

#### Data

#### DataString

#### Name

#### SingleValue

#### Type

## Engine class

### classadios2.bindings.Engine

#### BeginStep

#### BetweenStepPairs

#### BlocksInfo

#### Close

#### CurrentStep

#### EndStep

#### Flush

#### Get

#### GetMetadata

#### LockReaderSelections

#### LockWriterDefinitions

#### Name

#### PerformDataWrite

#### PerformGets

#### PerformPuts

#### Put

#### Steps

#### Type

## Operator class

### classadios2.bindings.Operator

#### Parameters

#### SetParameter

#### Type

## Query class

### classadios2.bindings.Query

#### GetBlockIDs

#### GetResult

## Transition from old API to new API

A python script using the high-level API of 2.9 and earlier needs to be modified to work with 2.10 and later.

adios2.open() is replaced with adios2.Stream(), and does not have 4th and 5th optional arguments for external xml and IO name.

the for in file is replaced with for _ in file.steps() but it works for both writing (by specifying the number of output steps) and reading (for the number of available steps in a stream/file).

```python
# OLD API
import adios2

# NEW API
from adios2 import Adios, Stream

# NEW API: this still works
import adios2

# OLD API
fr = adios2.open(args.instream, "r", mpi.comm_app,"adios2.xml", "SimulationOutput")

# NEW API
adios = Adios("adios2.xml", mpi.comm_app)
io = adios.declare_io("SimulationOutput")
fr = Stream(io, args.instream, "r", mpi.comm_app)

# OLD API
for fr_step in fr:
    fr_step....

# NEW API 1
for _ in fr.steps():
    fr....

# NEW API 2
for fr_step in fr.steps():
    fr_step....
```