from rich.console import Console
console = Console()
def exception_message(exit_message: str) -> None:
    console.print(f"[red]{exit_message}[/red]")
    raise SystemExit(1)

