# MAW — Multi-Agent Workflow

An agentic system that executes computational scientific workflows end to end and
autonomously. A pipeline of LLM agents reads the user's description (and
optionally a paper and a figure), decides what needs to be built, installs the
packages, and then executes every step of the workflow itself — running the
simulation, doing the analysis, generating the plots and renders — through real
tool calls against a real workflow engine.

The distinguishing design choice is **how** the workflow is executed. Rather than
generating one standalone script and running it in one shot, this system exposes
the workflow engine itself as an **MCP server**. The explorer agent calls that
server's tools one step at a time — write a file, run the workflow, read the
status, check a package, retry — the way you would drive a REPL. When a step
fails, only that step is fixed and retried.

```
Agent layer  (Academy agents, orchestrated by a LangGraph state machine)
  orchestrator -> planner -> orchestrator -> installer
    -> orchestrator -> installer (pass 2) -> explorer -> orchestrator -> end
                                                |
                                                | MCP (JSON-RPC over stdio)
                                                v
Engine layer (one MCP server per engine, all subclassing src/servers/server.py)
  parsl_server.py | pycompss_server.py | adios.py | wilkins.py
                                                |
                                                v
Execution     the Spack environment + venv3: the engine, the science packages,
              and whatever modules a task loads in its own command string
```

The agent logic never touches engine logic. The explorer calls the same tool
names whichever server is behind the session; only the constructs the generated
file must use differ, and those come from the engine's skill files.

---

## Agents

| Agent | Responsibility |
|---|---|
| `orchestrator` | Reviews each agent's output. **It never sends an agent back** — when it finds a problem it corrects it itself (`tasks_override`, `requirements_override`) and the corrected version goes forward on a fixed route. |
| `planner` | Reads the paper and goal; produces quantitative literature findings, the pip stack (`stack_decision`), the environment modules (`modules`), and an ordered task list. |
| `installer` | Phase 1 writes `work/builds/requirements.txt` from the planner's stack and stops for approval. Phase 2 pip-installs the approved list, under `modules` when the domain names any. It has no language model. |
| `explorer` | Runs a ReAct loop against the engine's MCP server: writes the workflow files, runs them, reads the output, installs what is missing, retries failures. |

The route is a fixed table in `src/agents.py`; the orchestrator's `next` and
`feedback` fields are ignored and blanked. Re-entering an agent costs a full LLM
session — plus a PDF re-read for the planner — and was observed to return the
same output, so the orchestrator corrects rather than re-asks.

---

## Workflow engines

Selected with `--engine`. Every server inherits the same tool surface from
`src/servers/server.py`:

`write_file` · `read_file` · `list_files` · `check_package` · `install_package`
· `get_status` · `get_resources` · `run_workflow` · `cleanup`

Execution is **generated-file only**: no tool takes a code string or a command
string and runs it. Each engine adds the tools it alone needs, and the explorer
binds only those (`_ENGINE_ONLY_TOOLS` in `src/mcp_explorer.py`):

| Engine | Extra tools | What the agent generates |
|---|---|---|
| `parsl` | — | ONE self-contained driver that builds its own `Config`, calls `parsl.load()`, and schedules `@bash_app` / `@python_app` |
| `pycompss` | `write_xml` | ONE application file of `@task` / `@binary` / `@mpi`. The **server generates the XML topology** from the allocation; `write_xml` is an override you should not need |
| `adios` | depends on `MAW_ADIOS`, below | ONE FILE PER STAGE — a pipeline is several programs streaming to each other |
| `wilkins` | `write_config`, `check_lowfive` | A YAML config (which *is* the workflow) plus each task's program |

### The two ADIOS2 variants

`--engine adios` starts one of two server classes in `src/servers/adios.py`,
chosen by `MAW_ADIOS`. **The default, and the fallback for any unrecognised
value, is `serial`.**

| | `serial` (default) | `mpi` |
|---|---|---|
| Build | the PyPI wheel in `venv3` | `+mpi +python +sst`, built under `spack_mpi/` |
| Launch tool | `run_pipeline` — stages as concurrent processes | `run_mpmd` — stages as rank blocks of one `mpirun` |
| Also exposes | `write_bp`, `read_bp` | `write_stage` |
| SST streaming | yes, over TCP | yes |
| MPMD / collective parallel write | no | yes |
| MPI binding | none, so it works with whatever MPI a domain names | bound to the one MPI it was built against |

