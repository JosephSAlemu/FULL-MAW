"""
Academy Approach -- Multi-Agent Workflow (Academy Runtime)

Every agent (orchestrator / planner / installer / explorer) is an Academy
agent behind an exchange, coordinated by an Academy Manager.

Pipeline:
    orchestrator -> planner -> orchestrator -> installer -> orchestrator
    -> installer -> orchestrator -> explorer -> orchestrator -> end

Environments
------------
Every --env is started the same way, with the Python CLI. On LCRC, source
setup_hpc.sh first; it sets the Intel MPI library paths and VENV_PYTHON.

    --env local       every agent in this process's event loop, on this
                      machine.

    --env hpc         every agent in this process's event loop, inside a PBS
                      job you already hold -- start one with `qsub -I`, then
                      run this on its compute node. Nothing is submitted.

    --env multi-node  Orchestrator, Planner and Installer in this process;
                      the Explorer on the head node of a (nodes + 1)-node PBS
                      job this script submits. That node's worker_init
                      sources the same setup_hpc.sh.

All output appears in the terminal you ran this from, in every mode. Under
local and hpc the Explorer prints there directly. Under multi-node it is in
another process on another node, so it writes runs/<run_id>_explorer.log
on the shared filesystem and this process prints that file as it grows.
Its tracer events go to runs/<run_id>_explorer_trace.jsonl for the same
reason: that process's tracer is not this one's. In local and hpc all four
agents share the tracer main() saves, so every event lands in the run
trace directly.

The Orchestrator, Planner and Installer never go to a compute node in any
mode: all three are LLM-latency bound, so a node reserved for any of them
is a node reserved to idle.

Why there is no MPIExecutor
---------------------------
The Explorer never submits tasks to a Parsl executor. It talks to a server
under servers/ over MCP stdio, and that server's whole job is to write the
files the Explorer generates and then launch the workflow file once. Under
--engine parsl the workflow file builds its own Config and calls parsl.load()
itself, and its MPI ranks come from the mpirun string inside a @bash_app in
that file. An MPIExecutor here would reserve an allocation nothing ever
submits to.

What the multi-node block asks PBS for
--------------------------------------
`select=<nodes+1>:ncpus=<ranks>:mpiprocs=<ranks>`. Both come from the one
--ranks value: mpiprocs puts one line per rank slot in $PBS_NODEFILE, and
ncpus reserves a core for each of those slots. Neither can be left at its
default of 1 -- that gives 1 nodefile line per node, or 1 core per node
however many slots the nodefile advertises. They are passed through
different PBSProProvider parameters (cpus_per_node and select_options)
because the provider's template already emits ncpus; a second one in the
same chunk makes qsub reject the job outright.

Why the multi-node block uses SingleNodeLauncher
------------------------------------------------
Deliberately. It puts exactly one worker pool on the allocation's head
node, which is where $PBS_NODEFILE is valid and readable and where
get_resources can see nnodes/nodelist. The remaining nodes are reserved
by PBS but untouched by Parsl, so the generated workflow's mpirun owns
them outright -- all of them, as one MPI world.

MpiExecLauncher would be wrong here twice over: it would start a pool on
every node (nodes+1 workers for one agent), and pools on non-head nodes
may not be able to read $PBS_NODEFILE, which lives in the head node's
local spool directory.

Usage:
    python academy_agents.py --paper 1 --combination b --env local --goal "..."

    # inside `qsub -I ...`
    source setup_hpc.sh
    python academy_agents.py --paper 1 --combination b --env hpc --goal "..."

    # from a login node; submits the Explorer's job itself
    source setup_hpc.sh
    python academy_agents.py --paper 1 --combination b --env multi-node \\
        --nodes 2 --ranks 4 --goal "..."
"""

import argparse
import asyncio
import contextlib
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import parsl
from parsl import Config, HighThroughputExecutor
from parsl.concurrent import ParslPoolExecutor
from parsl.launchers import SingleNodeLauncher
from parsl.providers import PBSProProvider

from academy.exchange import LocalExchangeFactory
from academy.exchange.cloud.client import HttpExchangeFactory
from academy.manager import Manager

from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel

from common.consts import (
    LITERATURE_PATH, RUN_PATH, IMAGE_PATH,
    DEFAULT_WALLTIME, HPC_MODULES, PROJECT_DIR, SRC_DIR, SETUP_SCRIPT_PATH,
    WORK_DIR, SKILLS_PATH,
)
from common.exceptions import exception_message
from mcp_explorer import post_run_review
from merge_explorer_trace import merge_into_tracer
from run_archiver import archive_run
from trace_logger import tracer
from src.agents import Planner, Installer, Explorer, Orchestrator
from src.states import AgentState, AgentSteps, UserInput

load_dotenv()

console = Console()


