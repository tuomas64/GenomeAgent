#!/usr/bin/env python3
"""
GenomeAgent Brain v1.0
----------------------

Single-call project learning.

Inputs:
    workspace/project_snapshot_brain.json
    workspace/scripts_brain.tar.gz

Outputs:
    workspace/brain_input.md
    workspace/project_overview.md
    workspace/project_knowledge.json
    workspace/brain_raw_response.txt

Requirements:
    python -m pip install openai python-dotenv

Environment:
    OPENAI_API_KEY must be set, or stored in .env
    Optional: GENOMEAGENT_MODEL, default gpt-5.5

Run:
    python3 brain.py
"""

import json
import os
import sys
import tarfile
from pathlib import Path
from datetime import datetime, UTC

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

try:
    from openai import OpenAI
except ImportError:
    sys.exit("Missing dependency: openai\nInstall with: python -m pip install openai python-dotenv")

if load_dotenv:
    load_dotenv()

WORKSPACE = Path("workspace")

SNAPSHOT = WORKSPACE / "project_snapshot_brain.json"
TARBALL = WORKSPACE / "scripts_brain.tar.gz"

BRAIN_INPUT = WORKSPACE / "brain_input.md"
OVERVIEW = WORKSPACE / "project_overview.md"
KNOWLEDGE = WORKSPACE / "project_knowledge.json"
RAW_RESPONSE = WORKSPACE / "brain_raw_response.txt"

MODEL = os.environ.get("GENOMEAGENT_MODEL", "gpt-5.5")

MAX_TOTAL_CHARS = int(os.environ.get("GENOMEAGENT_MAX_TOTAL_CHARS", "900000"))


RESPONSE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "project_overview_md": {
            "type": "string",
            "description": "Human-readable Markdown overview of the project."
        },
        "project_knowledge": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "project_name": {"type": "string"},
                "project_type": {"type": "string"},
                "main_goal": {"type": "string"},
                "major_workflows": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "name": {"type": "string"},
                            "purpose": {"type": "string"},
                            "entry_scripts": {
                                "type": "array",
                                "items": {"type": "string"}
                            },
                            "important_scripts": {
                                "type": "array",
                                "items": {"type": "string"}
                            },
                            "inputs": {
                                "type": "array",
                                "items": {"type": "string"}
                            },
                            "outputs": {
                                "type": "array",
                                "items": {"type": "string"}
                            },
                            "software": {
                                "type": "array",
                                "items": {"type": "string"}
                            },
                            "notes": {"type": "string"}
                        },
                        "required": [
                            "name", "purpose", "entry_scripts",
                            "important_scripts", "inputs", "outputs",
                            "software", "notes"
                        ]
                    }
                },
                "important_paths": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "path": {"type": "string"},
                            "meaning": {"type": "string"}
                        },
                        "required": ["path", "meaning"]
                    }
                },
                "software_and_versions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "software": {"type": "string"},
                            "version_or_path": {"type": "string"},
                            "evidence": {"type": "string"}
                        },
                        "required": ["software", "version_or_path", "evidence"]
                    }
                },
                "repetitive_tasks_to_automate": {
                    "type": "array",
                    "items": {"type": "string"}
                },
                "recommended_defaults": {
                    "type": "array",
                    "items": {"type": "string"}
                },
                "possible_obsolete_or_duplicate_scripts": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "path": {"type": "string"},
                            "reason": {"type": "string"}
                        },
                        "required": ["path", "reason"]
                    }
                },
                "open_questions": {
                    "type": "array",
                    "items": {"type": "string"}
                }
            },
            "required": [
                "project_name", "project_type", "main_goal",
                "major_workflows", "important_paths",
                "software_and_versions",
                "repetitive_tasks_to_automate",
                "recommended_defaults",
                "possible_obsolete_or_duplicate_scripts",
                "open_questions"
            ]
        }
    },
    "required": ["project_overview_md", "project_knowledge"]
}


def language_for_path(path: str) -> str:
    lower = path.lower()
    if lower.endswith((".slurm", ".sbatch", ".sh", ".bash", ".zsh")):
        return "bash"
    if lower.endswith(".py"):
        return "python"
    if lower.endswith((".r", ".rscript")):
        return "r"
    if lower.endswith((".yml", ".yaml")):
        return "yaml"
    if lower.endswith(".json"):
        return "json"
    return "text"


def load_snapshot_paths() -> list[str]:
    if not SNAPSHOT.exists():
        sys.exit(f"Missing {SNAPSHOT}")
    data = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    return [x["path"] for x in data.get("scripts", [])]


def tar_member_to_snapshot_path(member_name: str) -> str:
    return member_name.lstrip("./").lstrip("/")


