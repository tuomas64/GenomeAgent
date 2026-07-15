# Controlled Bounded AI Benchmark Execution

GenomeAgent v0.22 turns the existing non-sensitive evaluation suite into one
controlled Roihu GPU inference job. It does not train or activate the model.

## Lifecycle

1. `prepare` rebuilds the immutable eight-case run package and binds it to fresh
   backend evidence, the registered installed-manifest digest, the prompt, suite,
   cases and execution policy.
2. `authorize` records a named researcher's approval for that exact plan. The
   authorization expires after 15 minutes.
3. `launch` may submit exactly one `gputest` job using one GH200, 16 CPUs, 120 GB
   host memory and at most 15 minutes, matching Roihu's `gputest` limit.
4. `status` performs a bounded read-only observation and imports the small JSONL
   response evidence only after the worker reports completion.
5. `evaluate` applies the existing deterministic schema, factual, missing-evidence,
   recommended-action and safety scoring policy locally.

Preparation:

```bash
python3 scripts/ai_benchmark_execution.py prepare \
  roihu_qwen3_coder \
  --suite genomeagent_core_v1
```

The resulting plan ID is required for explicit authorization:

```bash
python3 scripts/ai_benchmark_execution.py authorize \
  roihu_qwen3_coder \
  --suite genomeagent_core_v1 \
  --plan-id PLAN_ID \
  --reviewer tuomas64 \
  --confirm-bounded-gpu-benchmark
```

The fresh authorization ID is then required for submission:

```bash
python3 scripts/ai_benchmark_execution.py launch \
  roihu_qwen3_coder \
  --authorization-id AUTHORIZATION_ID \
  --confirm-submit-bounded-gpu-benchmark
```

Observe and score without further model execution:

```bash
python3 scripts/ai_benchmark_execution.py status \
  roihu_qwen3_coder \
  --authorization-id AUTHORIZATION_ID

python3 scripts/ai_benchmark_execution.py evaluate \
  roihu_qwen3_coder \
  --authorization-id AUTHORIZATION_ID
```

## Execution boundary

The worker loads only the verified local installation. Hugging Face Hub,
Transformers and datasets offline modes are forced before vLLM is imported. The
job receives only versioned non-sensitive fixtures, uses temperature zero and a
fixed seed, caps each completion at 1,200 tokens and caps the combined response
artifact at 2 MiB.

Model output is stored as untrusted text. It is never interpreted by a shell,
executed as Python, submitted to Slurm, promoted into Brain knowledge or used to
edit the registry. A passing deterministic evaluation remains evidence for
researcher review; it does not activate the backend.