# No spare node. The block is exactly --nodes wide, so the allocation and the
# MPI application are the same shape: `--nodes 2 --ranks 4` is two nodes with
# four rank slots each, and $PBS_NODEFILE holds exactly the eight lines the
# workflow's mpiexec may use.
#
# There used to be one extra node on top, which made the allocation one node
# wider than the application. That meant $PBS_NODEFILE -- and therefore
# get_resources -- reported a shape the agent was told, in prose, to disbelieve:
# nnodes=3 for a 2x4 run. Sizing `--map-by ppr:N:node` from a number the prompt
# says to ignore is a trap, and the Explorer's own worker pool needs well under
# one core while it waits on LLM turns, so the node it was being given mostly
# sat idle.


# __ Helpers ___________________________________________________________________

def _slugify(path: str) -> str:
    """Turn a file path into a stable run-metadata id, e.g. for paper_id."""
    base = os.path.splitext(os.path.basename(path))[0] if path else "no_paper"
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "_", base).strip("_")
    return slug or "no_paper"


def available_domains() -> list[str]:
    """The domains: folder names under skills/use_cases/, sorted. [] if none.

    A folder is offered whether it holds the current single domain.SKILL.md or
    the older per-agent files -- which of those it is, is the skill loader's
    problem, not the user's.
    """
    uc_dir = os.path.join(SKILLS_PATH, "use_cases")
    if not os.path.isdir(uc_dir):
        return []
    return sorted(
        name for name in os.listdir(uc_dir)
        if os.path.isdir(os.path.join(uc_dir, name)) and not name.startswith(".")
    )


def resolve_domain(requested: str) -> str:
    """Validate a --domain value, or ask which domain to use.

    The name must match a folder under skills/use_cases/ exactly. It used to be
    matched as a substring, in both directions, against every folder name, and
    then -- failing that -- guessed at by sniffing the first 500 characters of
    each candidate skill for a package name out of the installer's stack
    decision. Both of those could pick the wrong folder, and when they picked
    nothing the run simply proceeded with no domain knowledge and said nothing
    about it. An exact name that is checked before the run starts cannot do
    either.

    Returns the folder name, or "" for a run with no domain skill.
    """
    choices = available_domains()

    if requested:
        if requested in choices:
            return requested
        exception_message(
            f"No such domain: {requested!r}.\n"
            f"Expected one of: {', '.join(choices) or '(none found)'}\n"
            f"Looked in {os.path.join(SKILLS_PATH, 'use_cases')}"
        )

    if not choices:
        console.print("[yellow]No domains under skills/use_cases/ -- running "
                      "without a domain skill.[/yellow]")
        return ""

    console.print("\n[bold]Available domains:[/bold]")
    for i, name in enumerate(choices, 1):
        console.print(f"  {i}. {name}")
    console.print(f"  {len(choices) + 1}. (none -- run without a domain skill)")

    while True:
        raw = input("\nSelect a domain by number: ").strip()
        try:
            idx = int(raw)
        except ValueError:
            console.print("[red]Invalid selection. Please input a number[/red]")
            continue
        if 1 <= idx <= len(choices):
            console.print(f"[dim]Selected: {choices[idx - 1]}[/dim]")
            return choices[idx - 1]
        if idx == len(choices) + 1:
            console.print("[dim]Running without a domain skill.[/dim]")
            return ""
        console.print("[red]Invalid selection. Choose a listed domain[/red]")


def make_run_dir(work_dir: Path) -> Path:
    """Prepare work/run0 for this run and return it. Nothing outside it is touched.

    This used to `shutil.rmtree(work_dir)` outright. The intent was narrow --
    discard the previous run's submit scripts and block logs so only the
    current run's are there to look at -- but work/ holds far more than that:
    work/run0 is where the Explorer writes workflow.py and every output the
    run produces, and run_archiver's own docstring explains at length why it
    deliberately does NOT clear work/run0. The two contradicted each other,
    and anything you renamed to keep (work/run0_hacc_good) was deleted too.

    Everything this run produces now lives under work/run0 -- the Explorer's
    outputs, the generated workflow.py, and Parsl's scratch at
    work/run0/runinfo. That makes work/run0 the one directory a run writes to,
    so anything you rename to keep it (work/run0_hacc_good) sits beside it and
    is never touched. It also matches where the MCP servers already point:
    DEFAULT_WORK_DIR is REPO_ROOT/work/run0 and the skills say /app/work/run0,
    whereas Parsl's scratch used to land one level up in work/runinfo.

    Within run0 only the scratch is cleared, since nothing in it is run output
    and Parsl recreates it. The rest is overwritten file-by-file as the run
    writes it.

    MAW_ROTATE_RUN0=1 moves a non-empty work/run0 aside to
    work/run0_prev_<timestamp> so this run starts clean instead of writing
    over it -- worth considering as a default, because a stale output left in
    place can make a @bash_app that ends on `test -s <output>` report success
    without having run.

    MAW_WIPE_WORK=1 restores the old behaviour of clearing all of work/.
    """
    work_dir.mkdir(parents=True, exist_ok=True)

    if os.getenv("MAW_WIPE_WORK") == "1":
        shutil.rmtree(work_dir, ignore_errors=True)

    run0 = work_dir / "run0"
    if os.getenv("MAW_ROTATE_RUN0") == "1" and run0.is_dir() and any(run0.iterdir()):
        kept = work_dir / f"run0_prev_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        run0.rename(kept)
        console.print(f"[dim]Previous outputs moved to {kept}[/dim]")

    run0.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(run0 / "runinfo", ignore_errors=True)
    return run0


