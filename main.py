#!/usr/bin/env python3

from rich.console import Console
from rich.prompt import Prompt
from copilot.cluster import Cluster

console = Console()
cluster = Cluster()


def show_result(result):
    if result["ok"]:
        if result["stdout"]:
            console.print(result["stdout"])
        else:
            console.print("[green]Command completed, no output.[/green]")
    else:
        console.print("[red]Command failed or was blocked.[/red]")
        console.print(result["stderr"])


def help_text():
    console.print("""
[bold green]Commands[/bold green]

  jobs
      Show running jobs

  disk
      Show project filesystem disk usage

  size
      Show total project folder size

  project
      List /scratch/project_2001113

  run <safe command>
      Run a safe read-only command on cluster

  exit
      Quit
""")


def main():
    console.print("[bold green]Pangenome Copilot v0.2[/bold green]")
    console.print(f"Cluster: [cyan]{cluster.name}[/cyan]")
    console.print("Mode: safe read-only\n")

    help_text()

    while True:
        user_input = Prompt.ask("[bold]copilot[/bold]").strip()

        if user_input in {"exit", "quit"}:
            break

        if user_input == "jobs":
            show_result(cluster.list_jobs())

        elif user_input == "disk":
            show_result(cluster.disk_usage())

        elif user_input == "size":
            show_result(cluster.project_size())

        elif user_input == "project":
            show_result(cluster.list_project())

        elif user_input.startswith("run "):
            command = user_input.replace("run ", "", 1)
            show_result(cluster.run(command))

        else:
            help_text()


if __name__ == "__main__":
    main()
