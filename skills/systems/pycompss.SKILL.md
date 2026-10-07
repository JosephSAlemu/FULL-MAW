---
name: systems/pycompss
description: >
  PyCOMPSs reference for MAW workflows. Covers the generated-file execution model
  (write_workflow/run_workflow), @task/@binary/@mpi decorators, the COMPSs runtime
  lifecycle and its two launch modes, plus how COMPSs is installed on this cluster.
---

# PyCOMPSs — System Skill

PyCOMPSs is a task-based parallel programming model from BSC (Barcelona
Supercomputing Center). Work is expressed as `@task`-decorated functions and
scheduled by the COMPSs runtime, which detects dependencies automatically from
task parameters.

Load this skill when generating or debugging PyCOMPSs workflow code.

---

## READ THIS FIRST: You Write One PyCOMPSs File, COMPSs Runs Everything

The pycompss engine is **generated-file only**. You have exactly two execution
tools:

| Tool | What it does |
|---|---|
| `write_workflow(python_code, filename)` | Writes ONE standalone PyCOMPSs driver file to `/app/work/run0/` |
| `run_workflow(filename)` | The server executes that file |

**You never execute anything yourself.** No subprocess, no direct CLI call, no
bash script, no PBS script, no `mpirun` typed by you. Command-line work is
expressed with COMPSs's own `@binary`/`@mpi` decorators, and COMPSs builds and
launches the command.

### The rule

Every unit of work in the generated file is a decorated function:

- **`@task`** — pure Python work: analysis, plotting, writing summaries.
- **`@binary(binary="...")` stacked above `@task`** — a command-line program.
- **`@mpi(binary="...", runner="mpirun", processes=N)` stacked above `@task`** —
  an MPI program on N ranks.

All of them live in the **same file**. A simulation plus its visualization is one
file containing an `@mpi` task and a `@task` — not two files, not two tool calls.

### What `write_workflow` rejects

- no `@task` defined anywhere
- `@binary`/`@mpi` used without `@task` beneath it
- importing or calling `subprocess`, or calling `os.system` / `os.popen`
- `mpirun`/`mpiexec`/`srun` named outside an `@mpi` decorator argument
- a runtime lifecycle that doesn't match this server's launch mode (below)

On rejection, read the returned `errors`, fix the file, and call
`write_workflow` again. Do not look for another way to run the command.

---

## Runtime Lifecycle: Match the Launch Mode

`write_workflow` reports `launch_mode` in its result. The generated file must
match it, and this is enforced:

| `launch_mode` | When | Your file must |
|---|---|---|
| `runcompss` | COMPSs available, inside a PBS job | **NOT** call `compss_start()`/`compss_stop()` — the `runcompss` launcher owns the runtime |
| `direct` | COMPSs available, outside a PBS job | **Call** `compss_start()` first and `compss_stop()` last |
| `fallback` | COMPSs not installed | Same shape as `direct`; the file still runs, just without COMPSs orchestration |

Calling the lifecycle functions under `runcompss` double-initializes the
runtime; omitting them in `direct` mode means no runtime ever starts.

---

## `@binary` — Command-Line Programs

The decorated function has a `pass` body. Arguments come from the **function
signature**, and their roles are declared in the `@task` decorator — not from a
tuple of pre-built argv strings.

```python
from pycompss.api.task import task
from pycompss.api.binary import binary
from pycompss.api.parameter import FILE_IN_STDIN, FILE_OUT_STDOUT

@binary(binary="grep", working_dir=".")
@task(infile={Type: FILE_IN_STDIN}, result={Type: FILE_OUT_STDOUT})
def grepper(keyword, infile, result):
    pass

grepper("Hi", "infile.txt", "outfile.txt")
# runs:  grep Hi < infile.txt > outfile.txt
```

Three ways to shape the command line:

| Need | How |
|---|---|
| Positional argument | A plain function parameter; its value is inserted in signature order |
| Redirect stdin/stdout | `@task(f={Type: FILE_IN_STDIN})` / `{Type: FILE_OUT_STDOUT}` |
| Flag prefix | `@task(hide={Type: FILE_IN, Prefix: "--hide="})` → `--hide=fileToHide.txt` |
| Free-form arg string | `@binary(binary="date", args="-d {{param_1}}")` — `args` is a **string** with `{{name}}` placeholders bound to parameters, not a tuple |

`fail_by_exit_value=True` makes a nonzero exit fail the task. The default
(`False`) returns the exit code as the task's return value instead, which is
what you want when a binary's nonzero exit is tolerable and you need to inspect
it.

Files declared `FILE_IN`/`FILE_OUT` are transferred to the worker and tracked in
the dependency graph, which is how COMPSs orders stages — that is the reason to
declare them rather than passing bare path strings.

---

## `@mpi` — MPI Programs

```python
from pycompss.api.mpi import mpi

@mpi(binary="hacc_tpm", runner="mpirun", processes=8)
@task(paramfile={Type: FILE_IN})
def run_hacc(paramfile):
    pass
```

`processes=N` is how rank count is expressed — you never type `mpirun -n 8`.
Parameters, files, and prefixes work exactly as in `@binary`.

