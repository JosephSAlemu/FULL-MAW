from rich.console import Console

def exception_message(exit_message: str) -> None:
    Console.print(f"[red]{exit_message}[/red]")
    raise SystemExit(1)