def build_brain_input() -> str:
    if not TARBALL.exists():
        sys.exit(f"Missing {TARBALL}")

    snapshot_paths = set(load_snapshot_paths())

    sections = []
    sections.append("# GenomeAgent Brain Input\n")
    sections.append(f"Created: {datetime.now(UTC).isoformat()}\n")
    sections.append(f"Snapshot: `{SNAPSHOT}`\n")
    sections.append(f"Tarball: `{TARBALL}`\n")
    sections.append(f"Scripts listed in snapshot: {len(snapshot_paths)}\n")
    sections.append("\n---\n")

    total_chars = 0
    added = 0
    skipped = 0

    with tarfile.open(TARBALL, "r:gz") as tar:
        members = [m for m in tar.getmembers() if m.isfile()]
        members.sort(key=lambda m: m.name)

        for member in members:
            member_path = tar_member_to_snapshot_path(member.name)

            # Usually member names should match snapshot paths. If not, keep anyway.
            extracted = tar.extractfile(member)
            if extracted is None:
                skipped += 1
                continue

            raw = extracted.read()
            try:
                text = raw.decode("utf-8", errors="replace")
            except Exception:
                skipped += 1
                continue

            if total_chars + len(text) > MAX_TOTAL_CHARS:
                skipped += 1
                continue

            lang = language_for_path(member_path)
            sections.append(f"\n## File: `{member_path}`\n")
            sections.append(f"Size: {len(raw)} bytes\n\n")
            sections.append(f"```{lang}\n{text}\n```\n")
            sections.append("\n---\n")

            total_chars += len(text)
            added += 1

    sections.append("\n# Brain input summary\n")
    sections.append(f"- Files included: {added}\n")
    sections.append(f"- Files skipped because of size/encoding/limits: {skipped}\n")
    sections.append(f"- Total script characters included: {total_chars}\n")

    brain_input = "".join(sections)
    BRAIN_INPUT.write_text(brain_input, encoding="utf-8")
    return brain_input


def build_prompt(brain_input: str) -> str:
    return f"""
You are GenomeBrain, the AI reasoning component of GenomeAgent.

You are given one Markdown document containing the filtered computational
scripts of a genomics research project. The scripts were collected from an HPC
project directory. Your task is to learn the project once and create useful
memory for future repetitive analyses.

Focus on practical reusable knowledge:
- What are the major workflows?
- Which scripts are entry points?
- Which scripts are variants/duplicates/obsolete?
- What software, modules, containers, and versions are used?
- What important paths, references, graphs, sample lists, and output folders appear?
- What repetitive analyses should GenomeAgent be able to generate later?
- What defaults should GenomeAgent remember when writing new SLURM scripts?

Be careful:
- Do not pretend certainty when scripts only give partial evidence.
- Prefer concrete paths and script names.
- Separate main workflows from helper scripts.
- Identify alternative mapping or analysis approaches when visible.
- This is a Fragaria / genome / pangenome / WGBS / RNA-seq / assembly project.

Return structured JSON according to the schema. The field project_overview_md
must be a readable Markdown overview. The field project_knowledge must be
machine-readable memory for future GenomeAgent tasks.

Here is the Brain input document:

{brain_input}
""".strip()


def call_openai(prompt: str) -> dict:
    client = OpenAI()

    response = client.responses.create(
        model=MODEL,
        input=[
            {
                "role": "system",
                "content": (
                    "You are GenomeBrain, an expert AI assistant for "
                    "computational genomics and HPC workflow understanding."
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
                "name": "genomeagent_project_knowledge",
                "schema": RESPONSE_SCHEMA,
                "strict": True,
            }
        },
    )

    RAW_RESPONSE.write_text(response.output_text, encoding="utf-8")
    return json.loads(response.output_text)


def main() -> None:
    WORKSPACE.mkdir(exist_ok=True)

    print("=" * 60)
    print("GenomeBrain v1.0")
    print("=" * 60)
    print(f"Model      : {MODEL}")
    print(f"Snapshot   : {SNAPSHOT}")
    print(f"Tarball    : {TARBALL}")
    print("Building brain_input.md from scripts_brain.tar.gz...")

    brain_input = build_brain_input()

    print(f"Brain input: {BRAIN_INPUT}")
    print(f"Characters : {len(brain_input)}")
    print("Calling OpenAI once to learn the project...")

    prompt = build_prompt(brain_input)
    result = call_openai(prompt)

    OVERVIEW.write_text(result["project_overview_md"], encoding="utf-8")
    KNOWLEDGE.write_text(
        json.dumps(result["project_knowledge"], indent=2),
        encoding="utf-8"
    )

    print()
    print("✓ GenomeBrain learning complete")
    print(f"Wrote: {OVERVIEW}")
    print(f"Wrote: {KNOWLEDGE}")
    print(f"Wrote: {RAW_RESPONSE}")


if __name__ == "__main__":
    main()
