#!/usr/bin/env python3
"""
GenomeAgent Brain v0.1
----------------------

First AI component of GenomeAgent.

Purpose:
    Read workspace/project_snapshot.json and ask the OpenAI API which
    files should be inspected first to understand the genome project.

Input:
    workspace/project_snapshot.json

Output:
    workspace/fetch_plan.json
    workspace/brain_notes.md

Requirements:
    pip install openai
    export OPENAI_API_KEY="..."

Optional:
    export GENOMEAGENT_MODEL="gpt-5.5"

Run:
    python3 brain.py
"""

import json
import os
import sys
from pathlib import Path
from datetime import datetime, UTC

try:
    from openai import OpenAI
except ImportError:
    sys.exit(
        "Missing dependency: openai\n"
        "Install with:\n\n"
        "    pip install openai\n"
    )

WORKSPACE = Path("workspace")
SNAPSHOT = WORKSPACE / "project_snapshot.json"
FETCH_PLAN = WORKSPACE / "fetch_plan.json"
BRAIN_NOTES = WORKSPACE / "brain_notes.md"

MODEL = os.environ.get("GENOMEAGENT_MODEL", "gpt-5.5")


FETCH_PLAN_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "scan_assessment": {
            "type": "string",
            "description": "Brief assessment of what kind of project this appears to be."
        },
        "selected_files": {
            "type": "array",
            "description": "Files whose contents should be inspected first.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "path": {"type": "string"},
                    "priority": {
                        "type": "string",
                        "enum": ["high", "medium", "low"]
                    },
                    "reason": {"type": "string"},
                    "likely_subsystem": {"type": "string"}
                },
                "required": ["path", "priority", "reason", "likely_subsystem"]
            }
        },
        "ignored_patterns": {
            "type": "array",
            "description": "Path or filename patterns that should probably be ignored for first-pass workflow understanding.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "pattern": {"type": "string"},
                    "reason": {"type": "string"}
                },
                "required": ["pattern", "reason"]
            }
        },
        "initial_hypotheses": {
            "type": "array",
            "description": "Initial hypotheses about project workflows inferred from paths alone.",
            "items": {"type": "string"}
        },
        "remaining_questions": {
            "type": "array",
            "description": "Questions the Brain wants to answer after inspecting selected files.",
            "items": {"type": "string"}
        }
    },
    "required": [
        "scan_assessment",
        "selected_files",
        "ignored_patterns",
        "initial_hypotheses",
        "remaining_questions"
    ]
}


def load_snapshot() -> dict:
    if not SNAPSHOT.exists():
        sys.exit(f"Missing input file: {SNAPSHOT}")
    return json.loads(SNAPSHOT.read_text(encoding="utf-8"))


def compact_inventory(snapshot: dict, max_files: int = 4000) -> list[dict]:
    """
    Keep only path and size for the first Brain step.
    This step intentionally does not include file contents.
    """
    files = snapshot.get("scripts", [])
    compact = []
    for item in files[:max_files]:
        path = item.get("path", "")
        compact.append({
            "path": path,
            "size": item.get("size", None)
        })
    return compact


def build_prompt(inventory: list[dict]) -> str:
    return f"""
You are GenomeBrain, the reasoning component of GenomeAgent.

You are analyzing the first inventory of a large computational genomics
research project from an HPC system. Your task is NOT to understand the full
project yet. Your task is to decide which files should be inspected first.

Use only file paths, filenames, extensions, and sizes. Do not assume you have
file contents yet.

Goals:
1. Select files that are likely to define the user's actual genome project
   workflows.
2. Prioritize scripts/configuration files that are likely entry points,
   SLURM jobs, pipeline drivers, or analysis scripts.
3. Ignore generated reports, software installations, third-party libraries,
   package internals, and collaborator/unrelated folders when they are obvious.
4. Prefer diversity: select representative files from major subsystems rather
   than hundreds of near-duplicates.
5. If uncertain, include the file with medium or low priority and explain why.

Important context:
- This is a Fragaria / genome / pangenome / WGBS / RNA-seq / assembly project.
- The project may contain pangenome graph construction, vg giraffe mapping,
  variant calling, methylation analyses, RNA-seq analyses, assembly QC,
  annotation work, miRNA work, Allas storage scripts, and HPC/SLURM workflows.
- Generated fastp JSON files are reports, not workflow scripts.
- Installed tools and package directories should not be first-pass targets.

Return valid JSON matching the requested schema.

Inventory:
{json.dumps(inventory, indent=2)}
""".strip()


