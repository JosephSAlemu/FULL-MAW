"""
Academy Approach -- Multi-Agent Workflow (Academy Runtime)

Same pipeline as agent_mcp.py, but instead of running the LangGraph directly in
one process, each agent (planner / installer / explorer / orchestrator) runs as
an Academy agent behind an exchange, coordinated by an Academy Manager.

Pipeline:
    orchestrator -> planner -> installer -> explorer -> end

The Orchestrator owns the LangGraph (see Orchestrator.agentic_workflow in
src/agents.py). Launching it starts the workflow; when the graph completes the
orchestrator loop calls agent_shutdown(), which unblocks Manager.wait().

Usage:
    python academy_agents.py --paper 1 --combination b --goal "Reproduce this workflow..."
    python academy_agents.py --paper YildizO_RAPIDS.pdf --combination a --goal "..."

--combination is required: a=PDF+Image+Desc, b=PDF+Desc, c=Image+Desc, d=Desc Only
"""

import argparse
import asyncio
import os
import re
import sys
import warnings
from datetime import datetime

warnings.filterwarnings("ignore", module="pypdf")

# src/ holds the Academy agent definitions (agents.py, states.py, utility.py) and
# is imported as top-level modules, so it must be on the path.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel

from academy.exchange import LocalExchangeFactory
from academy.manager import Manager

from common.consts import LITERATURE_PATH, RUN_PATH
from common.exceptions import exception_message
from trace_logger import tracer
from run_archiver import archive_run

from agents import Planner, Installer, Explorer, Orchestrator

load_dotenv()

console = Console()


# __ Helpers ___________________________________________________________________

def _slugify(path: str) -> str:
    """Turn a file path into a stable run-metadata id, e.g. for paper_id."""
    base = os.path.splitext(os.path.basename(path))[0] if path else "no_paper"
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "_", base).strip("_")
    return slug or "no_paper"


def user_input():
    parser = argparse.ArgumentParser(description="MAW -- Multi-Agent Workflow (Academy Approach)")
    parser.add_argument("--paper", type=int, help="Path to the PDF paper or paper index (1-based)")
    parser.add_argument("--image", type=str, help="Path to image file (diagram/figure) to use for planning")
    parser.add_argument("--goal", type=str, help="Goal for the workflow")
    parser.add_argument("--engine", type=str, default="parsl",
                        choices=["parsl", "pycompss", "adios"],
                        help="Workflow engine to use (default: parsl)")
    parser.add_argument("--env", type=str, default="local",
                        choices=["local", "hpc"],
                        help="Execution environment: local (default) or hpc (LCRC/PBS)")
    parser.add_argument("--condition", type=str, default="B",
                        choices=["A", "B", "C"],
                        help="Ablation condition: A=no-skills, B=full (default), C=single-agent")
    parser.add_argument("--trial", type=int, default=1,
                        help="Trial number for repeated (paper, condition) runs (default: 1)")
    parser.add_argument("--domain", type=str, default="",
                        help="Paper domain label, e.g. molecular_nucleation (optional)")
    parser.add_argument("--combination", type=str, required=True,
                        choices=["a", "b", "c", "d"],
                        help="Planner input combination: a=PDF+Image+Desc, "
                             "b=PDF+Desc, c=Image+Desc, d=Desc Only")
    return parser.parse_args()


def collect_inputs(args):
    """Resolve paper / image / data-file / goal selections (interactive fallbacks)."""
    pdfs = [os.path.join(LITERATURE_PATH, pdf) for pdf in os.listdir(LITERATURE_PATH)
            if pdf.lower().endswith(".pdf")]
    if not pdfs:
        exception_message("No PDFs found in the Literature/ folder. Add a paper and try again.")

    console.print("\n[bold]Available papers:[/bold]")
    for i, name in enumerate(pdfs, 1):
        console.print(f"  {i}. {os.path.basename(name)}")

    paper = None
    if args.paper:
        try:
            idx = int(args.paper)
            if 1 <= idx <= len(pdfs):
                paper = idx - 1
        except ValueError:
            match = next((i for i, p in enumerate(pdfs)
                          if os.path.basename(p) == args.paper or p == args.paper), None)
            if match is not None:
                paper = match

    while paper is None:
        choice = input("\nSelect a paper by number: ").strip()
        try:
            idx = int(choice)
            if 1 <= idx <= len(pdfs):
                paper = idx - 1
                break
        except ValueError:
            pass
        console.print("[red]Invalid selection. Choose a proper paper[/red]")
    pdf_path = pdfs[paper]
    console.print(f"[dim]Selected: {os.path.basename(pdf_path)}[/dim]")

    # __ Image selection __
    _IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
    images_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "images")
    os.makedirs(images_dir, exist_ok=True)

    if args.image:
        image_path = args.image if os.path.isabs(args.image) else os.path.join(images_dir, args.image)
        if not os.path.isfile(image_path):
            console.print(f"[red]Image not found: {args.image}[/red]")
            raise SystemExit(1)
        console.print(f"[dim]Image: {os.path.basename(image_path)}[/dim]")
    else:
        images = sorted(f for f in os.listdir(images_dir)
                        if os.path.splitext(f)[1].lower() in _IMAGE_EXTS)
        if not images:
            image_path = ""
        else:
            console.print("\n[bold]Available images:[/bold]")
            for i, name in enumerate(images, 1):
                console.print(f"  {i}. {name}")
            console.print("  0. Skip -- no image")
            choice = input("\nSelect an image by number (or 0 to skip): ").strip()
            if choice == "0" or not choice:
                image_path = ""
                console.print("[dim]No image selected.[/dim]")
            else:
                try:
                    image_path = os.path.join(images_dir, images[int(choice) - 1])
                    console.print(f"[dim]Selected: {os.path.basename(image_path)}[/dim]")
                except (ValueError, IndexError):
                    console.print("[red]Invalid selection -- no image will be used.[/red]")
                    image_path = ""

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

    # __ Goal __
    goal = args.goal or input("\nDescribe your goal for this workflow: ").strip()
    if not goal:
        console.print("[red]Goal cannot be empty.[/red]")
        raise SystemExit(1)

    return pdf_path, image_path, selected_data_files, goal


