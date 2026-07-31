# MAW — MCP Approach (Multi-Agent Workflow)

## What This Project Is

This is an agentic system that handles end-to-end execution of computational scientific workflows described
by the user, **end to end and autonomously**. A pipeline of LLM agents parses user input, decides what needs to be built, installs the required packages, and then
executes every step of the workflow itself — running simulations, doing analysis,
generating plots/renders — using real tool calls against a real workflow engine.

The distinguishing design choice is **how the agent executes the workflow**: instead of
generating one big standalone Python script and running it all at once (the "artifact"
approach, developed in a separate sibling project), this system exposes the workflow
engine itself (Parsl, PyCOMPSs, or ADIOS2) as an **MCP (Model Context Protocol) server**.
The agent calls that server's tools one step at a time — submit a task, check its status,
read its output, install a missing package, retry on failure — the same way you'd
interactively drive a REPL. If one step fails, only that step gets fixed and retried;
nothing needs to be regenerated or re-run from scratch.

Three scientific domains are currently supported out of the box (papers live in
`Literature/`):

| Domain | Paper | What gets reproduced |
|---|---|---|
| `molecular_nucleation` | `MOLECULAR.pdf` | LAMMPS MD simulation of nucleation + OVITO analysis/rendering |
| `cosmology` | `COSMOLOGY.pdf` | N-body-style particle simulation + density-field analysis |
| `eddy_uv` | `eddy-nekrs.pdf` | Fluid dynamics (eddy/streamfunction) simulation + visualization |

This repo is the **full multi-agent condition** (four specialized agents, "Condition B"
in the ablation naming used throughout the CLI/skills). A sibling repo,
`MCP_Approach_SINGLE`, implements the same MCP tool-calling approach but collapses all
four roles into a single agent ("Condition C") — everything below about the workflow
engine, MCP servers, environment setup, and HPC execution applies identically there;
only the agent-layer role split differs.

### Input Types

