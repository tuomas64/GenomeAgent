#!/usr/bin/env python3
"""Python entry point equivalent to bin/genomeagent-allas."""
from __future__ import annotations

import runpy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
runpy.run_path(str(ROOT / "tools" / "allas" / "genomeagent_allas.py"), run_name="__main__")