Other forms:

- **mpi4py code instead of a binary**: omit `binary=` and put the Python MPI
  code in the function body.
  ```python
  @mpi(processes=4)
  @task()
  def rank_report():
      from mpi4py import MPI
      return MPI.COMM_WORLD.rank
  ```
- **MPI + OpenMP**: `processes=` for ranks, plus `computing_units=` in a
  `@constraint` for threads per rank.
- **Limit spread across nodes**: `processes_per_node=2` with `processes=4` packs
  the four ranks into two nodes, two per node.
- **Per-rank data slices**: combine with collections and a
  `<arg_name>_layout={block_count, block_length, stride}` to avoid serializing
  the whole collection to every rank.

`runner=` selects the launcher. Argument conventions differ between MPI
implementations; set `COMPSS_MPIRUN_TYPE` to `impi` (Intel) or `ompi` (OpenMPI)
so COMPSs passes the right flags.

---

## Worked Example: an MPI step and a Python step in one file

Direct-link mode (outside PBS), so the file manages the lifecycle itself.

```python
from pycompss.api.task import task
from pycompss.api.mpi import mpi
from pycompss.api.parameter import FILE_IN, FILE_OUT
from pycompss.api.api import compss_start, compss_stop, compss_wait_on


@mpi(binary="/path/to/hacc_tpm", runner="mpirun", processes=8)
@task(paramfile={Type: FILE_IN}, snapshot={Type: FILE_OUT})
def run_sim(paramfile, snapshot):
    pass


@task(returns=str)
def visualize(snapshot, outdir):
    import os
    import matplotlib
    matplotlib.use("Agg")           # headless compute node
    import matplotlib.pyplot as plt

    os.makedirs(outdir, exist_ok=True)
    # ... read snapshot, render ...
    png = os.path.join(outdir, "density_slice.png")
    plt.savefig(png, dpi=150)
    plt.close()
    return png


def main():
    work = "/app/work/run0"
    snapshot = f"{work}/snapshot.dat"

    compss_start()
    try:
        # COMPSs orders these from the FILE_OUT -> argument dependency;
        # no manual waiting between them.
        run_sim("/path/to/indat.params", snapshot)
        png = compss_wait_on(visualize(snapshot, work))
        print(f"wrote {png}")
    finally:
        compss_stop()


if __name__ == "__main__":
    main()
```

Under `runcompss` mode, delete the `compss_start()`/`compss_stop()` calls and
the `try/finally`; everything else is identical.

`compss_wait_on()` only where the driver needs a real value — COMPSs infers
ordering from parameters, so waiting between submissions just serializes it.

---

## Key Differences from Parsl

| Feature | Parsl | PyCOMPSs |
|---|---|---|
| Python task | `@python_app` | `@task` |
| CLI task | `@bash_app` returning a command string | `@binary` + `@task`, `pass` body |
| MPI task | `@bash_app` returning `mpirun ...` | `@mpi(processes=N)` + `@task` |
| Init | `parsl.load(Config(...))` | `compss_start()`, or nothing under runcompss |
| Future resolution | `future.result()` | `compss_wait_on(future)` |
| Shutdown | `parsl.clear()` | `compss_stop()` |
| Worker imports | Must be inside the function body | Module-level is fine |
| Dependencies | Explicit via futures/`File` | Automatic from task parameters |

---

## Common Pitfalls

| Pitfall | Rule |
|---|---|
| `subprocess` anywhere in the file | Rejected. CLI work is `@binary`/`@mpi` |
| Writing `mpirun -n 8 ...` yourself | Rejected. Use `@mpi(processes=8)` |
| `@binary`/`@mpi` without `@task` under it | Both are required, `@task` innermost |
| A non-`pass` body under `@binary` | The body is ignored; arguments come from the signature |
| `args=("flag", "value")` as a tuple | `args` is a **string** with `{{name}}` placeholders |
| `compss_start()` under `runcompss` mode | Double-initializes the runtime — check `launch_mode` |
| Missing `compss_start()` in `direct` mode | No runtime ever starts |
| Passing paths as bare strings between tasks | Invisible to the scheduler. Declare `FILE_IN`/`FILE_OUT` |
| `compss_wait_on()` after every submission | Serializes the graph; wait only where a real value is needed |
| Rendering without `matplotlib.use("Agg")` | Headless nodes have no display |

---

## MCP Server Behavior

The server (`servers/pycompss_server.py`) runs the generated file in one of
three modes, reported as `launch_mode` by `write_workflow` and as
`launched_via` by `run_workflow`:

1. **`runcompss`** (COMPSs available AND inside a PBS job): the file is launched
   through the real `runcompss` binary against a `project.xml`/`resources.xml`
   pair generated for this node's own hostname (`_compss_config_files()`).
   It targets the real hostname rather than "localhost" because `runcompss`'s
   default local config launches its worker over SSH, and this cluster enforces
   SSH-key+Duo MFA on login nodes — SSH between nodes *inside* an active PBS
   allocation isn't subject to that policy.

