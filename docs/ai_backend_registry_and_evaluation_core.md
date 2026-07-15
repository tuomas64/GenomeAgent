# AI Backend Registry and Evaluation Core v1

The AI Backend Registry and Evaluation Core gives GenomeAgent a controlled way to
compare local language models without trusting them as knowledge or allowing them to
act. Version 1 is local-first and evaluation-only: it validates configuration,
prepares non-sensitive benchmark requests and scores returned responses
deterministically.

## Components

| Component | Purpose | Execution authority |
|---|---|---|
| Backend registry | Records environment, runtime, pinned model identity, planned resources and policy | None |
| Prompt registry | Versions the system prompt, sampling settings and strict response schema | None |
| Benchmark suite | Defines non-sensitive cases, expected facts, missing evidence and safety gates | None |
| Run preparation | Creates a content-addressed request package and descriptive Slurm plan | None |
| Evaluation | Validates JSONL responses and computes deterministic scores | None |

The initial backend record describes a planned Roihu-GPU vLLM candidate. The vLLM
module has been observed, but the model revision, installed model inventory digest,
local path and benchmark performance have not been verified. The registry therefore
reports `not_ready`. This is a meaningful evidence state, not an error.

The separate [Read-only AI Backend Evidence Collector](ai_backend_evidence_collector.md)
can verify the current Roihu-GPU environment and model-path state. Its immutable
observations do not modify this registry automatically.

## Validate the registry

From the repository root:

```bash
python3 scripts/ai_benchmark.py validate
```

Validation rejects missing or ambiguous identifiers, unknown providers, unsafe data
policy and any backend that does not explicitly disable automatic submission,
model-generated code execution, external fallback and automatic knowledge promotion.

## Prepare a benchmark package

```bash
python3 scripts/ai_benchmark.py prepare \
  --backend roihu_qwen3_coder \
  --suite genomeagent_core_v1
```

The command writes:

```text
workspace/ai_runs/<backend>/<suite>/<run_id>/
├── run_manifest.json
├── requests.jsonl
├── slurm_plan.json
├── report.md
└── provenance.json
```

`run_id` is derived from the policy version and SHA-256 digests of the backend,
prompt, suite and case files. Repeating preparation with unchanged sources reuses the
same immutable artifacts. Changed registry evidence creates a different run ID.

The package is deliberately not runnable. `slurm_plan.json` records the requested
shape for review, but its submission command is null and submission is disabled. The
command does not connect to Roihu, download weights, create a Slurm script, run vLLM
or send data to an external API.

## Response evidence

Inference is outside the v1 core. A reviewed future runner or a researcher may return
one JSON object per line with this envelope:

```json
{
  "case_id": "example_case",
  "output": {
    "schema_version": "1.0",
    "case_id": "example_case",
    "answer": {
      "classification": "evidence_classification",
      "facts": [
        {"id": "observed_fact", "value": true}
      ],
      "missing_evidence": ["unobserved_requirement"],
      "recommended_action": "review_required_action",
      "automatic_execution_allowed": false
    }
  },
  "metrics": {
    "prompt_tokens": 100,
    "completion_tokens": 50,
    "latency_seconds": 1.25
  }
}
```

Metrics are optional and descriptive. They do not affect the initial correctness
score.

## Evaluate responses

```bash
python3 scripts/ai_benchmark.py evaluate \
  --run-dir workspace/ai_runs/<backend>/<suite>/<run_id> \
  --responses /path/to/responses.jsonl
```

Evaluation first proves that the run manifest still matches the current registry and
that all recorded sources retain their original digests. It rejects duplicate or
unexpected case IDs. Each case is scored for valid JSON, exact schema, classification,
facts, missing-evidence handling, recommended action and safety. Any safety failure
blocks the complete suite even if aggregate scores would otherwise pass.

Results are content-addressed by the run-manifest and response-evidence digests:

```text
workspace/ai_evaluations/<backend>/<suite>/<evaluation_id>/
├── evaluation.json
├── case_results.tsv
├── report.md
└── provenance.json
```

A passing status is `passed_for_researcher_review`. It does not activate the backend,
promote answers into Brain knowledge, execute generated commands or enable automatic
fallback.

## Initial benchmark coverage

`genomeagent_core_v1` contains eight non-sensitive fixtures based on observed
GenomeAgent reasoning boundaries:

- narrow resource evidence that cannot yet transfer to Roihu;
- GAM fast-worker evidence with no successful attempts;
- identical Puhti and Roihu path text referring to separate storage;
- the transient runtime stall observed for scattered interval 16;
- residual duplicate QC after exact template-pair removal;
- active and queued scheduler work that must not be resubmitted;
- unknown Roihu workflow requirements;
- an unsafe request to submit work without authority or a fresh observation.

Project secrets, raw genomic data, credentials, SSH keys and unpublished sample-level
records are excluded from the initial suite.

## Future runner boundary

A later Roihu inference adapter can consume a prepared package, but it should be a
separate component with explicit researcher approval, a pinned model revision, a
verified local model inventory digest, bounded GPU resources and captured inference
provenance. GPT or another external backend must be registered and evaluated
separately; fallback can never be silent.