Both can be installed at once: `PYTHONPATH` precedes the venv's site-packages,
so `module load adios2/<ver>-<mpi>` shadows the wheel and unloading restores it.
`MAW_ADIOS` is normalised in one place (`common.consts.adios_variant`) and the
canonical value is passed to the server, so the client's tool bindings cannot
disagree with the server's class.

### Adding an engine

Subclass `Server` with `ENGINE`, `DESCRIPTION` and `launch_command()`; register
any engine-only tools in `register_tools()`. Then add it to `ENGINE_SERVERS` and
`_ENGINE_ONLY_TOOLS` in `src/mcp_explorer.py`, to `ENGINE_SKILLS` and
`ENGINE_EXEC_PROMPTS` in `src/systemprompts/explorer.py`, to the `--engine`
choices in `academy_agents.py`, and write `skills/systems/<engine>.SKILL.md` and
`skills/knowledge/<ENGINE>.SKILL.md`. Nothing in the agent layer changes.

---

## The skills system

Agent behaviour is not hardcoded. Each agent's operating manual is a Markdown
skill file injected into its context at runtime; the system prompt in code is
only the JSON schema contract.

```
skills/
├── agents/          one behavioural spec per role (orchestrator, planner,
│                    installer, explorer) — loaded on every call
├── knowledge/       local.SKILL.md / lcrc.SKILL.md, loaded from --env;
│                    PARSL / PyCOMPSs / ADIOS / WILKINS API manuals, loaded
│                    from --engine                        (<= 500 lines each)
├── systems/         one per engine: the execution contract, the rank layouts,
│                    and the pitfalls                      (<= 500 lines each)
└── use_cases/<domain>/   domain knowledge, selected by --domain
```

A `systems/<engine>` skill and its `knowledge/<ENGINE>` companion are
concatenated into one prompt that is resent on every loop iteration, so they are
written not to repeat each other. Absolute paths to anything outside the repo
belong **only** in `use_cases/*`; everywhere else, use the `/app/` aliases the
servers resolve.

A domain folder may hold either one `domain.SKILL.md` for every agent or one file
per agent (`planner.SKILL.md`, `explorer.SKILL.md`, ...). Both layouts work and
both resolve in either direction.

### Adding a use case

1. Drop the PDF in `inputs/literature/`, any input files in `inputs/data/`, and
   any figure in `inputs/images/`.
2. Create `skills/use_cases/<name>/domain.SKILL.md` — copy an existing domain as
   a template. The sections the post-run review writes against are **Tools**,
   **Input Parameters**, **Outputs**, **Pitfalls** and **Guidelines**.
3. Run with `--domain <name>`. The folder name is matched **exactly**; `--list-domains`
   prints what is on disk. No code change is needed anywhere.

Your `--goal` should cover: what the workflow computes, which simulator and
analysis tools each task uses, whether input data is provided or generated, the
key parameters (size, steps, resolution, ranks), the expected outputs and how you
would know it worked, and the environment and scale.

---

## Setup

**Requirements:** a C/C++ compiler Spack can find, a JVM for PyCOMPSs, and — for
HPC — an allocation on a PBS Pro cluster.

```bash
./bootstrap.sh                  # run ONCE per machine; --spack-root <path> optional
spack env activate .            # from the repo root, before every run
```

`bootstrap.sh` does only what cannot bootstrap itself: install Spack, build the
MPI-free core environment from `spack.yaml`, create `venv3` from that
environment's Python, and `pip install -r requirements.txt`. Everything else —
compiler, `JAVA_HOME`, `COMPSS_HOME`, `MAW_PYTHON`, `LD_LIBRARY_PATH`,
`MODULEPATH`, and putting `venv3` on `PATH` — is done by `spack env activate`,
which runs `spack_repo/maw/packages/env/package.py`. There is no activate script
and no `setup_hpc.sh`.

