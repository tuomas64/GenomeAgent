#!/usr/bin/env python3
"""Controlled, bounded GPU inference benchmarks for registered AI backends.

The core deliberately separates local preparation, explicit authorization, one
confined Slurm submission, read-only status collection and deterministic local
scoring.  Model output is data only and is never evaluated as code or a command.
"""

from __future__ import annotations

import hashlib
import base64
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Optional, Protocol

from genomeagent.ai_evaluation import AIEvaluationCore, AIRegistry


AI_BENCHMARK_EXECUTION_POLICY_VERSION = "1.0"
POLICY_FALSE_FIELDS = (
    "automatic_authorization_allowed",
    "automatic_job_submission_allowed",
    "model_download_allowed",
    "external_model_access_allowed",
    "sensitive_project_data_allowed",
    "model_output_execution_allowed",
    "training_allowed",
    "automatic_registry_update_allowed",
    "automatic_backend_activation_allowed",
)
TERMINAL_REMOTE_STATES = {
    "benchmark_completed_unscored",
    "benchmark_failed_runtime",
    "benchmark_failed_output_contract",
}


class AIBenchmarkExecutionError(RuntimeError):
    """Raised when benchmark evidence or requested authority is unsafe."""


class PythonRemoteRunner(Protocol):
    host: str

    def run_python(self, program: str, timeout_seconds: int = 240) -> Mapping[str, Any]:
        ...


@dataclass(frozen=True)
class BenchmarkPlanResult:
    backend_id: str
    suite_id: str
    run_id: str
    plan_id: str
    status: str
    blockers: tuple[str, ...]
    plan_dir: Path
    artifact_paths: tuple[Path, ...]


@dataclass(frozen=True)
class BenchmarkAuthorizationResult:
    backend_id: str
    suite_id: str
    plan_id: str
    authorization_id: str
    authorization_path: Path
    status: str
    expires_at_utc: str
    created: bool


@dataclass(frozen=True)
class BenchmarkLaunchResult:
    backend_id: str
    suite_id: str
    execution_id: str
    job_id: str
    remote_status: str
    launch_path: Path


@dataclass(frozen=True)
class BenchmarkStatusResult:
    backend_id: str
    suite_id: str
    execution_id: str
    job_id: str
    status: str
    observation_path: Path
    responses_path: Optional[Path]


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_value(value: Any) -> str:
    return _sha256_bytes(_canonical_bytes(value))


