from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from genomeagent.task_scanner import (
    SSHRemotePythonRunner,
    TaskScanError,
    TaskScannerCore,
    write_tsv,
)


class FakeRunner:
    host = "fixture-host"

    def run_python(self, program: str, timeout_seconds: int = 240):
        return {"program_seen": program, "timeout_seconds": timeout_seconds}


class FakeProfile:
    name = "fixture_task"
    title = "Fixture task"

    def collect(self, runner, config):
        return runner.run_python("fixture", timeout_seconds=config["timeout"])

    def interpret(self, data, config):
        return {
            "overall_status": "fixture_complete",
            "current_stage": "fixture",
            "next_safe_action": "none",
        }

    def write_artifacts(self, payload, scan_dir):
        artifact = scan_dir / "fixture.tsv"
        write_tsv(artifact, [{"a": "one\nline", "b": ["x", "y"]}], ["a", "b"])
        return [artifact]

    def render_report(self, payload, scan_dir):
        return "# Fixture report\n"


class TaskScannerCoreTests(unittest.TestCase):
    def test_remote_timeout_reports_last_phase_marker(self):
        expired = subprocess.TimeoutExpired(
            cmd=["ssh", "puhti"],
            timeout=240,
            stderr=b"GENOMEAGENT_PHASE scheduler start 2026-07-14T22:00:00+0300\n",
        )
        with patch("genomeagent.task_scanner.subprocess.run", side_effect=expired):
            with self.assertRaisesRegex(
                TaskScanError,
                "(?s)Last remote progress.*scheduler start",
            ):
                SSHRemotePythonRunner("puhti").run_python("fixture")

    def test_core_writes_standard_bundle_without_overwriting(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            core = TaskScannerCore(FakeRunner(), output_root=root)
            run = core.run(FakeProfile(), {"timeout": 12}, stamp="20260714T120000Z")

            self.assertEqual(run.scan_dir, root / "fixture_task" / "20260714T120000Z")
            payload = json.loads((run.scan_dir / "task_scan.json").read_text())
            self.assertEqual(payload["schema_version"], "1.0")
            self.assertEqual(payload["mode"], "read_only")
            self.assertEqual(payload["host"], "fixture-host")
            self.assertEqual(payload["status_summary"]["overall_status"], "fixture_complete")
            self.assertEqual(
                (run.scan_dir / "fixture.tsv").read_text(),
                "a\tb\none line\tx;y\n",
            )

            with self.assertRaises(TaskScanError):
                core.run(FakeProfile(), {"timeout": 12}, stamp="20260714T120000Z")


if __name__ == "__main__":
    unittest.main()