def venv_dir() -> Path:
    """The virtualenv the PBS workers should activate.

    Not a constant and not a search: it is the venv this process is
    already running in. You activate it before launching, so sys.prefix
    is exactly the environment that has parsl and academy installed --
    which is necessarily the right one, since this code could not have
    imported them otherwise.

    sys.prefix differs from sys.base_prefix precisely when the
    interpreter is inside a venv, so that comparison is the whole test.

    Raising here is deliberate. `source` on a wrong path fails inside the
    PBS submit script, where the only symptom is `process_worker_pool.py:
    command not found` and a job marked MISSING a minute later. Failing
    at startup costs nothing and says what is actually wrong.
    """
    if sys.prefix == sys.base_prefix:
        raise RuntimeError(
            "Not running inside a virtualenv, so the PBS workers have no "
            "environment to activate. Activate the project venv first:\n"
            f"    source {PROJECT_DIR}/venv3/bin/activate"
        )

    prefix = Path(sys.prefix)
    activate = prefix / "bin" / "activate"
    if not activate.is_file():
        raise RuntimeError(
            f"{activate} does not exist, so the workers cannot activate "
            f"this environment. If it is a conda env rather than a venv, "
            f"the submit script needs a `conda activate` prelude instead."
        )
    return prefix


def make_worker_init(run_dir: Path, engine: str = "parsl") -> str:
    """Shell prelude for the Explorer's worker pool, run before the launcher.

    It sources setup_hpc.sh, the same script you source before running this,
    so the Explorer's node has the same MPI library paths and VENV_PYTHON as
    an --env hpc run. That node never saw your shell, so nothing you exported
    there reaches it any other way. Sourcing is safe here only because the
    script sets no shell options: a `set -u` left behind would abort this
    submit script on the unset $PYTHONPATH two commands later.

    PROJECT_DIR goes on PYTHONPATH as well as SRC_DIR: the repo root is
    only on sys.path automatically for the launching process, because
    Python adds the main script's directory. On a worker the main script
    is Parsl's process_worker_pool.py, so without this the root-level
    modules (trace_logger, mcp_explorer, run_archiver) are not importable
    and the Explorer fails to unpickle before any of our code runs.
    """
    # os.path.isfile, not SETUP_SCRIPT_PATH.is_file(): consts.py builds every
    # path with an f-string, so this is a str and has no .is_file(). Calling it
    # raised AttributeError here -- inside make_worker_init, which only runs
    # under --env multi-node, which is why it went unnoticed.
    if not os.path.isfile(SETUP_SCRIPT_PATH):
        raise RuntimeError(
            f"{SETUP_SCRIPT_PATH} not found. The multi-node Explorer sources it on "
            f"its compute node for the Intel MPI library paths and VENV_PYTHON."
        )
    prelude = [
        f"module load {HPC_MODULES}",
        f"source {venv_dir()}/bin/activate",
        f"source {SETUP_SCRIPT_PATH}",
        f"cd {run_dir}",
        f"export PYTHONPATH={PROJECT_DIR}:{SRC_DIR}:$PYTHONPATH",
        # Where the MCP server writes and runs. Exported here rather than left
        # to setup_hpc.sh because run_dir is a per-run decision made in THIS
        # process (work/run0, possibly rotated aside first) -- the setup script
        # cannot know it, and the Explorer's node never saw the shell that did.
        # Everything else the servers need (JAVA_HOME, MAW_MPI_LAUNCHER, the
        # ADIOS2 and MPI library paths, the headless rendering variables) is
        # site knowledge and belongs in that script. PyCOMPSs needs nothing
        # beyond the activated venv it was pip-installed into, which the
        # `source .../activate` two lines up provides.
        f"export MAW_REPO_ROOT={PROJECT_DIR}",
        f"export MAW_WORK_DIR={run_dir}",
        f"export MAW_DATA_DIR={PROJECT_DIR}/data",
    ]

    # No engine-specific tail, and `engine` is accepted only so the call sites
    # need not care. There used to be one branch per engine here, each exporting
    # a library path from a constant in common.consts; those constants are gone,
    # and setup_hpc.sh -- sourced three lines up, on this node -- is now the
    # single place the environment for any engine is built. One script that
    # knows the machine beats three branches that each guessed at it.
    return "; ".join(prelude)


