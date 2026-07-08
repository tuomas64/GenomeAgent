#!/usr/bin/env python3

import json
import subprocess
from pathlib import Path
from datetime import datetime


class ExplorerCore:
    """
    Read-only Explorer Core.

    It connects to Puhti, records login context, changes to the configured
    scratch workspace, runs safe read-only commands, and saves raw JSON
    observations. It does not interpret or modify remote files.
    """

    def __init__(
        self,
        host_alias="puhti",
        cluster_name="puhti",
        user="tuomas64",
        workspace="/scratch/project_2001113",
        observation_dir="observations",
    ):
        self.host_alias = host_alias
        self.cluster_name = cluster_name
        self.user = user
        self.workspace = workspace
        self.observation_dir = Path(observation_dir)
        self.observation_dir.mkdir(parents=True, exist_ok=True)

    def timestamp(self):
        return datetime.now().isoformat(timespec="seconds")

    def save_observation(self, observation):
        safe_label = observation["label"].replace(" ", "_").replace("/", "_")
        ts = observation["timestamp"].replace(":", "-")
        out = self.observation_dir / f"{ts}_{safe_label}.json"
        with open(out, "w") as f:
            json.dump(observation, f, indent=2)

    def run_login_context(self):
        command = "pwd && hostname && whoami"
        result = subprocess.run(
            ["ssh", self.host_alias, command],
            text=True,
            capture_output=True,
        )

        observation = {
            "timestamp": self.timestamp(),
            "cluster": self.cluster_name,
            "host_alias": self.host_alias,
            "user": self.user,
            "workspace": None,
            "label": "login_context",
            "category": "cluster_observation",
            "command": command,
            "remote_command": command,
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "interpretation": None,
            "confidence": "observed_raw",
        }
        self.save_observation(observation)
        return observation

    def run_workspace_command(self, command, label):
        remote_command = f"cd {self.workspace} && {command}"
        result = subprocess.run(
            ["ssh", self.host_alias, remote_command],
            text=True,
            capture_output=True,
        )

        observation = {
            "timestamp": self.timestamp(),
            "cluster": self.cluster_name,
            "host_alias": self.host_alias,
            "user": self.user,
            "workspace": self.workspace,
            "label": label,
            "category": "workspace_observation",
            "command": command,
            "remote_command": remote_command,
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "interpretation": None,
            "confidence": "observed_raw",
        }
        self.save_observation(observation)
        return observation

    def explore_basic(self):
        observations = []
        observations.append(self.run_login_context())

        commands = [
            ("workspace_context", "pwd && hostname && whoami"),
            ("workspace_top_level", "find . -maxdepth 1 -mindepth 1 -printf '%f\\n' | sort | head -200"),
            ("workspace_directories_depth2", "find . -maxdepth 2 -type d | sort | head -300"),
            ("recent_shell_scripts", "find . -maxdepth 5 \\( -name '*.sh' -o -name '*.bash' -o -name '*.slurm' \\) -printf '%TY-%Tm-%Td %TH:%TM %p\\n' | sort -r | head -100"),
            ("project_disk_overview", "df -h ."),
            ("running_jobs", f"squeue -u {self.user}"),
        ]

        for label, command in commands:
            observations.append(self.run_workspace_command(command, label))

        return observations


def print_summary(observations):
    print()
    print("Explorer Core observations")
    print("==========================")
    for obs in observations:
        status = "OK" if obs["returncode"] == 0 else "FAILED"
        stdout_lines = len(obs["stdout"].splitlines()) if obs["stdout"] else 0
        stderr_lines = len(obs["stderr"].splitlines()) if obs["stderr"] else 0
        print(f"{obs['label']}: {status} | stdout lines={stdout_lines} | stderr lines={stderr_lines}")
    print()
    print("Raw JSON observations saved in: observations/")


def main():
    explorer = ExplorerCore()
    observations = explorer.explore_basic()
    print_summary(observations)


if __name__ == "__main__":
    main()