# __ Academy workflow __________________________________________________________

async def run_workflow(args, pdf_path, image_path, selected_data_files, goal, run_log_path):
    initial_state = {
        "messages":              [],
        "goal":                  goal,
        "pdf_path":              pdf_path,
        "literature_findings":   [],
        "stack_decision":        [],
        "tasks":                 [],
        "exploration_log":       [],
        "selected_data_files":   selected_data_files,
        "requirements_content":  "",
        "requirements_approved": False,
        "image_path":            image_path,
        "current_step":          "start",
        "orchestrator_feedback": "",
        "next":                  "",
        "planner_revisions":     0,
        "installer_revisions":   0,
        "explorer_revisions":    0,
        "engine":                args.engine,
        "env":                   args.env,
        "condition":             args.condition,
        "domain":                args.domain,
    }

    async with await Manager.from_exchange_factory(
        factory=LocalExchangeFactory(),
        executors=None,
    ) as manager:
        # Launch the three worker agents first so the orchestrator can hand them
        # the shared LLM in its agent_on_startup hook.
        console.print("[dim cyan][academy] launching worker agents...[/dim cyan]")
        planner   = await manager.launch(Planner,   name="planner")
        installer = await manager.launch(Installer, name="installer")
        explorer  = await manager.launch(Explorer,  name="explorer")

        agents = {
            "planner":   planner,
            "installer": installer,
            "explorer":  explorer,
        }

        console.print("[dim cyan][academy] launching orchestrator...[/dim cyan]")
        orchestrator = await manager.launch(
            Orchestrator,
            kwargs={
                "model":       os.getenv("MODEL_NAME"),
                "base_url":    os.getenv("OPENAI_BASE_URL"),
                "api_key":     os.getenv("OPENAI_API_KEY"),
                "agents":      agents,
                "run_log":     run_log_path,
                "agent_state": initial_state,
            },
            name="orchestrator",
        )


        # The orchestrator's @loop runs the LangGraph once, then self-shuts down.
        # Wait for it to finish before tearing the manager down.
        console.print("[dim cyan][academy] workflow running -- waiting for completion...[/dim cyan]")
        await orchestrator.agentic_workflow()
        console.print("[dim cyan][academy] orchestrator finished.[/dim cyan]")


# __ Run _______________________________________________________________________

def main():
    args = user_input()

    console.print(Panel(
        f"[bold blue]MAW -- Multi-Agent Workflow (Academy Approach)[/bold blue]\n"
        f"[dim]Engine: {args.engine} | Env: {args.env}[/dim]",
        border_style="blue",
    ))

    pdf_path, image_path, selected_data_files, goal = collect_inputs(args)

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
        paper_id=_slugify(pdf_path) if pdf_path else "no_paper",
        paper_path=pdf_path,
        domain=args.domain,
        framework=args.engine,
        env=args.env,
        goal=goal,
        model=os.getenv("MODEL_NAME", "claudeopus48"),
    )

    try:
        asyncio.run(run_workflow(args, pdf_path, image_path, selected_data_files, goal, run_log_path))
        tracer.finalize_run("completed")
    except Exception as e:
        import traceback as _traceback
        tracer.log_run_error(type(e).__name__, str(e), _traceback.format_exc())
        tracer.finalize_run("failed")
        tracer.save(trace_path)
        console.print(f"[red]Run failed: {e}[/red]")
        console.print(f"[dim]Trace saved (partial): {trace_path}[/dim]")
        archive_path = archive_run(tracer.run_metadata, trace_path)
        if archive_path:
            console.print(f"[dim]Run archived (failed): {archive_path}[/dim]")
        raise

    tracer.save(trace_path)
    console.print(f"[dim]Trace saved: {trace_path}[/dim]")

    archive_path = archive_run(tracer.run_metadata, trace_path)
    if archive_path:
        console.print(f"[dim]Run archived: {archive_path}[/dim]")

    # __ Summary __
    summary = tracer.get_summary()
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


if __name__ == "__main__":
    main()