2. **`direct`** (COMPSs available, NOT in a PBS job): the file runs under plain
   `VENV_PYTHON` and self-manages `compss_start()`/`compss_stop()`. Used because
   `runcompss`'s SSH-based worker launch would hit the login-node Duo wall
   outside a job allocation.

3. **`fallback`** (COMPSs not available): the file runs as plain Python. Same
   results, no COMPSs orchestration. Lets the workflow run on machines without
   COMPSs installed.

The `engine` field reports which runtime actually ran:
- `"pycompss"` — COMPSs runtime used (mode 1 or 2)
- `"pycompss-fallback"` — direct Python execution (mode 3)

---

## Running with COMPSs

On Improv there is no `module load COMPSs` — it was hand-built (see below) and
lives at `~/.local/COMPSs`. `servers/pycompss_server.py` hardcodes the required
env vars (`PYTHONPATH`, `LD_LIBRARY_PATH`, `COMPSS_HOME`, `JAVA_HOME`) into
`TASK_ENV` with sensible defaults, so no manual setup is needed:

```bash
python agent_mcp.py --engine pycompss --paper 1 --goal "..."
```

On local machines without COMPSs the same command works and falls back to
direct execution.

---

## How COMPSs Got Installed on Improv

**`pip install pycompss` does not work reliably — do not rely on it.** The PyPI
`pycompss` package is a thin wrapper whose `setup.py` downloads the real ~925MB
COMPSs distribution and runs its own install script as a side effect. Two bugs
in that wrapper, both confirmed by direct reproduction:

1. It silently installs to `~/.local/lib/.../site-packages` instead of the active
   venv if `VIRTUAL_ENV` isn't set — calling `./venv3/bin/pip` directly (without
   `source venv3/bin/activate`) triggers this silently.
2. Even with `VIRTUAL_ENV` set correctly, its nested
   `pip install --no-build-isolation --target=... .` step for the Python bindings
   can fail with `KeyError: 'TARGET_OS'` depending on call context — an env-var
   propagation bug in COMPSs's own `install.sh`/`setup.py`, not ours to fix.

**What actually works: run COMPSs's real installer directly**, bypassing the pip
wrapper entirely:

```bash
module load openjdk/21.0.0_35   # JAVA_HOME -- no module named "java", must use "openjdk"
module load boost/1.84.0        # for the C++ bindings-common build
# libxml2-devel has no module/package anywhere on Improv (no root, no spack CLI
# exposed) -- built from source instead, shared (not static) so it can link into
# COMPSs's .so targets:
#   curl -LO https://download.gnome.org/sources/libxml2/2.12/libxml2-2.12.9.tar.xz
#   ./configure --prefix=$HOME/.local/libxml2 --without-python && make -j4 && make install
export PATH="$HOME/.local/libxml2/bin:$PATH"
export CPATH="$HOME/.local/libxml2/include/libxml2:$CPATH"
export LIBRARY_PATH="$HOME/.local/libxml2/lib:$LIBRARY_PATH"
export LD_LIBRARY_PATH="$HOME/.local/libxml2/lib:$LD_LIBRARY_PATH"

curl -LO http://compss.bsc.es/repo/sc/stable/COMPSs_3.4.tar.gz
tar xzf COMPSs_3.4.tar.gz && cd COMPSs
bash install --no-tracing ~/.local/COMPSs
```

Confirmed end-to-end: `compss_start()` / `compss_stop()` round-trip cleanly
against this install. **COMPSs is not a site-packages install** — its own
installer puts the Python bindings at
`~/.local/COMPSs/Bindings/python/3/pycompss` (always major version "3",
regardless of the Python 3.x minor version) and expects activation via
`PYTHONPATH`/`LD_LIBRARY_PATH`/`COMPSS_HOME`, not `pip`. `pycompss_server.py`
builds these from `COMPSS_HOME` (defaults to `~/.local/COMPSs`, override via env
var if installed elsewhere).

---

## `runcompss` Requires COMPSs Env Vars in `~/.bashrc`

`pycompss_server.py` sets `COMPSS_HOME`/`JAVA_HOME`/`LD_LIBRARY_PATH`/
`PYTHONPATH`/`PATH` for its own subprocess (`TASK_ENV`), which covers the
master-side `runcompss` process. But `runcompss`'s worker launch opens a
**separate SSH session** to the target node, and SSH does not forward the
master's env vars — the worker gets whatever a fresh login shell sets up via
`~/.bashrc`. So `JAVA_HOME` (and `PATH` including `$JAVA_HOME/bin`) must also be
exported in `~/.bashrc`, or the SSH-launched worker JVM fails with
`Can't find JVM libraries in JAVA_HOME` or `setsid: failed to execute java: No
such file or directory`, and the master hangs retrying forever. `~/.bashrc` on
this account has this covered; replicate those exports if COMPSs is reinstalled
elsewhere or on a new account.

---

## Notes

- PyCOMPSs requires Java + the COMPSs runtime for full functionality
- Fallback mode keeps the workflow runnable anywhere
- On HPC, COMPSs handles task scheduling, data transfers, and fault tolerance
- Results are identical across modes — only orchestration differs
