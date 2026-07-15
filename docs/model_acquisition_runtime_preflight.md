# Read-only Model Acquisition Runtime Preflight v1

The preflight resolves environment facts for one exact controlled acquisition bundle
without performing acquisition. It uses one bounded SSH session and saves an immutable
observation locally.

## What it verifies

- Roihu host identity and `aarch64` architecture;
- `python-vllm/0.19.1` availability and exact vLLM package version;
- importability of `huggingface_hub.snapshot_download`, without calling it;
- the registered public inbound-only, no-token transfer context;
- `sbatch`, `sinfo`, `gputest` and GH200 visibility for the later smoke test;
- authoritative `csc-workspaces` quota and the plan's full working-space requirement;
- installation and hidden staging paths are both absent, including broken symlinks;
- existing path components contain no symlink and both destinations resolve to the
  same filesystem for future atomic publication.

The collector reads only path metadata and small command output. It does not enumerate
the future model directory, read model files, contact Hugging Face, create a directory,
write remotely, hash weights, submit Slurm, allocate a GPU or run inference.

## Usage

```bash
python3 scripts/model_acquisition_preflight.py collect roihu_qwen3_coder \
  --bundle-id <reviewed-bundle-id>

python3 scripts/model_acquisition_preflight.py ingest roihu_qwen3_coder \
  --bundle-id <reviewed-bundle-id>
```

Immutable observations are stored under:

```text
workspace/model_acquisition_preflight_evidence/<backend>/<bundle>/
```

Current deterministic state is rebuilt under:

```text
workspace/model_acquisition_preflight_state/<backend>/<bundle>/
```

## Freshness and readiness

Evidence is valid for 30 minutes. Changes to the backend configuration, runtime policy,
bundle artifacts or acquisition plan also make it stale. When every environment check
passes, status becomes `ready_for_execution_authorization_review`, but the remaining
blocker is still `fresh_execution_authorization_missing`.

A later component must bind a new explicit researcher authorization to the exact
bundle and unexpired evidence digest before generating or running any acquisition
worker.
