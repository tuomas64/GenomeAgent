import subprocess
import yaml
from pathlib import Path
from datetime import datetime

from .safety import is_safe


class Cluster:
    def __init__(self, config_path="config/cluster.yaml"):
        with open(config_path, "r") as f:
            cfg = yaml.safe_load(f)

        self.name = cfg["cluster"]["name"]
        self.host_alias = cfg["cluster"]["host_alias"]
        self.user = cfg["cluster"]["user"]
        self.scratch_project = cfg["cluster"]["scratch_project"]

        Path("logs").mkdir(exist_ok=True)

    def log(self, command, stdout, stderr, returncode):
        with open("logs/commands.log", "a") as f:
            f.write("\n" + "=" * 80 + "\n")
            f.write(f"time: {datetime.now().isoformat()}\n")
            f.write(f"cluster: {self.name}\n")
            f.write(f"command: {command}\n")
            f.write(f"returncode: {returncode}\n")
            f.write("--- stdout ---\n")
            f.write(stdout or "")
            f.write("\n--- stderr ---\n")
            f.write(stderr or "")
            f.write("\n")

    def run(self, command):
        if not is_safe(command):
            return {
                "ok": False,
                "stdout": "",
                "stderr": f"Blocked unsafe command: {command}",
                "returncode": None,
            }

        result = subprocess.run(
            ["ssh", self.host_alias, command],
            text=True,
            capture_output=True,
        )

        self.log(command, result.stdout, result.stderr, result.returncode)

        return {
            "ok": result.returncode == 0,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "returncode": result.returncode,
        }

    def list_jobs(self):
        return self.run(f"squeue -u {self.user}")

    def disk_usage(self):
        return self.run(f"df -h {self.scratch_project}")

    def project_size(self):
        return self.run(f"du -sh {self.scratch_project}")

    def list_project(self):
        return self.run(f"ls -lh {self.scratch_project}")