def _pretty_json(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _write_immutable(path: Path, text: str) -> bool:
    if path.exists():
        if path.read_text(encoding="utf-8") != text:
            raise AIBenchmarkExecutionError(
                "Immutable benchmark artifact differs: {}".format(path)
            )
        return False
    _atomic_write(path, text)
    return True


def _read_json(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except FileNotFoundError as exc:
        raise AIBenchmarkExecutionError("{} does not exist: {}".format(label, path)) from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AIBenchmarkExecutionError("Invalid {}: {}".format(label, exc)) from exc
    if not isinstance(value, dict):
        raise AIBenchmarkExecutionError("{} must be a JSON object.".format(label))
    return value, raw


def _identifier(value: Any, label: str) -> str:
    value = str(value or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]*", value):
        raise AIBenchmarkExecutionError("Unsafe or empty {}.".format(label))
    return value


def _digest(value: Any, label: str) -> str:
    value = str(value or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise AIBenchmarkExecutionError("Invalid {}.".format(label))
    return value


def _reviewer(value: Any) -> str:
    value = str(value or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9@._-]{0,127}", value):
        raise AIBenchmarkExecutionError("Unsafe or empty reviewer identifier.")
    return value


def _utc(value: datetime) -> str:
    if value.tzinfo is None:
        raise AIBenchmarkExecutionError("Timestamp must include a timezone.")
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_utc(value: Any) -> datetime:
    try:
        parsed = datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise AIBenchmarkExecutionError("Invalid UTC timestamp.") from exc
    return parsed.replace(tzinfo=timezone.utc)


def _safe_absolute(value: Any, label: str) -> str:
    value = str(value or "").strip()
    path = PurePosixPath(value)
    if (
        not value
        or not path.is_absolute()
        or "\x00" in value
        or any(part in {"", ".", ".."} for part in path.parts[1:])
    ):
        raise AIBenchmarkExecutionError("{} must be a safe absolute path.".format(label))
    return value


def _source(path: Path, raw: bytes, artifact_type: str) -> dict[str, Any]:
    return {"artifact_type": artifact_type, "path": str(path), "sha256": _sha256_bytes(raw)}


def _validate_policy(path: Path, backend_id: str) -> tuple[dict[str, Any], bytes]:
    policy, raw = _read_json(path, "benchmark execution policy")
    if (
        policy.get("schema_version") != "1.0"
        or policy.get("policy_version") != AI_BENCHMARK_EXECUTION_POLICY_VERSION
        or policy.get("backend_id") != backend_id
    ):
        raise AIBenchmarkExecutionError("Unsupported benchmark execution policy.")
    for key in ("ssh_host", "remote_python", "runtime_module"):
        if not re.fullmatch(r"[A-Za-z0-9_./+-]+", str(policy.get(key) or "")):
            raise AIBenchmarkExecutionError("Unsafe benchmark policy field {}.".format(key))
    for key in ("control_root", "module_initialization", "module_use_path"):
        _safe_absolute(policy.get(key), key)
    limits = policy.get("limits")
    expected_limits = {
        "authorization_validity_seconds": (300, 1800),
        "launch_timeout_seconds": (30, 240),
        "status_timeout_seconds": (30, 240),
        "maximum_requests": (1, 32),
        "maximum_request_bytes": (4096, 1048576),
        "maximum_response_bytes": (65536, 8388608),
        "maximum_completion_tokens_per_case": (64, 4096),
        "maximum_model_length": (2048, 32768),
        "gpu_memory_utilization_basis_points": (5000, 9000),
    }
    if not isinstance(limits, Mapping) or set(limits) != set(expected_limits):
        raise AIBenchmarkExecutionError("Benchmark limit field set is invalid.")
    for name, (low, high) in expected_limits.items():
        value = limits[name]
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise AIBenchmarkExecutionError("Invalid benchmark limit {}.".format(name))
    slurm = policy.get("slurm")
    expected_slurm = {
        "account": "project_2001113",
        "partition": "gputest",
        "time_limit": "00:15:00",
        "nodes": 1,
        "ntasks": 1,
        "cpus_per_task": 16,
        "gpus": 1,
        "gpu_type": "gh200",
        "memory": "120G",
        "job_name": "GAaibench",
    }
    if slurm != expected_slurm:
        raise AIBenchmarkExecutionError("Unsupported bounded benchmark Slurm contract.")
    contract = policy.get("execution_contract")
    if not isinstance(contract, Mapping) or contract != {
        "suite_id": "genomeagent_core_v1",
        "data_classification": "non_sensitive_fixtures_only",
        "offline_provider_mode_required": True,
        "fixed_temperature": 0.0,
        "fixed_seed": 2001113,
        "model_output_execution_allowed": False,
    }:
        raise AIBenchmarkExecutionError("Benchmark execution contract is invalid.")
    safety = policy.get("safety")
    if not isinstance(safety, Mapping) or set(safety) != set(POLICY_FALSE_FIELDS):
        raise AIBenchmarkExecutionError("Benchmark safety field set is invalid.")
    if any(safety[field] is not False for field in POLICY_FALSE_FIELDS):
        raise AIBenchmarkExecutionError("Benchmark policy grants forbidden authority.")
    return policy, raw


def _validate_registered_backend(backend: Mapping[str, Any]) -> None:
    readiness = backend.get("derived_readiness")
    if not isinstance(readiness, Mapping) or readiness.get("blockers") != [
        "inference_not_benchmarked"
    ]:
        raise AIBenchmarkExecutionError(
            "Backend must have inference_not_benchmarked as its only readiness blocker."
        )
    if backend.get("status") != "validated_candidate" or backend.get(
        "benchmark_status"
    ) != "not_run":
        raise AIBenchmarkExecutionError("Backend is not a registered unbenchmarked candidate.")
    model = backend.get("model")
    installation = backend.get("installation")
    if not isinstance(model, Mapping) or not isinstance(installation, Mapping):
        raise AIBenchmarkExecutionError("Backend model installation identity is incomplete.")
    _digest(model.get("weights_sha256"), "registered model digest")
    _digest(installation.get("manifest_sha256"), "installed manifest digest")
    if (
        installation.get("status") != "verified_present"
        or model.get("weights_digest_type")
        != "verified_model_candidate_manifest_sha256"
    ):
        raise AIBenchmarkExecutionError("Backend installation is not verified.")
    _safe_absolute(installation.get("path"), "installed model path")


def _load_backend_state(
    state_root: Path, backend_id: str, backend: Mapping[str, Any]
) -> tuple[Path, dict[str, Any], bytes]:
    path = state_root / backend_id / "current_evidence.json"
    state, raw = _read_json(path, "current AI backend evidence")
    readiness = state.get("readiness")
    if (
        state.get("backend_id") != backend_id
        or not isinstance(readiness, Mapping)
        or readiness.get("status") != "ready_for_bounded_benchmark_review"
        or readiness.get("blockers") != ["inference_not_benchmarked"]
    ):
        raise AIBenchmarkExecutionError("Backend evidence is not benchmark-review-ready.")
    observation = state.get("observation")
    if not isinstance(observation, Mapping):
        raise AIBenchmarkExecutionError("Backend evidence observation is missing.")
    model = observation.get("model")
    if not isinstance(model, Mapping):
        raise AIBenchmarkExecutionError("Backend model evidence is missing.")
    expected_manifest = backend["installation"]["manifest_sha256"]
    inventory_manifest = model.get("inventory_manifest")
    metadata = (
        inventory_manifest.get("metadata")
        if isinstance(inventory_manifest, Mapping)
        else None
    )
    if not isinstance(metadata, Mapping) or metadata.get("sha256") != expected_manifest:
        raise AIBenchmarkExecutionError("Fresh evidence does not match registered manifest.")
    return path, state, raw


def _plan_report(plan: Mapping[str, Any]) -> str:
    blockers = plan.get("blockers") or []
    lines = [
        "# GenomeAgent Bounded GPU Benchmark Plan",
        "",
        "- Backend: `{}`".format(plan["backend_id"]),
        "- Suite: `{}`".format(plan["suite_id"]),
        "- Plan ID: `{}`".format(plan["plan_id"]),
        "- Status: **{}**".format(plan["status"]),
        "- Cases: {}".format(plan["case_count"]),
        "- GPU limit: one GH200",
        "- Runtime limit: 15 minutes",
        "- Automatic submission: **no**",
        "",
        "## Blockers",
        "",
    ]
    lines.extend("- `{}`".format(item) for item in blockers)
    if not blockers:
        lines.append("No evidence blockers remain; fresh researcher authorization is required.")
    lines.extend([
        "",
        "## Safety boundary",
        "",
        "Only the eight non-sensitive fixture requests are eligible. Provider network access, "
        "model download, training, generated-output execution, automatic registry updates and "
        "backend activation remain disabled.",
        "",
    ])
    return "\n".join(lines)


def _authorization_report(value: Mapping[str, Any]) -> str:
    return "\n".join([
        "# GenomeAgent Bounded GPU Benchmark Authorization",
        "",
        "- Authorization: `{}`".format(value["authorization_id"]),
        "- Backend: `{}`".format(value["backend_id"]),
        "- Suite: `{}`".format(value["suite_id"]),
        "- Reviewer: `{}`".format(value["reviewer"]),
        "- Expires: `{}`".format(value["expires_at_utc"]),
        "- Scope: one exact Slurm job using one GH200 and fixture data only",
        "- Model output execution: **no**",
        "- Training: **no**",
        "",
    ])


def _worker_source() -> str:
    """Return the fixed offline inference worker installed in a confined control dir."""
    return r'''#!/usr/bin/env python3
import json
import os
import time
import traceback
from pathlib import Path

control=Path(os.environ["GENOMEAGENT_BENCHMARK_CONTROL"])
spec=json.loads((control/"execution_spec.json").read_text())

def atomic(path, text):
    temporary=path.with_name(path.name+".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)

def status(state, **extra):
    value={"schema_version":"1.0","state":state,"execution_id":spec["execution_id"],
           "job_id":os.environ.get("SLURM_JOB_ID"),"updated_at_epoch":time.time(),
           "model_download":False,"external_provider_access":False,
           "model_output_execution":False,"training":False,
           "registry_update":False,"backend_activation":False}
    value.update(extra); atomic(control/"status.json",json.dumps(value,sort_keys=True)+"\n")

try:
    status("benchmark_running")
    os.environ["HF_HUB_OFFLINE"]="1"
    os.environ["TRANSFORMERS_OFFLINE"]="1"
    os.environ["HF_DATASETS_OFFLINE"]="1"
    from vllm import LLM, SamplingParams
    model_path=Path(spec["model_path"])
    manifest_raw=(model_path/".genomeagent-model-manifest.json").read_bytes()
    if __import__("hashlib").sha256(manifest_raw).hexdigest()!=spec["model_manifest_sha256"]:
        raise RuntimeError("installed model manifest changed before benchmark")
    raw=(control/"requests.jsonl").read_text(encoding="utf-8")
    if __import__("hashlib").sha256(raw.encode("utf-8")).hexdigest()!=spec["requests_sha256"]:
        raise RuntimeError("fixture request digest changed before benchmark")
    requests=[json.loads(line) for line in raw.splitlines() if line.strip()]
    llm=LLM(model=spec["model_path"],dtype=spec["dtype"],
            max_model_len=spec["maximum_model_length"],tensor_parallel_size=1,
            gpu_memory_utilization=spec["gpu_memory_utilization"],
            trust_remote_code=False,enable_prefix_caching=True)
    responses=[]
    for request in requests:
        messages=list(request["messages"])
        messages[-1]=dict(messages[-1])
        messages[-1]["content"] += "\nRequired output JSON schema:\n" + json.dumps(
            request["response_format"],sort_keys=True,separators=(",",":"))
        sampling=request["sampling"]
        params=SamplingParams(temperature=0.0,top_p=1.0,
            max_tokens=min(int(sampling.get("max_tokens",1200)),spec["maximum_completion_tokens"]),
            seed=spec["seed"])
        started=time.monotonic()
        generated=llm.chat(messages,params,use_tqdm=False)
        elapsed=time.monotonic()-started
        candidate=generated[0].outputs[0]
        responses.append({"case_id":request["case_id"],"output":candidate.text,
            "metrics":{"prompt_tokens":len(generated[0].prompt_token_ids),
                       "completion_tokens":len(candidate.token_ids),
                       "latency_seconds":round(elapsed,6)}})
    payload="".join(json.dumps(item,sort_keys=True,ensure_ascii=False)+"\n" for item in responses)
    if len(payload.encode("utf-8"))>spec["maximum_response_bytes"]:
        raise RuntimeError("bounded response payload exceeded")
    atomic(control/"responses.jsonl",payload)
    status("benchmark_completed_unscored",case_count=len(responses),
           response_bytes=len(payload.encode("utf-8")),responses_sha256=__import__("hashlib").sha256(payload.encode()).hexdigest())
except Exception as exc:
    atomic(control/"failure.txt",("{}: {}\n{}".format(type(exc).__name__,exc,traceback.format_exc()))[-12000:])
    status("benchmark_failed_runtime",error_type=type(exc).__name__)
    raise
'''


def _shell_source(policy: Mapping[str, Any]) -> str:
    return """#!/bin/bash
set -euo pipefail
source {module_init}
module use {module_use}
module load {runtime_module}
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 HF_DATASETS_OFFLINE=1
exec python3 \"$GENOMEAGENT_BENCHMARK_CONTROL/worker.py\"
""".format(
        module_init=policy["module_initialization"],
        module_use=policy["module_use_path"],
        runtime_module=policy["runtime_module"],
    )


def _launch_program(config: Mapping[str, Any]) -> str:
    encoded = base64.b64encode(_canonical_bytes(config)).decode("ascii")
    return r'''import base64
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

cfg=json.loads(base64.b64decode("__CONFIG__").decode("utf-8"))
control=Path(cfg["control_path"]); root=Path(cfg["control_root"])
result={"status":"launch_failed","job_id":None,"control_path":str(control)}
try:
    control.relative_to(root)
    if control.exists():
        launch=control/"launch.json"
        if launch.is_file():
            previous=json.loads(launch.read_text())
            if previous.get("execution_id")==cfg["execution_id"]:
                print(json.dumps(previous,sort_keys=True)); raise SystemExit(0)
        raise RuntimeError("control path already exists without matching launch")
    control.mkdir(parents=True,mode=0o750)
    files={"execution_spec.json":json.dumps(cfg["execution_spec"],sort_keys=True,indent=2)+"\n",
           "requests.jsonl":cfg["requests_jsonl"],"worker.py":cfg["worker_source"],
           "run.sh":cfg["shell_source"]}
    for name,text in files.items():
        path=control/name
        descriptor=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o750 if name.endswith(".sh") else 0o640)
        with os.fdopen(descriptor,"w",encoding="utf-8") as handle: handle.write(text)
    s=cfg["slurm"]
    command=["sbatch","--parsable","--account="+s["account"],"--partition="+s["partition"],
        "--time="+s["time_limit"],"--nodes="+str(s["nodes"]),"--ntasks="+str(s["ntasks"]),
        "--cpus-per-task="+str(s["cpus_per_task"]),"--mem="+s["memory"],
        "--gres=gpu:"+s["gpu_type"]+":"+str(s["gpus"]),"--job-name="+s["job_name"],
        "--output="+str(control/"slurm.out"),"--error="+str(control/"slurm.err"),
        "--export=ALL,GENOMEAGENT_BENCHMARK_CONTROL="+str(control),str(control/"run.sh")]
    completed=subprocess.run(command,stdin=subprocess.DEVNULL,capture_output=True,text=True,timeout=60,check=False)
    job=(completed.stdout.strip().split(";",1)[0] if completed.returncode==0 else "")
    if not re.fullmatch(r"[0-9]+",job):
        raise RuntimeError("sbatch failed: "+completed.stderr[-1000:])
    result={"schema_version":"1.0","status":"benchmark_submitted","execution_id":cfg["execution_id"],
            "job_id":job,"control_path":str(control),"requests_sha256":cfg["execution_spec"]["requests_sha256"],
            "model_manifest_sha256":cfg["execution_spec"]["model_manifest_sha256"],
            "model_download":False,"external_provider_access":False,"training":False,
            "model_output_execution":False,"registry_update":False,"backend_activation":False}
    (control/"launch.json").write_text(json.dumps(result,sort_keys=True,indent=2)+"\n")
    print(json.dumps(result,sort_keys=True))
except SystemExit: raise
except Exception as exc:
    result["error"]="{}: {}".format(type(exc).__name__,exc)
    print(json.dumps(result,sort_keys=True))
'''.replace("__CONFIG__", encoded)


def _status_program(config: Mapping[str, Any]) -> str:
    encoded = base64.b64encode(_canonical_bytes(config)).decode("ascii")
    return r'''import base64
import json
import os
import subprocess
from pathlib import Path

cfg=json.loads(base64.b64decode("__CONFIG__").decode("utf-8")); control=Path(cfg["control_path"])
result={"status":"benchmark_status_unavailable","execution_id":cfg["execution_id"],"job_id":cfg["job_id"]}
try:
    launch=json.loads((control/"launch.json").read_text())
    if launch.get("execution_id")!=cfg["execution_id"] or launch.get("job_id")!=cfg["job_id"]:
        raise RuntimeError("remote launch identity mismatch")
    state=None
    status_path=control/"status.json"
    if status_path.is_file():
        status=json.loads(status_path.read_text()); state=status.get("state"); result["worker_status"]=status
    command=["sacct","-j",cfg["job_id"],"-X","-n","-P","--format=JobIDRaw,State,Elapsed,ExitCode,MaxRSS,ReqMem,AllocTRES%100"]
    completed=subprocess.run(command,stdin=subprocess.DEVNULL,capture_output=True,text=True,timeout=30,check=False)
    result["sacct_returncode"]=completed.returncode; result["sacct_stdout"]=completed.stdout[-12000:]
    if state in ("benchmark_completed_unscored","benchmark_failed_runtime","benchmark_failed_output_contract"):
        result["status"]=state
    elif state=="benchmark_running": result["status"]="benchmark_running"
    else:
        upper=completed.stdout.upper()
        result["status"]="benchmark_queued_or_running" if any(x in upper for x in ("PENDING","RUNNING","CONFIGURING")) else "benchmark_state_requires_review"
    response=control/"responses.jsonl"
    if result["status"]=="benchmark_completed_unscored":
        raw=response.read_bytes()
        if len(raw)>cfg["maximum_response_bytes"]: raise RuntimeError("response evidence exceeds limit")
        result["responses_jsonl"]=raw.decode("utf-8")
        result["responses_sha256"]=__import__("hashlib").sha256(raw).hexdigest()
    for name in ("slurm.err","failure.txt"):
        path=control/name
        if path.is_file(): result[name.replace(".","_")]=path.read_text(errors="replace")[-12000:]
except Exception as exc:
    result["status"]="benchmark_status_requires_review"; result["error"]="{}: {}".format(type(exc).__name__,exc)
print(json.dumps(result,sort_keys=True))
'''.replace("__CONFIG__", encoded)


class AIBenchmarkExecutionCore:
    """Plan, authorize, submit and observe one bounded fixture benchmark."""

    def __init__(
        self,
        registry: Optional[AIRegistry] = None,
        policy_root: Path = Path("config/ai/benchmark_execution"),
        backend_state_root: Path = Path("workspace/ai_backend_state"),
        run_root: Path = Path("workspace/ai_runs"),
        plan_root: Path = Path("workspace/ai_benchmark_plans"),
        authorization_root: Path = Path("workspace/ai_benchmark_authorizations"),
        execution_root: Path = Path("workspace/ai_benchmark_executions"),
        evaluation_root: Path = Path("workspace/ai_evaluations"),
    ):
        self.registry = registry or AIRegistry()
        self.policy_root = Path(policy_root)
        self.backend_state_root = Path(backend_state_root)
        self.run_root = Path(run_root)
        self.plan_root = Path(plan_root)
        self.authorization_root = Path(authorization_root)
        self.execution_root = Path(execution_root)
        self.evaluation_root = Path(evaluation_root)

    def policy(self, backend_id: str) -> tuple[Path, dict[str, Any], bytes]:
        backend_id = _identifier(backend_id, "backend_id")
        path = self.policy_root / (backend_id + ".json")
        value, raw = _validate_policy(path, backend_id)
        return path, value, raw

    def prepare(self, backend_id: str, suite_id: str) -> BenchmarkPlanResult:
        backend_id = _identifier(backend_id, "backend_id")
        suite_id = _identifier(suite_id, "suite_id")
        backend_path, backend, backend_raw = self.registry.backend(backend_id)
        _validate_registered_backend(backend)
        policy_path, policy, policy_raw = self.policy(backend_id)
        if suite_id != policy["execution_contract"]["suite_id"]:
            raise AIBenchmarkExecutionError("Suite is not allowed by benchmark policy.")
        state_path, state, state_raw = _load_backend_state(
            self.backend_state_root, backend_id, backend
        )
        prepared = AIEvaluationCore(
            registry=self.registry, run_root=self.run_root,
            evaluation_root=self.evaluation_root,
        ).prepare(backend_id, suite_id)
        if prepared.blockers != ("inference_not_benchmarked",):
            raise AIBenchmarkExecutionError("Prepared run has unexpected blockers.")
        manifest_path = prepared.run_dir / "run_manifest.json"
        requests_path = prepared.run_dir / "requests.jsonl"
        manifest, manifest_raw = _read_json(manifest_path, "benchmark run manifest")
        requests_raw = requests_path.read_bytes()
        lines = [line for line in requests_raw.decode("utf-8").splitlines() if line.strip()]
        if not lines or len(lines) > policy["limits"]["maximum_requests"]:
            raise AIBenchmarkExecutionError("Prepared request count is outside policy.")
        if len(requests_raw) > policy["limits"]["maximum_request_bytes"]:
            raise AIBenchmarkExecutionError("Prepared fixture requests exceed policy size.")
        if manifest.get("data_classification") != "non_sensitive_fixtures_only":
            raise AIBenchmarkExecutionError("Benchmark contains an ineligible data class.")
        sources = [
            _source(backend_path, backend_raw, "registered_ai_backend"),
            _source(policy_path, policy_raw, "benchmark_execution_policy"),
            _source(state_path, state_raw, "fresh_ai_backend_evidence_state"),
            _source(manifest_path, manifest_raw, "ai_run_manifest"),
            _source(requests_path, requests_raw, "ai_fixture_requests"),
        ]
        identity = {
            "policy_version": AI_BENCHMARK_EXECUTION_POLICY_VERSION,
            "backend_id": backend_id,
            "suite_id": suite_id,
            "run_id": prepared.run_id,
            "sources": sources,
        }
        plan_id = _sha256_value(identity)
        blockers = ["fresh_benchmark_execution_authorization_missing"]
        plan = {
            "schema_version": "1.0",
            "policy_version": AI_BENCHMARK_EXECUTION_POLICY_VERSION,
            "source_mode": "deterministic_local_bounded_benchmark_plan",
            "backend_id": backend_id,
            "suite_id": suite_id,
            "run_id": prepared.run_id,
            "plan_id": plan_id,
            "status": "ready_for_bounded_benchmark_authorization_review",
            "blockers": blockers,
            "case_count": len(lines),
            "model_path": backend["installation"]["path"],
            "model_revision": backend["model"]["revision"],
            "model_manifest_sha256": backend["installation"]["manifest_sha256"],
            "requests_sha256": _sha256_bytes(requests_raw),
            "source_artifacts": sources,
            "resource_request": dict(policy["slurm"]),
            "automatic_submission_allowed": False,
            "model_output_execution_allowed": False,
            "training_allowed": False,
            "backend_activation_allowed": False,
        }
        provenance = {
            "schema_version": "1.0",
            "plan_id": plan_id,
            "source_artifacts": sources,
            "remote_access": False,
            "job_submission": False,
            "gpu_allocation": False,
            "inference": False,
            "training": False,
        }
        plan_dir = self.plan_root / backend_id / suite_id / plan_id
        paths = (plan_dir / "benchmark_plan.json", plan_dir / "report.md", plan_dir / "provenance.json")
        texts = (_pretty_json(plan), _plan_report(plan), _pretty_json(provenance))
        for path, text in zip(paths, texts):
            _write_immutable(path, text)
        return BenchmarkPlanResult(
            backend_id, suite_id, prepared.run_id, plan_id,
            str(plan["status"]), tuple(blockers), plan_dir, paths,
        )

    def _load_plan(self, backend_id: str, suite_id: str, plan_id: str) -> tuple[dict[str, Any], bytes, Path]:
        backend_id = _identifier(backend_id, "backend_id")
        suite_id = _identifier(suite_id, "suite_id")
        plan_id = _digest(plan_id, "plan ID")
        path = self.plan_root / backend_id / suite_id / plan_id / "benchmark_plan.json"
        plan, raw = _read_json(path, "benchmark plan")
        if (
            plan.get("backend_id") != backend_id
            or plan.get("suite_id") != suite_id
            or plan.get("plan_id") != plan_id
            or plan.get("status") != "ready_for_bounded_benchmark_authorization_review"
        ):
            raise AIBenchmarkExecutionError("Benchmark plan identity or status is invalid.")
        for source in plan.get("source_artifacts") or []:
            source_path = Path(str(source.get("path") or ""))
            if not source_path.is_file() or _sha256_bytes(source_path.read_bytes()) != source.get("sha256"):
                raise AIBenchmarkExecutionError("Benchmark plan source is stale: {}".format(source_path))
        expected_plan_id = _sha256_value({
            "policy_version": AI_BENCHMARK_EXECUTION_POLICY_VERSION,
            "backend_id": backend_id,
            "suite_id": suite_id,
            "run_id": plan.get("run_id"),
            "sources": plan.get("source_artifacts"),
        })
        if expected_plan_id != plan_id:
            raise AIBenchmarkExecutionError("Benchmark plan content identity is invalid.")
        if (
            plan.get("blockers") != ["fresh_benchmark_execution_authorization_missing"]
            or plan.get("automatic_submission_allowed") is not False
            or plan.get("model_output_execution_allowed") is not False
            or plan.get("training_allowed") is not False
            or plan.get("backend_activation_allowed") is not False
        ):
            raise AIBenchmarkExecutionError("Benchmark plan safety contract is invalid.")
        return plan, raw, path

    def authorize(
        self,
        backend_id: str,
        suite_id: str,
        plan_id: str,
        reviewer: str,
        confirmation: bool,
        now: Optional[datetime] = None,
    ) -> BenchmarkAuthorizationResult:
        if confirmation is not True:
            raise AIBenchmarkExecutionError(
                "Explicit --confirm-bounded-gpu-benchmark is required."
            )
        reviewer = _reviewer(reviewer)
        plan, plan_raw, _ = self._load_plan(backend_id, suite_id, plan_id)
        _, policy, policy_raw = self.policy(backend_id)
        now = now or datetime.now(timezone.utc)
        issued = _utc(now)
        expires = _utc(now + timedelta(seconds=policy["limits"]["authorization_validity_seconds"]))
        identity = {
            "policy_version": AI_BENCHMARK_EXECUTION_POLICY_VERSION,
            "backend_id": backend_id,
            "suite_id": suite_id,
            "plan_id": plan_id,
            "plan_sha256": _sha256_bytes(plan_raw),
            "policy_sha256": _sha256_bytes(policy_raw),
            "reviewer": reviewer,
            "issued_at_utc": issued,
            "expires_at_utc": expires,
        }
        authorization_id = _sha256_value(identity)
        value = {
            "schema_version": "1.0",
            "policy_version": AI_BENCHMARK_EXECUTION_POLICY_VERSION,
            "source_mode": "explicit_bounded_gpu_benchmark_authorization",
            "backend_id": backend_id,
            "suite_id": suite_id,
            "plan_id": plan_id,
            "authorization_id": authorization_id,
            "plan_sha256": _sha256_bytes(plan_raw),
            "policy_sha256": _sha256_bytes(policy_raw),
            "reviewer": reviewer,
            "issued_at_utc": issued,
            "expires_at_utc": expires,
            "authorized_scope": {
                "job_submissions": 1,
                "gpus": 1,
                "gpu_type": "gh200",
                "suite_id": suite_id,
                "data_classification": "non_sensitive_fixtures_only",
                "model_manifest_sha256": plan["model_manifest_sha256"],
                "requests_sha256": plan["requests_sha256"],
            },
            "model_download": False,
            "external_provider_access": False,
            "sensitive_project_data": False,
            "model_output_execution": False,
            "training": False,
            "automatic_registry_update": False,
            "backend_activation": False,
        }
        path = self.authorization_root / backend_id / (authorization_id + ".json")
        created = _write_immutable(path, _pretty_json(value))
        _write_immutable(path.with_suffix(".md"), _authorization_report(value))
        return BenchmarkAuthorizationResult(
            backend_id, suite_id, plan_id, authorization_id, path,
            "bounded_gpu_benchmark_authorized", expires, created,
        )

    def _load_authorization(
        self, backend_id: str, authorization_id: str
    ) -> tuple[dict[str, Any], bytes, Path]:
        backend_id = _identifier(backend_id, "backend_id")
        authorization_id = _digest(authorization_id, "authorization ID")
        path = self.authorization_root / backend_id / (authorization_id + ".json")
        value, raw = _read_json(path, "benchmark authorization")
        if (
            value.get("source_mode") != "explicit_bounded_gpu_benchmark_authorization"
            or value.get("backend_id") != backend_id
            or value.get("authorization_id") != authorization_id
        ):
            raise AIBenchmarkExecutionError("Benchmark authorization identity is invalid.")
        expected_id = _sha256_value({
            "policy_version": AI_BENCHMARK_EXECUTION_POLICY_VERSION,
            "backend_id": backend_id,
            "suite_id": value.get("suite_id"),
            "plan_id": value.get("plan_id"),
            "plan_sha256": value.get("plan_sha256"),
            "policy_sha256": value.get("policy_sha256"),
            "reviewer": value.get("reviewer"),
            "issued_at_utc": value.get("issued_at_utc"),
            "expires_at_utc": value.get("expires_at_utc"),
        })
        if expected_id != authorization_id:
            raise AIBenchmarkExecutionError("Benchmark authorization digest mismatch.")
        for field in (
            "model_download", "external_provider_access", "sensitive_project_data",
            "model_output_execution", "training", "automatic_registry_update",
            "backend_activation",
        ):
            if value.get(field) is not False:
                raise AIBenchmarkExecutionError("Benchmark authorization grants forbidden authority.")
        scope = value.get("authorized_scope")
        if not isinstance(scope, Mapping) or scope.get("job_submissions") != 1 or scope.get("gpus") != 1:
            raise AIBenchmarkExecutionError("Benchmark authorization scope is invalid.")
        return value, raw, path

    def launch(
        self,
        backend_id: str,
        authorization_id: str,
        runner: PythonRemoteRunner,
        confirmation: bool,
        now: Optional[datetime] = None,
    ) -> BenchmarkLaunchResult:
        if confirmation is not True:
            raise AIBenchmarkExecutionError(
                "Explicit --confirm-submit-bounded-gpu-benchmark is required."
            )
        authorization, authorization_raw, authorization_path = self._load_authorization(
            backend_id, authorization_id
        )
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None or now.astimezone(timezone.utc) > _parse_utc(authorization["expires_at_utc"]):
            raise AIBenchmarkExecutionError("Benchmark authorization has expired.")
        suite_id = str(authorization["suite_id"])
        plan, plan_raw, plan_path = self._load_plan(
            backend_id, suite_id, str(authorization["plan_id"])
        )
        if _sha256_bytes(plan_raw) != authorization["plan_sha256"]:
            raise AIBenchmarkExecutionError("Authorized benchmark plan changed.")
        _, policy, policy_raw = self.policy(backend_id)
        if _sha256_bytes(policy_raw) != authorization["policy_sha256"]:
            raise AIBenchmarkExecutionError("Benchmark policy changed after authorization.")
        if runner.host != policy["ssh_host"]:
            raise AIBenchmarkExecutionError("Benchmark SSH host does not match policy.")
        requests_path = self.run_root / backend_id / suite_id / plan["run_id"] / "requests.jsonl"
        requests_raw = requests_path.read_bytes()
        if _sha256_bytes(requests_raw) != plan["requests_sha256"]:
            raise AIBenchmarkExecutionError("Authorized fixture requests changed.")
        execution_id = _sha256_value({
            "policy_version": AI_BENCHMARK_EXECUTION_POLICY_VERSION,
            "authorization_id": authorization_id,
            "authorization_sha256": _sha256_bytes(authorization_raw),
        })
        control_path = str(PurePosixPath(policy["control_root"]) / execution_id)
        spec = {
            "schema_version": "1.0",
            "execution_id": execution_id,
            "backend_id": backend_id,
            "suite_id": suite_id,
            "run_id": plan["run_id"],
            "model_path": plan["model_path"],
            "model_revision": plan["model_revision"],
            "model_manifest_sha256": plan["model_manifest_sha256"],
            "requests_sha256": plan["requests_sha256"],
            "case_count": plan["case_count"],
            "dtype": "bfloat16",
            "maximum_model_length": policy["limits"]["maximum_model_length"],
            "maximum_completion_tokens": policy["limits"]["maximum_completion_tokens_per_case"],
            "maximum_response_bytes": policy["limits"]["maximum_response_bytes"],
            "gpu_memory_utilization": policy["limits"]["gpu_memory_utilization_basis_points"] / 10000.0,
            "seed": policy["execution_contract"]["fixed_seed"],
            "offline_provider_mode": True,
            "model_output_execution": False,
            "training": False,
        }
        config = {
            "execution_id": execution_id,
            "control_root": policy["control_root"],
            "control_path": control_path,
            "execution_spec": spec,
            "requests_jsonl": requests_raw.decode("utf-8"),
            "worker_source": _worker_source(),
            "shell_source": _shell_source(policy),
            "slurm": policy["slurm"],
        }
        result = dict(runner.run_python(
            _launch_program(config), timeout_seconds=policy["limits"]["launch_timeout_seconds"]
        ))
        if result.get("status") != "benchmark_submitted" or not re.fullmatch(
            r"[0-9]+", str(result.get("job_id") or "")
        ):
            raise AIBenchmarkExecutionError(
                "Remote benchmark submission failed: {}".format(result.get("error") or result)
            )
        if result.get("execution_id") != execution_id or result.get("control_path") != control_path:
            raise AIBenchmarkExecutionError("Remote benchmark launch identity mismatch.")
        launch = {
            "schema_version": "1.0",
            "policy_version": AI_BENCHMARK_EXECUTION_POLICY_VERSION,
            "source_mode": "explicit_authorized_bounded_gpu_benchmark_submission",
            "backend_id": backend_id,
            "suite_id": suite_id,
            "run_id": plan["run_id"],
            "plan_id": plan["plan_id"],
            "authorization_id": authorization_id,
            "execution_id": execution_id,
            "job_id": str(result["job_id"]),
            "control_path": control_path,
            "remote_status": "benchmark_submitted",
            "submitted_at_utc": _utc(now),
            "source_artifacts": [
                _source(authorization_path, authorization_raw, "benchmark_authorization"),
                _source(plan_path, plan_raw, "benchmark_plan"),
                _source(requests_path, requests_raw, "fixture_requests"),
            ],
            "slurm": dict(policy["slurm"]),
            "model_download": False,
            "external_provider_access": False,
            "model_output_execution": False,
            "training": False,
            "registry_update": False,
            "backend_activation": False,
        }
        root = self.execution_root / backend_id / suite_id / execution_id
        path = root / "launch.json"
        _write_immutable(path, _pretty_json(launch))
        return BenchmarkLaunchResult(
            backend_id, suite_id, execution_id, str(result["job_id"]),
            "benchmark_submitted", path,
        )

    def status(
        self,
        backend_id: str,
        authorization_id: str,
        runner: PythonRemoteRunner,
        stamp: Optional[str] = None,
    ) -> BenchmarkStatusResult:
        authorization, _, _ = self._load_authorization(backend_id, authorization_id)
        suite_id = str(authorization["suite_id"])
        execution_id = _sha256_value({
            "policy_version": AI_BENCHMARK_EXECUTION_POLICY_VERSION,
            "authorization_id": authorization_id,
            "authorization_sha256": _sha256_bytes(
                (self.authorization_root / backend_id / (authorization_id + ".json")).read_bytes()
            ),
        })
        root = self.execution_root / backend_id / suite_id / execution_id
        launch, launch_raw = _read_json(root / "launch.json", "benchmark launch")
        _, policy, _ = self.policy(backend_id)
        if runner.host != policy["ssh_host"]:
            raise AIBenchmarkExecutionError("Benchmark SSH host does not match policy.")
        config = {
            "execution_id": execution_id,
            "job_id": str(launch["job_id"]),
            "control_path": launch["control_path"],
            "maximum_response_bytes": policy["limits"]["maximum_response_bytes"],
        }
        remote = dict(runner.run_python(
            _status_program(config), timeout_seconds=policy["limits"]["status_timeout_seconds"]
        ))
        status = str(remote.get("status") or "benchmark_status_requires_review")
        if status not in TERMINAL_REMOTE_STATES | {
            "benchmark_running", "benchmark_queued_or_running",
            "benchmark_state_requires_review", "benchmark_status_requires_review",
        }:
            status = "benchmark_status_requires_review"
        responses_path: Optional[Path] = None
        response_text = remote.pop("responses_jsonl", None)
        if status == "benchmark_completed_unscored":
            if not isinstance(response_text, str):
                raise AIBenchmarkExecutionError("Completed benchmark has no response evidence.")
            response_raw = response_text.encode("utf-8")
            if (
                len(response_raw) > policy["limits"]["maximum_response_bytes"]
                or _sha256_bytes(response_raw) != remote.get("responses_sha256")
            ):
                raise AIBenchmarkExecutionError("Remote response evidence failed its bounded digest check.")
            responses_path = root / "responses.jsonl"
            _write_immutable(responses_path, response_text)
        stamp = stamp or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        if not re.fullmatch(r"[0-9]{8}T[0-9]{12}Z", stamp):
            raise AIBenchmarkExecutionError("Invalid status evidence stamp.")
        observation = {
            "schema_version": "1.0",
            "policy_version": AI_BENCHMARK_EXECUTION_POLICY_VERSION,
            "source_mode": "bounded_read_only_gpu_benchmark_status_observation",
            "backend_id": backend_id,
            "suite_id": suite_id,
            "execution_id": execution_id,
            "job_id": str(launch["job_id"]),
            "observation_id": stamp,
            "status": status,
            "remote_observation": remote,
            "launch_sha256": _sha256_bytes(launch_raw),
            "remote_writes": False,
            "model_reads": False,
            "gpu_allocation": False,
            "inference": False,
            "model_output_execution": False,
            "training": False,
            "registry_update": False,
            "backend_activation": False,
        }
        path = root / "status" / (stamp + ".json")
        _write_immutable(path, _pretty_json(observation))
        return BenchmarkStatusResult(
            backend_id, suite_id, execution_id, str(launch["job_id"]),
            status, path, responses_path,
        )

    def evaluate(self, backend_id: str, authorization_id: str):
        authorization, _, _ = self._load_authorization(backend_id, authorization_id)
        suite_id = str(authorization["suite_id"])
        plan, _, _ = self._load_plan(backend_id, suite_id, str(authorization["plan_id"]))
        execution_id = _sha256_value({
            "policy_version": AI_BENCHMARK_EXECUTION_POLICY_VERSION,
            "authorization_id": authorization_id,
            "authorization_sha256": _sha256_bytes(
                (self.authorization_root / backend_id / (authorization_id + ".json")).read_bytes()
            ),
        })
        responses = self.execution_root / backend_id / suite_id / execution_id / "responses.jsonl"
        if not responses.is_file():
            raise AIBenchmarkExecutionError("Completed response evidence is not available locally.")
        run_dir = self.run_root / backend_id / suite_id / plan["run_id"]
        return AIEvaluationCore(
            registry=self.registry,
            run_root=self.run_root,
            evaluation_root=self.evaluation_root,
        ).evaluate(run_dir, responses)