def user_input():
    parser = argparse.ArgumentParser(description="MAW -- Multi-Agent Workflow (Academy Approach)")
    parser.add_argument("--paper", type=int, help="Path to the PDF paper or paper index (1-based)")
    parser.add_argument("--image", type=str, help="Path to image file (diagram/figure) to use for planning")
    parser.add_argument("--goal", type=str, help="Goal for the workflow")
    parser.add_argument("--engine", type=str, default="parsl",
                        choices=["parsl", "pycompss", "adios"],
                        help="Workflow engine to use (default: parsl)")
    parser.add_argument("--env", type=str, default="local",
                        choices=["local", "hpc", "multi-node"],
                        help="local: this machine. hpc: inside a PBS job you already "
                             "hold (qsub -I), everything in one process. multi-node: "
                             "submit a separate PBS job for the Explorer. On LCRC, "
                             "source setup_hpc.sh first")
    parser.add_argument("--condition", type=str, default="B",
                        choices=["A", "B", "C"],
                        help="Ablation condition: A=no-skills, B=full (default), C=single-agent")
    parser.add_argument("--trial", type=int, default=1,
                        help="Trial number for repeated (paper, condition) runs (default: 1)")
    parser.add_argument("--domain", type=str, default="",
                        help="The domain: a folder name under skills/use_cases/, by "
                             "exact name (e.g. new_molecular_nucleation). It both "
                             "selects the domain skill and labels the run trace. Omit "
                             "to be asked; --list-domains shows what is there")
    parser.add_argument("--list-domains", action="store_true",
                        help="Print the available domains and exit")
    parser.add_argument("--combination", type=str, required=True,
                        choices=["a", "b", "c", "d"],
                        help="Planner input combination: a=PDF+Image+DataFile+Desc, "
                             "b=PDF+DataFile+Desc, c=Image+DataFile+Desc, d=DataFile+Desc")
    parser.add_argument("--nodes", type=int, default=0,
                        help="multi-node only: nodes the Explorer's MPI application "
                             "runs across. The allocation is exactly this wide")
    parser.add_argument("--ranks", type=int, default=0,
                        help="multi-node only: MPI ranks per node (mpiprocs). This is "
                             "the cap on ranks the agent can place on any one node, so "
                             "--nodes x --ranks is the rank ceiling for the run. No core "
                             "count is requested -- PBS applies its own per-chunk default")
    args = parser.parse_args()

    if args.list_domains:
        names = available_domains()
        console.print("\n[bold]Available domains:[/bold]")
        for name in names:
            console.print(f"  {name}")
        if not names:
            console.print(f"  (none found under {os.path.join(SKILLS_PATH, 'use_cases')})")
        raise SystemExit(0)

    # Fail here rather than after the prompts, or as a qsub for select=0.
    if args.env == "multi-node" and (args.nodes < 1 or args.ranks < 1):
        parser.error("--env multi-node needs --nodes and --ranks, both at least 1")
    if args.env == "multi-node" and not os.getenv("PBS_ACCOUNT"):
        parser.error("--env multi-node needs PBS_ACCOUNT set (in .env or the environment)")
    return args


def collect_inputs(args) -> UserInput:
    """Resolve paper / image / data-file / goal selections (interactive fallbacks)."""

    inputs = UserInput()

    # Which domain skill this run loads. Resolved first: an unknown name is a
    # typo worth reporting before the user has picked a paper and typed a goal.
    inputs.domain = resolve_domain(args.domain)

    # Paper selection
    if args.combination == "a" or args.combination == "b":
        pdf_list = []

        pdfs_path = os.listdir(LITERATURE_PATH)
        if not pdfs_path:
            exception_message("No PDFs found in the Literature/ folder. Add a paper and try again.")

        console.print("\n[bold]Available papers:[/bold]")
        for pdf in pdfs_path:
            if Path(pdf).suffix.lower() == ".pdf":
                pdf_list.append(pdf)
                console.print(f"  {len(pdf_list)}. {pdf}")

        while args.paper is None or args.paper < 1 or args.paper > len(pdf_list):
            choice = input("\nSelect a paper by number: ").strip()
            try:
                idx = int(choice)
                if 1 <= idx <= len(pdf_list):
                    args.paper = idx
                else:
                    console.print("[red]Invalid selection. Choose a proper paper[/red]")
            except ValueError:
                console.print("[red]Invalid selection. Please input a number[/red]")

        inputs.pdf = os.path.join(LITERATURE_PATH, pdf_list[args.paper - 1])
        console.print(f"[dim]Selected: {pdf_list[args.paper - 1]}[/dim]")

    # __ Image selection __
    if args.combination == "a" or args.combination == "c":
        _IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
        os.makedirs(IMAGE_PATH, exist_ok=True)

        # similar to paper selection except you can give a path instead of a number
        if args.image:
            image_path = args.image if os.path.isabs(args.image) else os.path.join(IMAGE_PATH, args.image)
            if not os.path.isfile(image_path) or Path(image_path).suffix.lower() not in _IMAGE_EXTS:
                exception_message(f"Image not found: {args.image}")
            console.print(f"[dim]Image: {os.path.basename(image_path)}[/dim]")
            inputs.image = image_path

        else:
            images_list = []

            image_dir_entries = os.listdir(IMAGE_PATH)
            if not image_dir_entries:
                exception_message("No Images found in the images/ folder. Add an image and try again.")

            console.print("\n[bold]Available Images:[/bold]")
            for image in image_dir_entries:
                if Path(image).suffix.lower() in _IMAGE_EXTS:
                    images_list.append(image)
                    console.print(f"{len(images_list)}. {image}")

            if not images_list:
                exception_message("No Images found in the images/ folder. Add an image and try again.")

            while inputs.image == "":
                choice = input("\nSelect an image by number: ").strip()
                try:
                    idx = int(choice)
                    if 1 <= idx <= len(images_list):
                        inputs.image = os.path.join(IMAGE_PATH, images_list[idx - 1])
                    else:
                        console.print("[red]Invalid selection. Choose a proper image[/red]")
                except ValueError:
                    console.print("[red]Invalid selection. Please input a number[/red]")

            console.print(f"[dim]Selected: {os.path.basename(inputs.image)}[/dim]")

    # __ Data file selection __
    data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
    all_data_files = sorted(f for f in os.listdir(data_dir)
                            if os.path.isfile(os.path.join(data_dir, f))) if os.path.isdir(data_dir) else []

    if not all_data_files:
        console.print("[yellow]No files found in data/ -- agents will have no input data.[/yellow]")
        selected_data_files = []
    else:
        console.print("\n[bold]Available data files:[/bold]")
        for i, name in enumerate(all_data_files, 1):
            console.print(f"  {i}. {name}")
        raw = input("\nSelect files by number (comma-separated, or Enter for all): ").strip()
        if not raw:
            selected_data_files = all_data_files
            console.print("[dim]Using all data files.[/dim]")
        else:
            selected_data_files = []
            for token in raw.split(","):
                token = token.strip()
                try:
                    selected_data_files.append(all_data_files[int(token) - 1])
                except (ValueError, IndexError):
                    console.print(f"[yellow]Skipping invalid selection: {token!r}[/yellow]")
            if not selected_data_files:
                console.print("[yellow]No valid files selected -- using all.[/yellow]")
                selected_data_files = all_data_files
            else:
                console.print(f"[dim]Selected: {', '.join(selected_data_files)}[/dim]")

    inputs.data_files = selected_data_files

    # __ Goal __
    goal = args.goal or input("\nDescribe your goal for this workflow: ").strip()
    if not goal:
        exception_message("[red]Goal cannot be empty.[/red]")
    inputs.goal = goal

    inputs.nodes = args.nodes
    inputs.ranks = args.ranks
    # Needed by make_worker_init: only pycompss gets an engine prelude on the
    # Explorer's node.
    inputs.engine = args.engine

    return inputs