def ask_openai(prompt: str) -> dict:
    client = OpenAI()

    response = client.responses.create(
        model=MODEL,
        input=[
            {
                "role": "system",
                "content": (
                    "You are GenomeBrain, an AI research assistant that "
                    "plans efficient exploration of computational genomics projects."
                ),
            },
            {
                "role": "user",
                "content": prompt,
            },
        ],
        text={
            "format": {
                "type": "json_schema",
                "name": "genomeagent_fetch_plan",
                "schema": FETCH_PLAN_SCHEMA,
                "strict": True,
            }
        },
    )

    raw = response.output_text
    return json.loads(raw)


def write_outputs(plan: dict) -> None:
    WORKSPACE.mkdir(exist_ok=True)

    plan["_genomeagent"] = {
        "brain_version": "GenomeBrain v0.1",
        "model": MODEL,
        "created_at": datetime.now(UTC).isoformat(),
        "input": str(SNAPSHOT),
    }

    FETCH_PLAN.write_text(json.dumps(plan, indent=2), encoding="utf-8")

    selected = plan.get("selected_files", [])
    high = [x for x in selected if x.get("priority") == "high"]
    medium = [x for x in selected if x.get("priority") == "medium"]
    low = [x for x in selected if x.get("priority") == "low"]

    notes = []
    notes.append("# GenomeBrain notes\n")
    notes.append(f"Created: {plan['_genomeagent']['created_at']}\n")
    notes.append(f"Model: `{MODEL}`\n")
    notes.append("## Scan assessment\n")
    notes.append(plan.get("scan_assessment", "") + "\n")
    notes.append("## Selected files\n")
    notes.append(f"- High priority: {len(high)}")
    notes.append(f"- Medium priority: {len(medium)}")
    notes.append(f"- Low priority: {len(low)}\n")

    for priority, group in [("High", high), ("Medium", medium), ("Low", low)]:
        notes.append(f"### {priority} priority\n")
        for item in group:
            notes.append(
                f"- `{item['path']}` — {item['likely_subsystem']}: {item['reason']}"
            )
        notes.append("")

    notes.append("## Ignored patterns\n")
    for item in plan.get("ignored_patterns", []):
        notes.append(f"- `{item['pattern']}` — {item['reason']}")
    notes.append("")

    notes.append("## Initial hypotheses\n")
    for h in plan.get("initial_hypotheses", []):
        notes.append(f"- {h}")
    notes.append("")

    notes.append("## Remaining questions\n")
    for q in plan.get("remaining_questions", []):
        notes.append(f"- {q}")
    notes.append("")

    BRAIN_NOTES.write_text("\n".join(notes), encoding="utf-8")


def main() -> None:
    snapshot = load_snapshot()
    inventory = compact_inventory(snapshot)

    print("=" * 60)
    print("GenomeBrain v0.1")
    print("=" * 60)
    print(f"Input snapshot : {SNAPSHOT}")
    print(f"Inventory items: {len(inventory)}")
    print(f"Model          : {MODEL}")
    print("Asking OpenAI which files should be inspected first...")

    prompt = build_prompt(inventory)
    plan = ask_openai(prompt)
    write_outputs(plan)

    print()
    print("✓ Brain planning complete")
    print(f"Fetch plan : {FETCH_PLAN}")
    print(f"Notes      : {BRAIN_NOTES}")
    print(f"Selected   : {len(plan.get('selected_files', []))} files")


if __name__ == "__main__":
    main()
