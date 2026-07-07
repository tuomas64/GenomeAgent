SAFE_COMMANDS = {
    "hostname", "whoami", "pwd", "ls", "du", "df",
    "squeue", "sacct", "seff", "tail", "head",
    "cat", "grep", "find", "wc"
}

BLOCKED_PATTERNS = [
    "rm ", "rm -", "mv ", "cp ", "scp ", "rsync ",
    "sbatch", "scancel", "chmod", "chown", "sudo",
    ">", ">>", "| sh", "| bash", "python ", "perl "
]


def is_safe(command: str) -> bool:
    command = command.strip()

    if not command:
        return False

    if any(pattern in command for pattern in BLOCKED_PATTERNS):
        return False

    first_word = command.split()[0]
    return first_word in SAFE_COMMANDS
