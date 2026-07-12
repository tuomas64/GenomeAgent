#!/usr/bin/env python3

import subprocess
from rich.console import Console
from rich.prompt import Prompt

console = Console()

PUHTI_HOST = "puhti"
PUHTI_USER = "tuomas64"

SAFE_COMMANDS = [
    "hostname",
    "whoami",
    "pwd",
    "ls",
    "du",
    "df",
    "squeue",
    "sacct",
    "seff",
    "tail",
    "head",
    "cat",
    "grep",
    "find",
]

BLOCKED_PATTERNS = [
    "rm ",
    "rm -",
    "mv ",
    "scp ",
    "rsync ",
    "sbatch",
    "scancel",
    "chmod",
    "chown",
    "sudo",
    ">",
    ">>",
    "| sh",
    "| bash",
]


def is_safe(command: str) -> bool:
    command = command.strip()

    if any(pattern in command for pattern in BLOCKED_PATTERNS):
        return False

    first_word = command.split()[0] if command.split() else ""

    return first_word in SAFE_COMMANDS


def run_puhti(command: str):
    if not is_safe(command):
        console.print("[red]Blocked for safety.[/red]")
        console.print(f"Command was: {command}")
        return

    ssh_command = ["ssh", PUHTI_HOST, command]

    console.print(f"[cyan]Running on Puhti:[/cyan] {command}\n")

    result = subprocess.run(
        ssh_command,
        text=True,
        capture_output=True,
    )

    if result.stdout:
        console.print(result.stdout)

    if result.stderr:
        console.print("[yellow]STDERR:[/yellow]")
        console.print(result.stderr)


def main():
    console.print("[bold green]Pangenome Copilot v0.1[/bold green]")
    console.print("Safe read-only Puhti assistant")
    console.print("Type 'exit' to quit.\n")

    while True:
        user_input = Prompt.ask("[bold]copilot[/bold]")

        if user_input.lower() in {"exit", "quit"}:
            break

        if user_input == "jobs":
            run_puhti(f"squeue -u {PUHTI_USER}")
        elif user_input == "disk":
            run_puhti("df -h /scratch/project_2001113")
        elif user_input == "home":
            run_puhti("pwd && ls -lh")
        elif user_input.startswith("run "):
            command = user_input.replace("run ", "", 1)
            run_puhti(command)
        else:
            console.print("""
Commands:

  jobs
      Show running Puhti jobs

  disk
      Show /scratch/project_2001113 disk usage

  home
      Show Puhti home directory

  run <safe command>
      Run a safe read-only command on Puhti

Examples:

  jobs
  disk
  run hostname
  run squeue -u tuomas64
  run ls -lh /scratch/project_2001113
  run du -sh /scratch/project_2001113/pangenome
""")


if __name__ == "__main__":
    main()
