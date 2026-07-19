#!/usr/bin/env python3
"""Git-backed Dataset Registry for GenomeAgent.

The registry separates dataset identity from scientific use and storage location.
A dataset can have many scientific links and many materialized locations without
being duplicated.  The component is local-only and never transfers or deletes
files.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Iterable, Mapping, Sequence

COMPONENT_VERSION = "0.1.2"
SCHEMA_VERSION = "1.1"
SUPPORTED_SCHEMA_VERSIONS = {"1.0", SCHEMA_VERSION}
RELATIONSHIPS = {"produced_by", "consumed_by", "associated_with", "derived_from"}
LOCATION_STATES = {
    "available_local",
    "partial_local",
    "uploading",
    "archived_unverified",
    "archived_verified",
    "available_local_and_uploading",
    "available_local_and_archived",
    "intentionally_removed_after_archive",
    "restore_required",
    "restoring",
    "missing_unexpected",
    "historical_location_unknown",
}
MATERIALIZATIONS = {"complete", "partial", "virtual", "unknown"}


class DatasetRegistryError(RuntimeError):
    """Raised for invalid registry operations."""


def utc_now() -> str:
    return (
        dt.datetime.now(dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def safe_identifier(value: str, label: str = "identifier") -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value):
        raise DatasetRegistryError(f"Invalid {label}: {value!r}")
    return value


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise DatasetRegistryError(f"Cannot read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise DatasetRegistryError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise DatasetRegistryError(f"Expected JSON object in {path}")
    return value


def write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(dict(value), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def dataset_dir(root: Path) -> Path:
    return Path(root) / "data_registry" / "datasets"


def archive_dir(root: Path) -> Path:
    return Path(root) / "data_registry" / "archives"


def adhoc_dir(root: Path) -> Path:
    return Path(root) / "workspace" / "allas_tasks" / "_adhoc_manifests"


def task_dir(root: Path) -> Path:
    return Path(root) / "config" / "major_tasks"


def rules_path(root: Path) -> Path:
    return Path(root) / "config" / "dataset_assignment" / "rules.json"


def dataset_path(root: Path, dataset_id: str) -> Path:
    return dataset_dir(root) / f"{safe_identifier(dataset_id, 'dataset ID')}.json"


def _dedupe_dicts(rows: Iterable[Mapping[str, Any]], key_fields: Sequence[str]) -> list[dict[str, Any]]:
    seen: set[tuple[str, ...]] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        key = tuple(str(item.get(field, "")) for field in key_fields)
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _location_id(location: Mapping[str, Any]) -> str:
    parts = [
        str(location.get("provider", "filesystem")),
        str(location.get("host", "")),
        str(location.get("path", "")),
        str(location.get("bucket", "")),
        str(location.get("object", "")),
        str(location.get("prefix", "")),
        str(location.get("sample_set_id", "")),
    ]
    return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()[:16]


def normalize_dataset(data: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(data)
    schema = str(result.get("schema_version", ""))
    if schema not in SUPPORTED_SCHEMA_VERSIONS:
        raise DatasetRegistryError(f"Unsupported dataset schema_version {schema!r}")
    dataset_id = safe_identifier(str(result.get("dataset_id", "")), "dataset ID")
    result["dataset_id"] = dataset_id
    result["schema_version"] = SCHEMA_VERSION
    result.setdefault("title", dataset_id)
    result.setdefault("description", "")
    result.setdefault("lifecycle_state", "unknown")

    links = list(result.get("scientific_links") or [])
    created = result.get("created_by")
    if isinstance(created, Mapping) and created.get("task_id"):
        links.append(
            {
                "major_task": created.get("task_id"),
                "run_id": created.get("run_id"),
                "stage_id": created.get("stage_id"),
                "relationship": "produced_by",
                "role": "output",
                "evidence": ["migrated from created_by"],
            }
        )
    for used in result.get("used_by") or []:
        if isinstance(used, Mapping) and used.get("task_id"):
            links.append(
                {
                    "major_task": used.get("task_id"),
                    "run_id": used.get("run_id"),
                    "stage_id": used.get("stage_id"),
                    "relationship": "consumed_by",
                    "role": used.get("role", "input"),
                    "evidence": ["migrated from used_by"],
                }
            )
    normalized_links: list[dict[str, Any]] = []
    for link in links:
        if not isinstance(link, Mapping):
            continue
        item = {k: v for k, v in dict(link).items() if v not in (None, "", [], {})}
        major_task = item.get("major_task") or item.get("task_id")
        if not major_task:
            continue
        item["major_task"] = safe_identifier(str(major_task), "major task")
        item.pop("task_id", None)
        relationship = str(item.get("relationship", "associated_with"))
        if relationship not in RELATIONSHIPS:
            raise DatasetRegistryError(f"Unsupported scientific relationship: {relationship}")
        item["relationship"] = relationship
        if item.get("run_id"):
            item["run_id"] = safe_identifier(str(item["run_id"]), "run ID")
        if item.get("stage_id"):
            item["stage_id"] = safe_identifier(str(item["stage_id"]), "stage ID")
        if item.get("task"):
            item["task"] = safe_identifier(str(item["task"]), "task ID")
        item.setdefault("added_at", utc_now())
        normalized_links.append(item)
    result["scientific_links"] = _dedupe_dicts(
        normalized_links,
        ("major_task", "run_id", "task", "stage_id", "relationship", "role"),
    )

    normalized_locations: list[dict[str, Any]] = []
    for location in result.get("locations") or []:
        if not isinstance(location, Mapping):
            continue
        # v1.0 allowed one record with paths[]. Expand it into addressable locations.
        paths = location.get("paths")
        expanded = []
        if isinstance(paths, list) and paths:
            for path in paths:
                item = dict(location)
                item.pop("paths", None)
                item["path"] = path
                expanded.append(item)
        else:
            expanded.append(dict(location))
        for item in expanded:
            provider = str(item.get("provider") or ("filesystem" if item.get("path") else "unknown"))
            item["provider"] = provider
            if provider == "filesystem":
                if not item.get("host") or not item.get("path"):
                    raise DatasetRegistryError(f"Filesystem location for {dataset_id} needs host and path")
                if not str(item["path"]).startswith("/"):
                    raise DatasetRegistryError(f"Filesystem path must be absolute: {item['path']!r}")
            elif provider == "allas":
                if not item.get("bucket"):
                    raise DatasetRegistryError(f"Allas location for {dataset_id} needs bucket")
                if not item.get("object") and not item.get("prefix"):
                    raise DatasetRegistryError(f"Allas location for {dataset_id} needs object or prefix")
            state = str(item.get("state", "historical_location_unknown"))
            if state not in LOCATION_STATES:
                raise DatasetRegistryError(f"Unsupported location state: {state}")
            item["state"] = state
            materialization = str(item.get("materialization", "unknown"))
            if materialization not in MATERIALIZATIONS:
                raise DatasetRegistryError(f"Unsupported materialization: {materialization}")
            item["materialization"] = materialization
            item.setdefault("location_id", _location_id(item))
            item.setdefault("added_at", utc_now())
            normalized_locations.append(item)
    result["locations"] = _dedupe_dicts(normalized_locations, ("location_id",))

    transfer_links = []
    for transfer in result.get("transfer_links") or []:
        if not isinstance(transfer, Mapping) or not transfer.get("task_id"):
            continue
        item = dict(transfer)
        item["task_id"] = safe_identifier(str(item["task_id"]), "transfer task ID")
        item.setdefault("added_at", utc_now())
        transfer_links.append(item)
    result["transfer_links"] = _dedupe_dicts(transfer_links, ("task_id", "operation"))
    result["assignment_status"] = "assigned" if result["scientific_links"] else "unassigned"
    result.setdefault("safety", {"automatic_local_deletion": False})
    result.setdefault("updated_at", utc_now())
    return result


def validate_dataset(data: Mapping[str, Any]) -> dict[str, Any]:
    result = normalize_dataset(data)
    if not isinstance(result.get("title"), str) or not result["title"].strip():
        raise DatasetRegistryError(f"{result['dataset_id']}.title must be non-empty")
    if not isinstance(result.get("locations"), list):
        raise DatasetRegistryError(f"{result['dataset_id']}.locations must be a list")
    return result


def load_dataset(root: Path, dataset_id: str) -> tuple[Path, dict[str, Any]]:
    path = dataset_path(root, dataset_id)
    if not path.exists():
        raise DatasetRegistryError(f"Unknown dataset {dataset_id!r}")
    return path, validate_dataset(read_json(path))


def save_dataset(root: Path, data: Mapping[str, Any]) -> Path:
    normalized = validate_dataset(data)
    normalized["updated_at"] = utc_now()
    path = dataset_path(root, normalized["dataset_id"])
    write_json(path, normalized)
    return path


def discover_datasets(root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    base = dataset_dir(root)
    if not base.exists():
        return rows
    for path in sorted(base.glob("*.json")):
        try:
            rows.append(validate_dataset(read_json(path)))
        except DatasetRegistryError:
            continue
    return rows


def discover_major_tasks(root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    base = task_dir(root)
    if not base.exists():
        return rows
    for path in sorted(base.glob("*.json")):
        try:
            data = read_json(path)
        except DatasetRegistryError:
            continue
        task_id = data.get("task_id") or path.stem
        if not isinstance(task_id, str) or not task_id:
            continue
        data = dict(data)
        data["task_id"] = task_id
        data["source_path"] = str(path)
        data.setdefault("runs", [])
        data.setdefault("stages", [])
        data.setdefault("tasks", [])
        rows.append(data)
    return rows


def major_task_by_id(root: Path, task_id: str) -> dict[str, Any]:
    for task in discover_major_tasks(root):
        if task["task_id"] == task_id:
            return task
    available = ", ".join(x["task_id"] for x in discover_major_tasks(root)) or "none"
    raise DatasetRegistryError(f"Unknown major task {task_id!r}. Available: {available}")


def major_task_choices(root: Path) -> list[str]:
    return [task["task_id"] for task in discover_major_tasks(root)]


def major_task_definition_path(root: Path, task_id: str) -> Path:
    return task_dir(root) / f"{safe_identifier(task_id, 'major task')}.json"


def _validated_task_definition(data: Mapping[str, Any], source: str) -> dict[str, Any]:
    # Reuse the authoritative Task Lifecycle validator without making the
    # dataset registry responsible for the lifecycle schema.
    try:
        from genomeagent.task_lifecycle import LifecycleError, validate_task_definition
    except ImportError as exc:
        raise DatasetRegistryError(f"Task Lifecycle component is unavailable: {exc}") from exc
    try:
        return validate_task_definition(dict(data), source)
    except LifecycleError as exc:
        raise DatasetRegistryError(str(exc)) from exc


def create_incremental_major_task(
    root: Path,
    *,
    task_id: str,
    title: str,
    description: str,
    first_task_id: str,
    first_task_title: str,
    status: str = "active",
    first_task_status: str = "active",
    methods_boundary: str | None = None,
    evidence: Sequence[str] | None = None,
) -> Path:
    """Create a valid incremental major task together with its first evidenced task."""
    task_id = safe_identifier(task_id, "major task")
    first_task_id = safe_identifier(first_task_id, "task ID")
    path = major_task_definition_path(root, task_id)
    if path.exists():
        raise DatasetRegistryError(f"Major task already exists: {task_id}")
    title = str(title).strip()
    description = str(description).strip()
    first_task_title = str(first_task_title).strip()
    if not title or not description or not first_task_title:
        raise DatasetRegistryError("Major-task title, description, and first task title must be non-empty")
    component: dict[str, Any] = {
        "task_id": first_task_id,
        "title": first_task_title,
        "status": first_task_status,
    }
    if evidence:
        component["evidence"] = list(evidence)
    data = {
        "schema_version": "1.0",
        "definition_mode": "incremental_registry",
        "task_id": task_id,
        "title": title,
        "description": description,
        "status": status,
        "methods_boundary": methods_boundary or (
            "Incremental registry of evidenced datasets, tasks, successful executions, and outputs."
        ),
        "tasks": [component],
        "stages": [],
        "runs": [],
        "registry_policy": {
            "add_only_evidenced_tasks": True,
            "allow_multiple_dataset_consumers": True,
            "do_not_invent_missing_stages": True,
        },
    }
    validated = _validated_task_definition(data, str(path))
    write_json(path, validated)
    return path


def add_incremental_task(
    root: Path,
    *,
    major_task: str,
    task_id: str,
    title: str,
    status: str = "active",
    evidence: Sequence[str] | None = None,
) -> Path:
    """Add one evidenced component task to an incremental major task."""
    definition = major_task_by_id(root, major_task)
    if definition.get("definition_mode", "workflow") != "incremental_registry":
        raise DatasetRegistryError(
            f"Cannot add component tasks to workflow major task {major_task!r}; "
            "select an existing run/stage or create a separate incremental major task."
        )
    task_id = safe_identifier(task_id, "task ID")
    title = str(title).strip()
    if not title:
        raise DatasetRegistryError("Task title must be non-empty")
    existing = {str(item.get("task_id")) for item in definition.get("tasks", []) if item.get("task_id")}
    if task_id in existing:
        raise DatasetRegistryError(f"Task {task_id!r} already exists under {major_task}")
    item: dict[str, Any] = {"task_id": task_id, "title": title, "status": status}
    if evidence:
        item["evidence"] = list(evidence)
    definition = dict(definition)
    definition.pop("source_path", None)
    definition["tasks"] = list(definition.get("tasks", [])) + [item]
    path = major_task_definition_path(root, major_task)
    validated = _validated_task_definition(definition, str(path))
    write_json(path, validated)
    return path


def make_link(
    root: Path,
    *,
    major_task: str,
    relationship: str,
    role: str | None = None,
    run_id: str | None = None,
    task: str | None = None,
    stage_id: str | None = None,
    evidence: Sequence[str] | None = None,
) -> dict[str, Any]:
    definition = major_task_by_id(root, safe_identifier(major_task, "major task"))
    if relationship not in RELATIONSHIPS:
        raise DatasetRegistryError(f"Unsupported relationship {relationship!r}")
    run_ids = {str(x.get("run_id")) for x in definition.get("runs", []) if x.get("run_id")}
    stage_ids = {str(x.get("stage_id")) for x in definition.get("stages", []) if x.get("stage_id")}
    task_ids = {str(x.get("task_id")) for x in definition.get("tasks", []) if x.get("task_id")}
    if run_id and run_id not in run_ids:
        raise DatasetRegistryError(f"Unknown run {run_id!r} under {major_task}")
    if stage_id and stage_id not in stage_ids:
        raise DatasetRegistryError(f"Unknown stage {stage_id!r} under {major_task}")
    if task and task not in task_ids:
        raise DatasetRegistryError(f"Unknown task {task!r} under {major_task}")
    link = {
        "major_task": major_task,
        "relationship": relationship,
        "added_at": utc_now(),
    }
    if role:
        link["role"] = role
    if run_id:
        link["run_id"] = run_id
    if task:
        link["task"] = task
    if stage_id:
        link["stage_id"] = stage_id
    if evidence:
        link["evidence"] = list(evidence)
    return link


def add_scientific_link(root: Path, dataset_id: str, link: Mapping[str, Any]) -> Path:
    _, data = load_dataset(root, dataset_id)
    links = list(data.get("scientific_links") or [])
    links.append(dict(link))
    data["scientific_links"] = links
    return save_dataset(root, data)


def add_location(root: Path, dataset_id: str, location: Mapping[str, Any]) -> Path:
    _, data = load_dataset(root, dataset_id)
    locations = list(data.get("locations") or [])
    locations.append(dict(location))
    data["locations"] = locations
    return save_dataset(root, data)


def create_or_update_dataset(
    root: Path,
    *,
    dataset_id: str,
    title: str | None = None,
    description: str | None = None,
    locations: Sequence[Mapping[str, Any]] | None = None,
    scientific_links: Sequence[Mapping[str, Any]] | None = None,
    transfer_links: Sequence[Mapping[str, Any]] | None = None,
    lifecycle_state: str | None = None,
    expected: Mapping[str, Any] | None = None,
) -> Path:
    path = dataset_path(root, dataset_id)
    if path.exists():
        data = validate_dataset(read_json(path))
    else:
        data = {
            "schema_version": SCHEMA_VERSION,
            "dataset_id": dataset_id,
            "title": title or dataset_id,
            "description": description or "",
            "lifecycle_state": lifecycle_state or "unknown",
            "scientific_links": [],
            "locations": [],
            "transfer_links": [],
            "created_at": utc_now(),
            "safety": {"automatic_local_deletion": False},
        }
    if title:
        data["title"] = title
    if description:
        data["description"] = description
    if lifecycle_state:
        data["lifecycle_state"] = lifecycle_state
    if locations:
        data["locations"] = list(data.get("locations") or []) + [dict(x) for x in locations]
    if scientific_links:
        data["scientific_links"] = list(data.get("scientific_links") or []) + [dict(x) for x in scientific_links]
    if transfer_links:
        data["transfer_links"] = list(data.get("transfer_links") or []) + [dict(x) for x in transfer_links]
    if expected:
        merged = dict(data.get("expected") or {})
        merged.update(expected)
        data["expected"] = merged
    return save_dataset(root, data)


def _read_optional_json(path: Path) -> dict[str, Any] | None:
    try:
        return read_json(path)
    except DatasetRegistryError:
        return None


def discover_adhoc_transfers(root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    base = adhoc_dir(root)
    if not base.exists():
        return rows
    for path in sorted(base.glob("*.json")):
        data = _read_optional_json(path)
        if not data or data.get("task_mode") != "ad_hoc_folder":
            continue
        data = dict(data)
        data["manifest_path"] = str(path)
        rows.append(data)
    return rows


def transfer_by_identifier(root: Path, identifier: str) -> tuple[Path, dict[str, Any]]:
    direct = adhoc_dir(root) / f"{identifier}.json"
    if direct.exists():
        data = read_json(direct)
        return direct, data
    normalized = identifier.rstrip("/")
    for transfer in discover_adhoc_transfers(root):
        original = str(transfer.get("local", {}).get("original_path", "")).rstrip("/")
        if normalized == original:
            return Path(transfer["manifest_path"]), transfer
    raise DatasetRegistryError(f"Unknown ad-hoc transfer or source path: {identifier!r}")


def datasets_for_transfer(root: Path, task_id: str) -> list[dict[str, Any]]:
    out = []
    for dataset in discover_datasets(root):
        if any(str(x.get("task_id")) == task_id for x in dataset.get("transfer_links", [])):
            out.append(dataset)
    return out


def _allas_location_from_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    local = manifest.get("local", {}) if isinstance(manifest.get("local"), Mapping) else {}
    archive = manifest.get("archive", {}) if isinstance(manifest.get("archive"), Mapping) else {}
    roots = local.get("paths") or []
    transfer_task_id = str(manifest.get("registered_from_adhoc_task") or manifest.get("dataset_id") or "")
    prefix = str(roots[0]).rstrip("/") if len(roots) == 1 else str(local.get("original_path", "")).strip("/")
    status = str(archive.get("status", "not_verified"))
    state = "archived_verified" if status.startswith("verified") else ("uploading" if status in {"uploading", "submitted"} else "archived_unverified")
    location = {
        "provider": "allas",
        "bucket": archive.get("bucket"),
        "prefix": prefix or str(manifest.get("dataset_id", "")),
        "state": state,
        "materialization": "complete",
        "verification_status": status,
        "transfer_task_id": transfer_task_id,
    }
    if archive.get("verified_at"):
        location["verified_at"] = archive["verified_at"]
    return location


def register_transfer(
    root: Path,
    *,
    identifier: str,
    dataset_id: str,
    title: str | None = None,
    description: str | None = None,
    scientific_links: Sequence[Mapping[str, Any]] | None = None,
) -> Path:
    manifest_path, manifest = transfer_by_identifier(root, identifier)
    task_id = str(manifest.get("dataset_id") or identifier)
    local = manifest.get("local", {})
    source = local.get("original_path")
    locations: list[dict[str, Any]] = []
    if source:
        local_state = str(local.get("status", "available_local"))
        state = local_state if local_state in LOCATION_STATES else "available_local"
        locations.append(
            {
                "provider": "filesystem",
                "host": manifest.get("remote", {}).get("host", "unknown"),
                "path": source,
                "state": state,
                "materialization": "complete",
            }
        )
    locations.append(_allas_location_from_manifest(manifest))
    transfer_link = {
        "task_id": task_id,
        "operation": "upload",
        "manifest_path": str(manifest_path.relative_to(root)) if manifest_path.is_relative_to(root) else str(manifest_path),
        "status": manifest.get("archive", {}).get("status", "unknown"),
        "added_at": utc_now(),
    }
    return create_or_update_dataset(
        root,
        dataset_id=dataset_id,
        title=title,
        description=description,
        locations=locations,
        scientific_links=scientific_links,
        transfer_links=[transfer_link],
        lifecycle_state="available_local_and_uploading" if transfer_link["status"] in {"not_verified", "uploading", "submitted"} else "available_local_and_archived",
    )


def sync_transfer_manifest(root: Path, manifest: Mapping[str, Any]) -> list[Path]:
    task_id = str(manifest.get("registered_from_adhoc_task") or manifest.get("dataset_id") or "")
    if not task_id:
        return []
    updated: list[Path] = []
    for dataset in datasets_for_transfer(root, task_id):
        locations = list(dataset.get("locations", []))
        status = str(manifest.get("archive", {}).get("status", ""))
        state = "archived_verified" if status.startswith("verified") else "archived_unverified"
        matched = False
        for location in locations:
            if location.get("provider") == "allas" and location.get("transfer_task_id") == task_id:
                location["state"] = state
                location["verification_status"] = status
                if manifest.get("archive", {}).get("verified_at"):
                    location["verified_at"] = manifest["archive"]["verified_at"]
                matched = True
        if not matched:
            locations.append(_allas_location_from_manifest(manifest))
        dataset["locations"] = locations
        if status.startswith("verified"):
            has_local = any(x.get("provider") == "filesystem" and x.get("state") in {"available_local", "partial_local"} for x in locations)
            dataset["lifecycle_state"] = "available_local_and_archived" if has_local else "archived_verified"
        for link in dataset.get("transfer_links", []):
            if link.get("task_id") == task_id:
                link["status"] = status
                link["updated_at"] = utc_now()
        updated.append(save_dataset(root, dataset))
    return updated



def register_managed_archive(root: Path, dataset_id: str) -> Path | None:
    manifest_path = archive_dir(root) / f"{dataset_id}.json"
    if not manifest_path.exists() or not dataset_path(root, dataset_id).exists():
        return None
    manifest = read_json(manifest_path)
    local = manifest.get("local", {}) if isinstance(manifest.get("local"), Mapping) else {}
    archive = manifest.get("archive", {}) if isinstance(manifest.get("archive"), Mapping) else {}
    status = str(archive.get("status", "not_verified"))
    state = "archived_verified" if status.startswith("verified") else "archived_unverified"
    transfer_task_id = str(manifest.get("dataset_id") or dataset_id)
    locations = []
    for rel in local.get("paths") or []:
        locations.append(
            {
                "provider": "allas",
                "bucket": archive.get("bucket"),
                "prefix": str(rel).rstrip("/"),
                "state": state,
                "materialization": "complete",
                "verification_status": status,
                "transfer_task_id": transfer_task_id,
            }
        )
    if not locations:
        locations.append(_allas_location_from_manifest(manifest))
    transfer_link = {
        "task_id": transfer_task_id,
        "operation": "upload",
        "manifest_path": str(manifest_path.relative_to(root)),
        "status": status,
        "added_at": utc_now(),
    }
    return create_or_update_dataset(
        root,
        dataset_id=dataset_id,
        locations=locations,
        transfer_links=[transfer_link],
    )


def resolve_dataset_identifier(root: Path, identifier: str) -> dict[str, Any] | None:
    direct = dataset_path(root, identifier) if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", identifier) else None
    if direct and direct.exists():
        return validate_dataset(read_json(direct))
    normalized = identifier.rstrip("/")
    for dataset in discover_datasets(root):
        for location in dataset.get("locations", []):
            if normalized == str(location.get("path", "")).rstrip("/"):
                return dataset
            allas = "/".join(x for x in [str(location.get("bucket", "")), str(location.get("object") or location.get("prefix") or "")] if x)
            if normalized == allas.rstrip("/"):
                return dataset
        if any(str(x.get("task_id")) == identifier for x in dataset.get("transfer_links", [])):
            return dataset
    return None


def list_unassigned(root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for dataset in discover_datasets(root):
        if not dataset.get("scientific_links"):
            rows.append({"kind": "dataset", "identifier": dataset["dataset_id"], "description": dataset.get("description", "")})
    assigned_transfers = {
        str(link.get("task_id"))
        for dataset in discover_datasets(root)
        for link in dataset.get("transfer_links", [])
        if link.get("task_id")
    }
    for transfer in discover_adhoc_transfers(root):
        task_id = str(transfer.get("dataset_id", ""))
        if task_id and task_id not in assigned_transfers:
            rows.append(
                {
                    "kind": "allas_transfer",
                    "identifier": task_id,
                    "description": str(transfer.get("local", {}).get("original_path", "")),
                    "status": str(transfer.get("archive", {}).get("status", "unknown")),
                }
            )
    return rows


def load_rules(root: Path) -> list[dict[str, Any]]:
    path = rules_path(root)
    if not path.exists():
        return []
    data = read_json(path)
    rules = data.get("rules", [])
    return [dict(x) for x in rules if isinstance(x, Mapping)]


def suggest_assignment(root: Path, identifier: str) -> list[dict[str, Any]]:
    targets = [identifier]
    try:
        _, transfer = transfer_by_identifier(root, identifier)
        local = transfer.get("local", {})
        targets.extend([str(local.get("original_path", "")), str(local.get("source_root", ""))])
    except DatasetRegistryError:
        pass
    dataset = resolve_dataset_identifier(root, identifier)
    if dataset:
        targets.extend(
            str(x.get("path") or x.get("object") or x.get("prefix") or "")
            for x in dataset.get("locations", [])
        )
    suggestions: list[dict[str, Any]] = []
    for rule in load_rules(root):
        patterns = rule.get("match", [])
        if isinstance(patterns, str):
            patterns = [patterns]
        matched = any(re.search(str(pattern), target, flags=re.IGNORECASE) for pattern in patterns for target in targets if target)
        if not matched:
            continue
        suggestion = {
            "confidence": rule.get("confidence", "medium"),
            "dataset_id": rule.get("dataset_id"),
            "title": rule.get("title"),
            "description": rule.get("description", ""),
            "scientific_links": rule.get("scientific_links", []),
            "evidence": [f"assignment rule {rule.get('rule_id', 'unnamed')} matched"],
        }
        suggestions.append(suggestion)
    order = {"high": 0, "medium": 1, "low": 2}
    return sorted(suggestions, key=lambda x: order.get(str(x.get("confidence")), 9))


def migrate_all(root: Path) -> list[Path]:
    updated: list[Path] = []
    for path in sorted(dataset_dir(root).glob("*.json")) if dataset_dir(root).exists() else []:
        normalized = validate_dataset(read_json(path))
        write_json(path, normalized)
        updated.append(path)
    return updated