Every run is built from up to four kinds of input. **Description is the only one every
run needs** — Paper and Images are optional, gated by the required `--combination` flag
(`a`=PDF+Image+Desc, `b`=PDF+Desc, `c`=Image+Desc, `d`=Desc only — see the
[Flag Matrix](#flag-matrix--every-value-you-can-pass-on-hpc) below); Config/Data is
always optional regardless of `--combination`.

**Images:**
- Workflow design images/sketches of the user-defined workflow.
- Optional — pass a path with `--image`, or pick one interactively from `images/` at
  startup (enter `0` to skip). Only used when `--combination` is `a` or `c`.

**Description:**
- Free-text goal describing the workflow to reproduce, passed with `--goal "..."`.
- The one input every run needs, regardless of `--combination`.

  Example (`molecular_nucleation`):
  > I would like to have a 2-task workflow consisting of one producer and one consumer task. The producer runs a LAMMPS molecular dynamics simulation of crystallization and generates trajectory data. The consumer runs OVITO, which reads the resulting trajectory dump file, identifies diamond structures using the diamond structure identification modifier, renders each frame using the TachyonRenderer, and saves the output as PNG images.

  The full goal text for every other use case (`cosmology`, `eddy_uv`, ...) lives in
  [`inputs/usecase_descriptions/descriptions.py`](inputs/usecase_descriptions/descriptions.py)
  rather than being duplicated here — open that file for the exact wording used per domain.

**Paper:**
- The PDF being reproduced, dropped into `Literature/` and selected with
  `--paper <1-based index or path>`.
- Optional — only used when `--combination` is `a` or `b`.

**Config/Data:**
- Static input files a workflow needs at runtime (LAMMPS input scripts, force fields,
  etc.), placed in `data/`.
- Optional — prompted for interactively at startup (press Enter to select all); if
  `data/` is empty the run proceeds with no input data.


---

## Architecture

```
Agent Layer (LangGraph state machine)
  orchestrator -> planner -> installer -> explorer -> end
                                            |
                                            | MCP protocol (JSON-RPC over stdio)
                                            v
Workflow Engine Layer (MCP Server, one per engine)
  servers/parsl_server.py     -- Parsl
  servers/pycompss_server.py  -- PyCOMPSs
  servers/adios_server.py     -- ADIOS2


Execution Layer (Python venv)
  LAMMPS, OVITO, Parsl, ADIOS2, numpy, matplotlib, astropy, camb, ase, etc.
  (installed on demand — see "How Packages Get Installed" below)
```

The MCP server runs as a single long-lived subprocess started by the explorer agent.
Communication is stdio-based JSON-RPC. This keeps the agent logic (LangGraph, LLM
calls, retries) completely decoupled from the engine logic (how a task actually gets
scheduled and run) — the explorer calls the same tool names (`submit_task`,
`get_task_status`, etc.) no matter which engine is behind the server.

### Agent Roles

| Agent | Responsibility |
|---|---|
| `orchestrator` | Supervisor. Reviews each agent's output after it runs and decides where to route next (retry, move forward, or end). Owns the two-phase installer approval flow. |
| `planner` | Reads the paper (PDF text, optionally an image/figure) and produces: quantitative literature findings, the package list (`stack_decision`), and an ordered task list (10–15 tasks) the explorer will execute. |
| `installer` | Phase 1: writes/regenerates `builds/requirements.txt` from the planner's `stack_decision` and asks the orchestrator to approve it. Phase 2 (after approval): `pip install`s those packages into the venv. |
| `explorer` | Runs a ReAct loop, calling MCP server tools to execute each planner task: submitting code, checking status/output, installing anything missing, retrying failures. |

Flow: `orchestrator -> planner -> installer -> explorer -> end`, with the orchestrator
able to route backward to `planner` (if tasks are vague/contradict the environment) or
back to `explorer` (if critical tasks failed) rather than always moving forward linearly.

### The Skills System (`skills/`)

Agent behavior is not hardcoded in Python — each agent's operating manual is a Markdown
"skill" file, injected into its context at runtime. The system prompt in code is just
the JSON schema contract; the skill file is the actual instructions.

```
skills/
├── agents/                        # one behavioral spec per agent role
│   ├── orchestrator.SKILL.md
│   ├── planner.SKILL.md
│   ├── installer.SKILL.md
│   └── explorer.SKILL.md
├── knowledge/                     # environment knowledge, loaded based on --env
│   ├── local.SKILL.md             # local machine constraints (no PBS, no MPI, single node)
│   └── lcrc.SKILL.md              # LCRC/HPC constraints (PBS variables, mpirun, storage paths)
├── systems/                       # one skill per workflow engine, loaded based on --engine
│   ├── parsl.SKILL.md
│   ├── pycompss.SKILL.md
│   └── adios.SKILL.md
└── use_cases/                     # one skill per domain, requested by the planner/explorer
    ├── molecular_nucleation/
    ├── cosmology/
    └── eddy_uv/
        ├── orchestrator.SKILL.md
        ├── planner.SKILL.md
        ├── installer.SKILL.md
        └── explorer.SKILL.md
```

Agents request additional skills on their first tool call via a `skill_requests` field
(e.g. `["use_cases/cosmology/explorer", "systems/parsl"]`); the relevant environment
knowledge skill (`local` or `lcrc`) is loaded automatically based on `--env`.

### Adding a New Use Case

Use cases (domains) are auto-discovered from `skills/use_cases/` at runtime — there is
no registry to edit in `agent_mcp.py` or `mcp_explorer.py`. To add one, using an
existing domain (e.g. `cosmology`) as a template:

1. **Add the paper.** Drop the PDF into `Literature/`.
2. **Add the goal text.** 

Your description should include the following:

Scientific goal— what should the workflow compute or produce?
Method / software— which simulator and analysis tools should each task use?
Input data— is input data provided, or generated by the workflow itself?
Key parameters— size, steps, resolution, MPI ranks (or leave blank for defaults)?
Expected outputs / success criteria— what outputs, and how do we know it worked?
Environment & scale— local or HPC, and how many processes / ranks / nodes?


EXAMPLES:
   [`inputs/usecase_descriptions/descriptions.py`](inputs/usecase_descriptions/descriptions.py)
   with the free-text description of the workflow — this is what you'll pass to
   `--goal` (or paste in when prompted).

   
4. **(Optional) add a diagram.** Drop a workflow sketch/figure into
   `inputs/input_images/` if you want to be able to run with `--combination a` or `c`.
5. **Write the skill files.** Create `skills/use_cases/<name>/` with the same four
   files every other use case has:
   ```
   skills/use_cases/<name>/
   ├── orchestrator.SKILL.md
   ├── planner.SKILL.md
   ├── installer.SKILL.md
   └── explorer.SKILL.md
   ```
   Copy an existing use case's files as a starting point and rewrite the
   domain-specific parts (expected packages, task breakdown, engine quirks, etc.) —
   keep the same section structure each file expects.
6. **Run it.** Pass `--domain <name>` where `<name>` matches (or substring-matches)
   the `skills/use_cases/<name>/` folder name — the orchestrator, planner, and
   explorer each pick up the matching skill files automatically (`_list_skills` in
   `agent_mcp.py` just lists what's on disk). No code changes are needed anywhere else.

Optionally, add a row for the new domain to the table in
["What This Project Is"](#what-this-project-is) above so it's discoverable at a glance.

---

## Repo Layout

```
MCP_Approach/
├── agent_mcp.py           # Entry point: CLI, LangGraph graph, orchestrator/planner/installer nodes
├── mcp_explorer.py        # Explorer agent: ReAct loop, MCP client connection, engine dispatch
├── run_archiver.py        # Snapshots work/run0 + trace.json after every run, then wipes run0
├── trace_logger.py        # Per-run event tracing (LLM calls, tool calls, routing, timing)
├── trace_schema.py        # Pydantic schema — single source of truth for trace.json's shape
├── requirements.txt       # Agent-side deps only (LangGraph, MCP SDK, etc.) — see setup below
├── setup_hpc.sh           # Source this before running on LCRC (Improv/Swing) — sets MPI lib paths
├── .env                   # LLM API config (NOT committed — see Environment Setup)
├── servers/
│   ├── parsl_server.py       # Parsl MCP server
│   ├── pycompss_server.py    # PyCOMPSs MCP server
│   └── adios_server.py       # ADIOS2 MCP server
├── skills/                # see "The Skills System" above
├── Literature/             # source PDFs the planner reads (one per domain)
├── data/                   # static input files (LAMMPS input scripts, force fields, etc.)
├── images/                 # example output images used in docs
├── SAMPLE RUNS/            # two complete real run archives (see "Run Output" below) —
│                           #   browse these to see exactly what a finished run looks like
├── builds/                 # generated at runtime — requirements.txt regenerated per run
├── runs/                   # per-run JSONL logs (gitignored)
├── work/                   # live scratch dir, work/run0 (gitignored, wiped/archived each run)
└── venv3/                  # the project's Python venv (gitignored — you create this)
```

---

## Workflow Engines

All three engines expose the **same MCP tool interface**, so the explorer agent's logic
is identical regardless of which one is selected with `--engine`. Each falls back to
plain subprocess execution if the real engine library isn't installed, so the system
still runs (just without that engine's scheduling/optimizations).

| Tool | Parsl | PyCOMPSs | ADIOS2 | Description |
|---|:---:|:---:|:---:|---|
| `submit_task` | ✓ | ✓ | ✓ | Submit Python code for execution, with dependency tracking |
| `submit_shell_task` | ✓ | ✓ | ✓ | Run a shell command via the engine |
| `submit_mpi_task` | ✓ | ✓ | ✓ | Run an MPI command (`mpirun -np $PBS_NP ...`) |
| `run_lammps` | ✓ | ✓ | ✓ | Purpose-built LAMMPS task launcher |
| `get_task_status` | ✓ | ✓ | ✓ | pending / running / completed / failed |
| `get_task_result` | ✓ | ✓ | ✓ | Full stdout/stderr of a completed task |
| `list_tasks` | ✓ | ✓ | ✓ | List all submitted tasks and statuses |
| `get_resources` | ✓ | ✓ | ✓ | PBS node/rank info — **explorer must call this first on HPC** |
| `install_package` | ✓ | ✓ | ✓ | `pip install` into the venv, mid-run |
| `check_package` | ✓ | ✓ | ✓ | Verify a package is importable |
| `list_files` / `read_file` | ✓ | ✓ | ✓ | Inspect working directory contents |
| `write_bp` / `read_bp` | – | – | ✓ | ADIOS2 BP-format I/O between pipeline stages |
| `cleanup` | ✓ | ✓ | ✓ | Stop and clean up the MCP server process |

- **Parsl**: tasks run as real `@python_app`s on a `HighThroughputExecutor` (falls back to subprocess if not installed).
- **PyCOMPSs**: tasks run under the real COMPSs runtime with `compss_wait_on()` dependency tracking when available; falls back to direct execution otherwise. Built by BSC.
- **ADIOS2**: adds high-performance streaming I/O (BP file format, SST) between pipeline stages on top of the same task-execution model. Built by ORNL.

To add a new engine: implement the same tool set in a new `servers/<engine>_server.py`,
register it in `ENGINE_SERVERS` in `mcp_explorer.py`, and add the engine name to the
`--engine` choices in `agent_mcp.py`. Nothing else in the agent layer needs to change.

---

## Environment Setup

**Requirements:** Python 3.11, and (for HPC) an LCRC account with access to Improv, Swing, or Bebop.

### 1. Create the venv

There is one combined venv for both the agent framework and the scientific workflow
packages — despite the historical naming split between `requirements.txt` (agent) and
`builds/requirements.txt` (workflow), everything installs into the same environment.

```bash
cd MCP_Approach
python3 -m venv venv3
source venv3/bin/activate
pip install -r requirements.txt
```

This installs only the **agent-side** dependencies: LangGraph, the `mcp` SDK,
`langchain-openai`, `pypdf`, `rich`, etc. — enough to start the agent pipeline.

### 2. How the scientific packages get installed

You will **not** find a hand-maintained list of scientific packages (LAMMPS, OVITO,
Parsl, ADIOS2, numpy, astropy, camb, ase, ...) to `pip install` up front — the `installer`
agent generates and installs that list *itself*, per run, based on what the `planner`
decides the specific paper needs (`stack_decision`). Concretely:

1. Planner reads the paper, decides the package list.
2. Installer writes that list to `builds/requirements.txt` and shows it to the
   orchestrator for approval.
3. Once approved, installer runs `pip install` on it, into the **same venv** the agent
   itself is running in.
4. If the explorer hits a missing package mid-execution anyway, it calls the
   `install_package` MCP tool directly — no need to wait for another install phase.

Because the venv is persistent across runs, packages accumulate — the venv only grows
over time as new papers/domains are run against it. The first run for a new domain will
take noticeably longer while it installs everything.

### 3. Configure the LLM backend

Create a `.env` file in the repo root (this file is gitignored — you must create your
own):

```bash
OPENAI_API_KEY=<your key>
OPENAI_BASE_URL=<your endpoint>
MODEL_NAME=<model name>
```

The agent talks to an OpenAI-compatible endpoint (this project was built against
Argonne's Argo API gateway) via `langchain-openai`. Any OpenAI-compatible
`base_url`/`api_key`/model combination will work.

### 4. Add papers

Drop any PDF you want reproduced into `Literature/`. The CLI lists them by index at
startup; `--paper 1` (etc.) selects by that index, or you can pass a direct path.

---

## Running Locally

```bash
source venv3/bin/activate

python agent_mcp.py \
  --paper 1 \
  --combination b \
  --goal "Reproduce this workflow using LAMMPS and OVITO"
```

`--env` defaults to `local` and `--engine` defaults to `parsl`. On a local machine, the
`knowledge/local` skill is loaded automatically, which forbids the agent from
recommending PBS, MPI, `mpirun`/`srun`, or multi-node configs — everything runs
single-process on one machine.

---

## Testing a New Simulation Without Pre-Configuring a Use Case

You don't need to go through the full [Adding a New Use Case](#adding-a-new-use-case)
setup (paper, `descriptions.py` entry, skill files) just to try something new. Upload
any supporting artifacts you have — a workflow diagram (`--image <path>`, or drop it
in `inputs/input_images/`), input files in `data/` — skip `--paper`, and use
`--combination d` (Desc only). Everything else comes from your `--goal`.

Since the goal message is the only thing describing the simulation in this path,
write one that covers these six considerations:

| Consideration | Ask yourself |
|---|---|
| Scientific goal | What should the workflow compute or produce? |
| Method / software | Which simulator and analysis tools should each task use? |
| Input data | Is input data provided, or generated by the workflow itself? |
| Key parameters | Size, steps, resolution, MPI ranks (or leave blank for defaults) |
| Expected outputs / success criteria | What outputs, and how do we know it worked? |
| Environment & scale | Local or HPC, and how many processes/ranks/nodes? |

```bash
python agent_mcp.py \
  --combination d \
  --goal "Run a 2-task LAMMPS + OVITO workflow: simulate crystallization, then \
identify diamond structures and render PNGs with TachyonRenderer. Use 4 MPI ranks \
locally; success = one PNG per frame showing colored diamond-structure atoms."
```

---

## Running on HPC (LCRC — Improv / Swing / Bebop)

### One-time venv setup (on a login node — compute nodes may lack internet access)

```bash
cd /lcrc/project/<your-project>/MCP_Approach
python3 -m venv venv3
source venv3/bin/activate
pip install -r requirements.txt
```

Store the repo and venv under `/lcrc/project/<project>/` (persistent, backed up) — not
under `/scratch` (15 GB, node-local, wiped after the job ends).

### Before every run: set MPI library paths

```bash
source setup_hpc.sh
```

`setup_hpc.sh` sets `LD_LIBRARY_PATH` to the Intel MPI shared libraries
(`libmpi.so.12`, `libfabric.so.1`) that LAMMPS's Python bindings load at import time.
Without it you'll hit:
```
OSError: libmpi.so.12: cannot open shared object file: No such file or directory
```
It also exports `VENV_PYTHON`, pointing the MCP servers at this venv's interpreter so
every task subprocess uses the right Python (and MPI libs) automatically.

### Run inside a PBS allocation

The agent must be run **inside a PBS job** (interactive or batch) — the explorer's
first tool call is always `get_resources`, and if that reports `in_pbs: false`, it will
stop immediately rather than attempt any compute task on a login node.

```bash
qsub -I -A <allocation> -l select=1 -l walltime=01:00:00 -q <queue>   # example interactive job
cd /lcrc/project/<project>/MCP_Approach
source setup_hpc.sh
source venv3/bin/activate

python agent_mcp.py \
  --env hpc \
  --paper 1 \
  --combination b \
  --goal "Reproduce this workflow using LAMMPS and OVITO"
```

### Flag Matrix — Every Value You Can Pass on HPC

| Flag | Required? | Values on HPC | Meaning |
|---|---|---|---|
| `--env` | yes, always `hpc` here | `hpc` | Loads `knowledge/lcrc`, enables PBS-aware MPI sizing |
| `--engine` | no (default `parsl`) | `parsl` \| `pycompss` \| `adios` | Which MCP workflow-engine server to launch |
| `--paper` | no, but needed to pick a paper | 1-based index into `Literature/` (e.g. `1`, `2`, `3`) or a direct PDF path | Which paper the agent reads |
| `--image` | no | path to an image file | Optional figure/diagram to aid planning |
| `--goal` | no, but should always be set | free text | Natural-language goal describing what to reproduce |
| `--domain` | no | `molecular_nucleation` \| `cosmology` \| `eddy_uv` \| any free text \| `""` (default) | Label used in the archive folder name; also what you'll typically set to match the paper |
| `--condition` | no (default `B`) | `A` (no skills) \| `B` (full multi-agent) \| `C` (single-agent) | Ablation condition — `B` is this repo's normal mode |
| `--combination` | **yes** | `a` (PDF+Image+Desc) \| `b` (PDF+Desc) \| `c` (Image+Desc) \| `d` (Desc only) | Which planner inputs are actually used |
| `--trial` | no (default `1`) | any integer | Distinguishes repeated runs of the same (paper, condition) pair |

### Example Commands — One Per Engine

```bash
# Parsl engine, cosmology paper, full multi-agent (default condition B)
python agent_mcp.py --env hpc --engine parsl \
  --paper 1 --domain cosmology --combination b --trial 1 \
  --goal "Reproduce the density-field analysis from this paper"

# PyCOMPSs engine, molecular nucleation paper
python agent_mcp.py --env hpc --engine pycompss \
  --paper 2 --domain molecular_nucleation --combination b --trial 1 \
  --goal "Reproduce this workflow using LAMMPS and OVITO"

# ADIOS2 engine, eddy_uv paper, passing a figure image alongside the PDF
python agent_mcp.py --env hpc --engine adios \
  --paper 3 --image Literature/eddy_uv_diagram.png --domain eddy_uv --combination a --trial 1 \
  --goal "Reproduce the eddy streamfunction visualization from this paper"

# Same run again as a second trial, with the no-skills ablation condition
python agent_mcp.py --env hpc --engine parsl \
  --paper 1 --domain cosmology --condition A --combination b --trial 2 \
  --goal "Reproduce the density-field analysis from this paper"
```

With `--env hpc`, the `knowledge/lcrc` skill is loaded. It reads these PBS variables to
size MPI launches automatically:

| Variable | Meaning |
|---|---|
| `PBS_JOBID` | Set when running inside a PBS job |
| `PBS_NUM_NODES` | Number of allocated nodes |
| `PBS_NP` | Total MPI ranks across all nodes |
| `PBS_NUM_PPN` | Processors per node |
| `PBS_NODEFILE` | Path to file listing allocated hostnames |

The `submit_mpi_task` MCP tool reads `PBS_NP` and automatically runs
`mpirun -np $PBS_NP <command>` — **`mpirun`, not `srun`** (this is a PBS cluster, not
SLURM).

### Storage paths on LCRC

| Path | Use for |
|---|---|
| `/lcrc/project/<project>/` | Repo, venv, results — persistent |
| `/lcrc/globalscratch/<user>/` | Large intermediate output — shared, may be cleared |
| `/scratch` | 15 GB node-local scratch — wiped after job ends, never put the venv here |

Task code and planner task lists always use `/app/...` paths, never a hardcoded
`/lcrc/...` path — the MCP server resolves `/app/` to the actual repo location on disk
automatically.

### Compute node notes

- No display server: headless rendering requires `LIBGL_ALWAYS_SOFTWARE=1` and
  `PYOPENGL_PLATFORM=osmesa` (already handled by the render tasks the agent generates).
- No `module load` for Python — the venv is the only Python environment used.
- Load system modules only for things that can't come from pip (vendor MPI, CUDA, etc.).

---

## CLI Reference (`agent_mcp.py`)

| Flag | Default | Choices | Meaning |
|---|---|---|---|
| `--paper` | — | PDF path or 1-based index into `Literature/` | Which paper to reproduce |
| `--image` | — | path | Optional figure/diagram image to aid planning |
| `--goal` | — | free text | The natural-language goal given to the agent |
| `--engine` | `parsl` | `parsl`, `pycompss`, `adios` | Which workflow engine MCP server to launch |
| `--env` | `local` | `local`, `hpc` | Execution environment — controls which knowledge skill loads |
| `--condition` | `B` | `A`, `B`, `C` | Ablation condition: A = no skills, B = full multi-agent (default), C = single-agent |
| `--combination` | — (required) | `a`, `b`, `c`, `d` | Planner input combination: a=PDF+Image+Desc, b=PDF+Desc, c=Image+Desc, d=Desc only |
| `--domain` | `""` | free text | Optional domain label (e.g. `cosmology`) used in archive folder naming |
| `--trial` | `1` | int | Trial number, for repeated (paper, condition) runs |

---

## Run Output & Archiving

`work/run0/` is a single fixed working directory, shared by every run — the next run
overwrites whatever the previous one left there. Right after each run's trace is saved
(success or failure), `run_archiver.py` automatically:

1. Copies `work/run0/` and that run's `trace.json` into a permanent archive folder
   **outside this repo's live scratch space**.
2. Clears `run0` so the next run starts clean.

Archive folders are named:
```
<date>__<usecase>__<engine>__<condition>__<combination>__trial<trial>
```
e.g. `20260709__cosmology__parsl__B__b__trial1`. Name collisions get a Windows-style
` (2)`, ` (3)`, ... suffix rather than overwriting.

**`SAMPLE RUNS/`** in this repo contains two complete real archived runs (cosmology and
molecular nucleation, both Parsl/condition B) — browse them to see exactly what a
finished run's directory structure, LAMMPS output, renders, and `trace.json` look like
without having to execute anything yourself.

`trace.json` (schema in `trace_schema.py`) records every LLM call, tool call, and
routing decision with elapsed time and timestamps, letting a run be reconstructed or
scored after the fact.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `OSError: libmpi.so.12: cannot open shared object file` | You forgot to `source setup_hpc.sh` before running on HPC |
| Explorer stops immediately, reports `in_pbs: false` | You're on a login node — start a PBS job first |
| `No PDFs found in the Literature/ folder` | Add a PDF to `Literature/` before running |
| `--combination` missing / argparse error | `--combination` is required (a/b/c/d) — see CLI Reference |