# __ Parsl configuration (multi-node only) ______________________________________

def make_config(nodes: int, ranks: int, run_dir: Path, engine: str = "parsl") -> Config:
    """Build the config: one block, for the Explorer.

    Renders `select=<nodes+1>:ncpus=<ranks>:mpiprocs=<ranks>`, i.e. (nodes+1) x
    ranks lines in $PBS_NODEFILE, with one core reserved per rank slot. ncpus
    goes in via cpus_per_node and mpiprocs via select_options -- see the note
    on the provider below for why they cannot both go in the same place.

    `run_dir` is work/run0, so Parsl's scratch goes to work/run0/runinfo and
    everything this run produces is under one directory. That is also where the
    MCP server writes -- MAW_WORK_DIR is exported to the same path by
    make_worker_init -- so no worker's cwd can affect it.
    """
    return Config(
        # Parsl's own scratch (block logs, submit scripts, interchange
        # logs) is pinned here rather than left to default to ./runinfo in
        # whatever directory this was launched from. It must stay OUT of
        # work/run0: the Explorer walks work/ to build the artifact manifest
        # and the run digest, and Parsl's log tree would bury the real
        # outputs in both.
        run_dir=str(run_dir / "runinfo"),
        executors=[
            # ---------------------------------------------------------------
            # Explorer: one worker on the head node of an allocation
            # (nodes + 1) wide, every node offering `ranks` rank slots.
            #
            # One core per rank slot, both sides derived from the single
            # --ranks value: cpus_per_node sets ncpus, select_options sets
            # mpiprocs.
            #
            # They go in through DIFFERENT parameters because PBSProProvider's
            # template already emits ncpus itself:
            #
            #     #PBS -l select=${nodes_per_block}:ncpus=${ncpus}${select_options}
            #
            # so ncpus comes from cpus_per_node and select_options is appended
            # after it. Putting "ncpus=" in select_options as well produces a
            # second one in the same chunk and qsub rejects the whole job with
            # "duplicated resource within a section of a select specification".
            #
            # Neither value can be left out. cpus_per_node DEFAULTS TO 1, which
            # is where this came from on Improv:
            #
            #     Resource_List.select = 3:ncpus=1:mpiprocs=4
            #     Resource_List.ncpus  = 3
            #
            # 12 rank slots in $PBS_NODEFILE and 3 cores to run them on. A
            # workflow's `mpiexec -np 8 --bind-to core` then asks for 4 cores
            # on nodes that hold 1 -- it either fails outright or
            # oversubscribes 4:1 (observed as cpupercent 353 against a walltime
            # of 13 minutes and cput of 25).
            #
            # Deriving both from --ranks keeps the one flag the caller can
            # actually answer ("how many ranks per node"). An earlier version
            # exposed --ncpus separately, and it served as the core reservation
            # AND the rank cap at once, so sizing it for either got the other
            # wrong -- 32 cores reserved to run 8 ranks.
            #
            # Threads per rank are deliberately not expressible: that would
            # need cpus_per_node = ranks x threads plus ompthreads, and nothing
            # in the workflow asks for them yet.
            #
            # max_workers_per_node=1 because exactly one thing runs in this
            # block: the Explorer. The ranks are not Parsl workers -- they are
            # launched by mpirun from inside the generated workflow, in the
            # allocation this block holds.
            # ---------------------------------------------------------------
            HighThroughputExecutor(
                label="Explorer",
                max_workers_per_node=1,
                provider=PBSProProvider(
                    account=os.getenv("PBS_ACCOUNT"),
                    worker_init=make_worker_init(run_dir, engine),
                    launcher=SingleNodeLauncher(),
                    nodes_per_block=nodes,
                    cpus_per_node=ranks,
                    select_options=f"mpiprocs={ranks}",
                    walltime=DEFAULT_WALLTIME,
                    # Fixed topology: no scaling in or out.
                    init_blocks=1,
                    min_blocks=1,
                    max_blocks=1,
                ),
            ),
        ],
    )