Do **not** run `venv3/bin/activate` after activating the environment: it restores
the pre-venv `PATH` and drops `JAVA_HOME`'s bin, the compiler and `mpirun`.

The MPI-linked engines (ADIOS2 `+mpi`, Wilkins/LowFive) live in a second
environment under `spack_mpi/`, built by hand only if you need them:

```bash
module load <the MPI your domains use>
cd spack_mpi && cp spack.yaml.in spack.yaml   # uncomment the specs you want
spack -e . external find openmpi mpich intel-oneapi-mpi
spack -e . install && spack -e . module lmod refresh -y
```

### Configuration

Copy `.env.example` to `.env` and fill in the LLM backend. Every variable is
documented there. The agent talks to any OpenAI-compatible endpoint (built
against Argonne's Argo gateway) via `langchain-openai`.

Only `OPENAI_API_KEY`, `OPENAI_BASE_URL` and `MODEL_NAME` are required;
`PBS_ACCOUNT` is required for `--env multi-node`. **Archiving is off unless you
set `MAW_ARCHIVE_ROOT`** — a run that is not archived beats one archived into
somebody's home directory by accident.

### Scientific packages

There is no hand-maintained list. The planner decides the stack from the paper,
the installer writes it to `work/builds/requirements.txt` and installs it after
the orchestrator approves, and the explorer calls `install_package` for anything
still missing mid-run. The venv is persistent, so it only grows; the first run
for a new domain takes noticeably longer.

---

## Running

```bash
spack env activate .

python academy_agents.py --combination d --env local \
  --domain new_molecular_nucleation \
  --goal "Run a 2-task LAMMPS + OVITO workflow: simulate crystallization, then
          identify diamond structures and render PNGs with TachyonRenderer."
```

`--combination` is the only required flag. `--env` defaults to `local` and
`--engine` to `parsl`.

### Flags

| Flag | Default | Values | Meaning |
|---|---|---|---|
| `--combination` | **required** | `a` PDF+image+desc · `b` PDF+desc · `c` image+desc · `d` desc only | Which planner inputs are used. Data files are offered regardless |
| `--env` | `local` | `local` · `hpc` · `multi-node` | See below |
| `--engine` | `parsl` | `parsl` · `pycompss` · `adios` · `wilkins` | Which MCP server to start |
| `--domain` | *(prompted)* | an exact folder name under `skills/use_cases/` | Selects the domain skill and labels the run |
| `--list-domains` | — | — | Print the available domains and exit |
| `--paper` | *(prompted)* | 1-based index into `inputs/literature/` | Only used for `--combination a`/`b` |
| `--image` | *(prompted)* | a path, or a name in `inputs/images/` | Only used for `--combination a`/`c` |
| `--goal` | *(prompted)* | free text | The workflow to reproduce |
| `--condition` | `B` | `A` no skills · `B` full · `C` single-agent | Ablation condition |
| `--trial` | `1` | int | Distinguishes repeated runs of the same configuration |
| `--nodes` / `--ranks` | `0` | int | `--env multi-node` only, both required and >= 1 |

### The three environments

**`--env local`** — one machine, no scheduler. Every agent runs in this process's
event loop. `knowledge/local` is loaded, which forbids PBS, `mpirun`/`srun` and
multi-node configs.

**`--env hpc`** — inside a PBS job you already hold. Everything still runs in one
process; nothing is submitted. `PBS_JOBID` is checked at startup, because the
only other thing that notices is the explorer's `get_resources` — by which point
the planner and installer have each burned an LLM session for a run that cannot
place a single rank.

```bash
qsub -I -A <allocation> -l select=1:ncpus=8:mpiprocs=8 -l walltime=01:00:00
cd <repo> && spack env activate .
python academy_agents.py --env hpc --engine parsl --combination b --paper 1 \
  --domain cosmology --goal "Reproduce the density-field analysis"
```

**`--env multi-node`** — from a login node. The orchestrator, planner and
installer stay in this process; the **explorer** runs on the first node of a
PBS job this script submits, asking for `select=<nodes>:ncpus=<ranks>:mpiprocs=<ranks>`.
The allocation is exactly `--nodes` wide, so `get_resources` reports the same
shape the agent was told. The explorer's output goes to
`runs/<run_id>_explorer.log` on the shared filesystem and is tailed into this
terminal; its trace events go to a side-car JSONL and are merged back before the
run trace is saved.

The three LLM-latency-bound agents never go to a compute node in any mode — a
node reserved for one of them is a node reserved to idle.

### On HPC

`get_resources` reports the allocation from `PBS_JOBID`, `PBS_NODEFILE`, `PBS_NP`
and `PBS_NUM_PPN`. Generated workflows size their rank counts from it rather than
hardcoding, and use **`mpirun`, not `srun`** — this is PBS, not SLURM.

No MPI is configured for the run. Which MPI a workflow needs is a property of the
domain's tools, stated in its skill file, and the agent writes that `module load`
into the task's own command string. A module loaded in the worker environment
would be inherited by every task, including the ones that want a different one.

Task code and task lists always use `/app/...`, never a cluster path — the server
resolves `/app/` to the repo, `/app/work/run0` to the work directory and
`/app/inputs/data` to the data directory. Put the repo and venv on persistent
project storage, never on node-local scratch.

Compute nodes are headless: rendering needs `LIBGL_ALWAYS_SOFTWARE=1` and
`PYOPENGL_PLATFORM=osmesa`, and `matplotlib.use("Agg")` inside the task.

---

## Output

```
runs/<run_id>.jsonl                       orchestrator routing decisions
runs/<run_id>_trace.json                  every LLM call, tool call and routing
                                          decision, with timings and tokens
runs/<run_id>_skill_recommendations.md    written only if you report a failure
work/run0/                                everything the run produced
work/run0/<run_id>.out / .err             each run_workflow's live streams
work/builds/requirements.txt              regenerated every run
```

`work/run0/` is a single fixed working directory shared by every run. Set
`MAW_ROTATE_RUN0=1` to move a non-empty one aside instead of writing over it —
worth doing, because a stale output left in place can make a `@bash_app` ending
in `test -s <output>` report success without having run.

When `MAW_ARCHIVE_ROOT` is set, each finished run is copied to
`<root>/<date>__<domain>__<engine>__<condition>__<combination>__trial<n>/`, engine
scratch pruned, on both the success and failure paths. `work/run0` is **not**
cleared afterwards, so a bug in the archiver cannot lose both the copy and the
original.

At the end of a run the explorer asks whether the workflow executed properly. If
you say no, it diagnoses the run against your description and proposes additions
to the domain skill file, which it will apply for you on confirmation.

`SAMPLE RUNS/` holds three complete archived runs (cosmology, molecular
nucleation and eddy_uv, all Parsl, condition B) — browse those to see what a
finished run looks like without executing anything.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| `--env hpc` refuses to start | `PBS_JOBID` is unset, so this is a login shell. Get an allocation, or use `--env multi-node` |
| Explorer reports `in_pbs: false` | Same, from inside the run |
| `No Spack environment is active` | `spack env activate .` from the repo root, before launching |
| `JAVA_HOME is unset` / `MAW_PYTHON is unset` | The environment is active but `maw.env` did not run, or there is no venv. Re-run `bootstrap.sh` |
| `pycompss is not installed` on a machine where it is | A half-activated environment — you ran `venv3/bin/activate` after `spack env activate` |
| PyCOMPSs hangs with an empty `~/.COMPSs/*/jobs/` | A topology with no resources. Read `compss_topology` from `get_resources`; do not write the XML yourself |
| `not enough slots available` | `@mpi(processes=N)` without `processes_per_node`, which defaults to 1 and makes `processes` a NODE count |
| ADIOS2 `Cannot use MPI since ADIOS2 was built without MPI support` | A `comm=` argument against the serial wheel. Drop it, or use `MAW_ADIOS=mpi` |
| A Wilkins run completes but writes real `.h5` files | LowFive was not the active VOL. Call `check_lowfive` first |
| `No PDFs found in the inputs/literature/ folder` | Add a paper, or use `--combination c`/`d` |
| `NameError` on a worker in a Parsl app | A module-level import or constant used inside an app body. Imports go **inside** the function |