def make_state(args, inputs) -> AgentState:
    multi_node = args.env == "multi-node"
    return AgentState(
        messages=[],
        goal=inputs.goal,
        pdf_path=inputs.pdf,
        literature_findings=[],
        stack_decision=[],
        tasks=[],
        exploration_log=[],
        selected_data_files=inputs.data_files,
        requirements_content="",
        requirements_approved=False,
        image_path=inputs.image,
        current_step=AgentSteps.START,
        orchestrator_feedback="",
        next="",
        planner_revisions=0,
        installer_revisions=0,
        explorer_revisions=0,
        engine=args.engine,
        # The agents see "hpc" for multi-node too. Both modes are LCRC under
        # PBS, so the environment knowledge is the same, and the Planner and
        # Orchestrator look it up by this key -- an unknown value falls back
        # to knowledge/local, which would tell them there is no PBS and no
        # multi-node MPI. The run metadata still records the real --env.
        env="hpc" if multi_node else args.env,
        condition=args.condition,
        # The folder name IS the domain -- one flag, one field. It was briefly
        # two (--domain as a free-text label, --use-case as the folder), which
        # meant the trace could record a domain that matched no folder and the
        # skill lookup could disagree with the label. run_archiver already read
        # metadata.domain as the use case (`usecase = metadata.domain or ...`),
        # so this is the name that was always meant to carry it.
        domain=inputs.domain,
        explorer_summary="",
        run_succeeded=True,
        reported_issue="",
        skill_recommendations=[],
        skill_edits_applied=False,
        # Capacity of the allocation this script submits; only multi-node has
        # one. Under hpc the Explorer reads the allocation you hold from PBS
        # itself via get_resources, so these stay 0 and are not shown to it.
        mpi_nodes=inputs.nodes if multi_node else 0,
        mpi_ranks_per_node=inputs.ranks if multi_node else 0,
        mpi_slots=inputs.nodes * inputs.ranks if multi_node else 0,
    )


def make_summary(run_log_path, final_state):
    summary = tracer.get_summary()

    verified = getattr(tracer.run_metadata, "user_verified", None)
    if verified is False:
        console.print("\n[bold yellow]User reported the workflow did not execute properly.[/bold yellow]")
        console.print(f"[dim]Issue: {tracer.run_metadata.reported_issue}[/dim]")
        recs_path = os.path.splitext(run_log_path)[0] + "_skill_recommendations.md"
        if os.path.isfile(recs_path):
            console.print(f"[dim]Skill recommendations: {recs_path}[/dim]")
        if final_state.get("skill_edits_applied"):
            console.print("[dim]Suggested skill edits were applied to the skill file(s).[/dim]")
    elif verified:
        console.print("\n[bold green]User confirmed the workflow executed properly.[/bold green]")

    console.print("\n[bold]Trace Summary:[/bold]")
    console.print(f"  Agents: {', '.join(summary['agents_involved'])}")
    console.print(f"  Routing path: {' -> '.join(summary['routing_path'])}")
    console.print(f"  Tool calls: {summary['tool_calls']} ({summary['tool_successes']} succeeded)")
    console.print(f"  Total time: {summary['total_duration_s']}s")

    total_in = summary.get('total_input_tokens', 0)
    total_out = summary.get('total_output_tokens', 0)
    if total_in or total_out:
        console.print("\n[bold]Token Usage:[/bold]")
        console.print(f"  Input tokens:  {total_in:,}")
        console.print(f"  Output tokens: {total_out:,}")
        console.print(f"  Total tokens:  {total_in + total_out:,}")
        tokens_by_agent = summary.get('tokens_by_agent', {})
        if tokens_by_agent:
            console.print("  By agent:")
            for agent, count in sorted(tokens_by_agent.items()):
                console.print(f"    {agent}: {count:,}")


# __ Academy workflow __________________________________________________________

async def follow(path: str, prefix: str, poll_s: float = 0.5) -> None:
    """Print lines appended to `path` until cancelled, then flush the rest.

    This is how the multi-node Explorer's output reaches this terminal: it
    runs on another node, writes its log on the shared filesystem, and this
    reads it as it grows. The file need not exist yet -- it appears when the
    Explorer's PBS job starts.

    Writes straight to sys.__stdout__ with plain text rather than through
    rich: log lines can contain [brackets] that rich would take as markup,
    and this must never write into a redirected sys.stdout.
    """
    out = sys.__stdout__
    pos, partial = 0, ""

    def drain() -> None:
        nonlocal pos, partial
        try:
            with open(path) as f:
                f.seek(pos)
                chunk = f.read()
                pos = f.tell()
        except FileNotFoundError:
            return
        if not chunk:
            return
        lines = (partial + chunk).split("\n")
        partial = lines.pop()          # keep an unfinished last line for later
        for line in lines:
            out.write(f"{prefix} {line}\n")
        out.flush()

    try:
        while True:
            drain()
            await asyncio.sleep(poll_s)
    except asyncio.CancelledError:
        drain()                        # whatever was written before the stop
        if partial:
            out.write(f"{prefix} {partial}\n")
            out.flush()
        raise


async def run_workflow(args, inputs, run_log_path: str) -> dict:
    initial_state = make_state(args, inputs)

    if args.env == "multi-node":
        return await run_multi_node(inputs, initial_state, run_log_path)

    # local and hpc: every agent in this process's event loop, no DFK, no
    # job submitted. LocalExchangeFactory is correct precisely because
    # nothing leaves the process. The Explorer prints straight to this
    # terminal and shares this tracer, so it needs neither a log file nor a
    # trace file. Under hpc this process is already on a compute node
    # inside your qsub -I allocation, and the Explorer sees it through PBS.
    async with await Manager.from_exchange_factory(
        factory=LocalExchangeFactory(),
        executors=None,
    ) as manager:
        explorer = await manager.launch(
            Explorer,
            name="explorer",
            executor="event_loop",
        )
        return await run_agents(manager, explorer, initial_state, run_log_path)


async def run_multi_node(inputs, initial_state, run_log_path: str) -> dict:
    """Run with the Explorer on its own PBS allocation; the rest stay here.

    The Explorer is in another process on another node, so its terminal
    and its tracer are not this one's: its output goes to a log file and
    its events to a JSONL trace, both beside the run log on the shared
    filesystem. follow() prints the log here as it grows, so the run still
    reads as one terminal.
    """
    base = os.path.splitext(run_log_path)[0]
    explorer_log = base + "_explorer.log"
    explorer_trace = base + "_explorer_trace.jsonl"

    # make_run_dir returns work/run0 -- the whole run lives there now.
    run_dir = make_run_dir(Path(WORK_DIR))
    console.print(f"[dim]Project dir:    {PROJECT_DIR}[/dim]")
    console.print(f"[dim]Venv:           {venv_dir()}[/dim]")
    console.print(f"[dim]Run dir:        {run_dir}[/dim]")
    console.print(f"[dim]Parsl scratch:  {run_dir / 'runinfo'}[/dim]")
    console.print(f"[dim]Explorer log:   {explorer_log}[/dim]")
    console.print(f"[dim]Explorer trace: {explorer_trace}[/dim]")
    console.print(
        f"[dim]Explorer allocation: {inputs.nodes} nodes "
        f"x mpiprocs={inputs.ranks} -> up to "
        f"{inputs.nodes * inputs.ranks} ranks across {inputs.nodes} nodes[/dim]"
    )

    dfk = parsl.load(make_config(inputs.nodes, inputs.ranks, run_dir, inputs.engine))
    follower = asyncio.create_task(follow(explorer_log, "[explorer]"))
    # HttpExchangeFactory, not LocalExchangeFactory: the local exchange only
    # passes messages within one process, and the Explorer is on another node.
    #
    # No argument means Academy's default hosted exchange, which is what this
    # runs against. ACADEMY_EXCHANGE_URL only exists to point at a different
    # one (a self-hosted exchange, say) without editing this file; unset, the
    # call is identical to HttpExchangeFactory(). Printed either way so the
    # run log records which exchange the agents actually used.
    exchange_url = os.getenv("ACADEMY_EXCHANGE_URL", "")
    factory = HttpExchangeFactory(exchange_url) if exchange_url else HttpExchangeFactory()
    console.print(f"[dim]Exchange:       {exchange_url or 'Academy default'}[/dim]")

    try:
        async with await Manager.from_exchange_factory(
            factory=factory,
            executors={
                "explorer": ParslPoolExecutor(dfk=dfk, executors=["Explorer"]),
            },
        ) as manager:
            # Returns once the agent is submitted, not once it is running:
            # the PBS job queues while the Planner and Installer work, and
            # the first explorer() call is what waits for it.
            explorer = await manager.launch(
                Explorer,
                name="explorer",
                executor="explorer",
                kwargs={"log_path": explorer_log, "trace_path": explorer_trace},
            )
            return await run_agents(manager, explorer, initial_state, run_log_path)
    finally:
        follower.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await follower
        dfk.cleanup()


async def run_agents(manager, explorer, initial_state, run_log_path) -> dict:
    """Launch the Orchestrator, Planner and Installer, then run the graph.

    All three are Academy agents in this process's event loop. `explorer`
    is a handle to the Explorer agent, already launched by the caller on
    whichever executor this --env calls for.
    """
    console.print("[dim cyan][academy] launching worker agents...[/dim cyan]")
    planner = await manager.launch(Planner, name="planner", executor="event_loop")
    installer = await manager.launch(Installer, name="installer", executor="event_loop")

    console.print("[dim cyan][academy] launching orchestrator...[/dim cyan]")
    orchestrator = await manager.launch(
        Orchestrator,
        kwargs={
            "model": os.getenv("MODEL_NAME"),
            "base_url": os.getenv("OPENAI_BASE_URL"),
            "api_key": os.getenv("OPENAI_API_KEY"),
            "agents": {
                "planner": planner,
                "installer": installer,
                "explorer": explorer,
            },
            "run_log": run_log_path,
            "agent_state": initial_state,
        },
        executor="event_loop",
        name="orchestrator",
    )

    console.print("[dim cyan][academy] workflow running -- waiting for completion...[/dim cyan]")
    final_state = await orchestrator.agentic_workflow()
    console.print("[dim cyan][academy] orchestrator finished.[/dim cyan]")

    # The explorer's end-of-run verification runs here, once, against the
    # finished state -- not inside the explorer, which the orchestrator can
    # route back to several times.
    review = await post_run_review(
        {**(final_state or {}), "run_log": run_log_path}
    )
    return {**(final_state or {}), **review}


# __ Run _______________________________________________________________________

def _merge_sidecar(trace_path: str) -> None:
    """Fold the multi-node Explorer's side-car trace into this run's tracer.

    Called on both the success and failure paths, and before tracer.save() and
    archive_run() -- so the saved trace.json, the archived copy, and the
    summary printed at the end all include the Explorer, rather than reporting
    `Tool calls: 0` and omitting it from `agents_involved` because its events
    were logged by a different process's tracer.

    A no-op under --env local and --env hpc, where the Explorer shares this
    process's tracer and writes no side-car.
    """
    added = merge_into_tracer(tracer, trace_path)
    if added:
        console.print(f"[dim]Merged {added} explorer event(s) from side-car trace[/dim]")


def main():
    args = user_input()

    console.print(Panel(
        f"[bold blue]MAW -- Multi-Agent Workflow (Academy Approach)[/bold blue]\n"
        f"[dim]Engine: {args.engine} | Env: {args.env}[/dim]",
        border_style="blue",
    ))

    inputs = collect_inputs(args)

    os.makedirs(RUN_PATH, exist_ok=True)
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_log_path = os.path.join(RUN_PATH, run_id + ".jsonl")
    trace_path = os.path.join(RUN_PATH, run_id + "_trace.json")
    console.print(f"[dim]Run log: {run_log_path}[/dim]")

    tracer.reset()  # clear any previous run from the dashboard before starting
    tracer.start_run(
        run_id=run_id,
        condition=args.condition,
        combination=args.combination,
        trial=args.trial,
        paper_id=_slugify(inputs.pdf) if inputs.pdf else "no_paper",
        paper_path=inputs.pdf,
        domain=inputs.domain,
        framework=args.engine,
        env=args.env,
        goal=inputs.goal,
        model=os.getenv("MODEL_NAME", "claudeopus48"),
    )

    final_state = {}
    try:
        final_state = asyncio.run(run_workflow(args, inputs, run_log_path)) or {}
        # The explorer's end-of-run verification is what says whether the
        # result was usable; the graph completing only means the agents
        # stopped.
        verified = getattr(tracer.run_metadata, "user_verified", None)
        tracer.finalize_run("completed" if verified is not False else "failed")
    except Exception as e:
        import traceback as _traceback
        tracer.log_run_error(type(e).__name__, str(e), _traceback.format_exc())
        tracer.finalize_run("failed")
        _merge_sidecar(trace_path)
        tracer.save(trace_path)
        console.print(f"[red]Run failed: {e}[/red]")
        console.print(f"[dim]Trace saved (partial): {trace_path}[/dim]")
        archive_path = archive_run(tracer.run_metadata, trace_path)
        if archive_path:
            console.print(f"[dim]Run archived (failed): {archive_path}[/dim]")
        raise

    _merge_sidecar(trace_path)
    tracer.save(trace_path)
    console.print(f"[dim]Trace saved: {trace_path}[/dim]")

    archive_path = archive_run(tracer.run_metadata, trace_path)
    if archive_path:
        console.print(f"[dim]Run archived: {archive_path}[/dim]")

    # __ Summary __
    make_summary(run_log_path, final_state)


if __name__ == "__main__":
    main()