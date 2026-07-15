# Controlled Model Acquisition Approval and Bundle Core v1

This core advances a review-ready pinned model plan to an explicitly approved,
content-addressed execution contract while retaining a separate gate before any remote
action. It is the first acquisition-facing boundary of the GenomeAgent Execution
Engine, but it does not yet execute acquisition.

## Two local stages

An approval is bound to one exact current plan ID, every plan-artifact SHA-256, the
approved source-evidence ID and digest, and the researcher identifier. The approval
authorizes execution-bundle preparation only. Repeating the same approval is
idempotent.

Bundle preparation replays the current plan, approval, acquisition specification and
approved public source inventory. It writes a data-only contract containing:

- the immutable repository revision;
- expected relative paths and sizes;
- available provider Git LFS SHA-256 values;
- provenance-only Git blob and Xet identifiers;
- a same-filesystem hidden staging path and final installation path;
- mandatory local SHA-256 computation for every regular file;
- exact inventory, symlink, manifest and atomic-publication requirements.

The bundle deliberately contains no shell command, Python worker, credential, token
or Slurm submission. Its initial status is
`environment_execution_preflight_required`.

## Provider digest boundary

The approved source inventory is a digest of normalized provider metadata, not a
digest of all downloaded bytes. Git LFS records commonly supply a content SHA-256 for
large weight files. Ordinary Git objects commonly supply a Git blob ID, which is not
treated as a raw-file SHA-256.

Before publication, a future approved worker must therefore:

1. match every downloaded path and size to the approved source inventory;
2. match every available provider LFS SHA-256;
3. compute and record SHA-256 for every regular downloaded file;
4. reject symlinks and unexpected files;
5. write a complete local integrity manifest;
6. publish by an atomic rename on the destination filesystem.

Files without provider SHA-256 remain verifiable through the immutable revision,
approved path/size inventory and the newly recorded local SHA-256, but GenomeAgent
does not falsely label that local digest as provider-confirmed.

## Usage

After rerunning the acquisition planner and reviewing its new policy-v1.1 plan, record
preparation approval:

```bash
python3 scripts/model_acquisition_control.py approve roihu_qwen3_coder \
  --plan-id <reviewed-plan-id> \
  --reviewer <researcher-id> \
  --confirm-execution-preparation
```

Then prepare the data-only bundle:

```bash
python3 scripts/model_acquisition_control.py prepare roihu_qwen3_coder \
  --plan-id <reviewed-plan-id> \
  --approval-id <approval-id>
```

Artifacts are written under:

```text
workspace/model_acquisition_approvals/<backend_id>/
workspace/model_acquisition_bundles/<backend_id>/<bundle_id>/
```

## Remaining execution gates

The prepared bundle reports four blockers:

- `acquisition_runtime_unverified`;
- `transfer_execution_context_unregistered`;
- `remote_target_state_unverified`;
- `fresh_execution_authorization_missing`.

The next component is a read-only acquisition-runtime evidence collector. It must
verify the exact download library/runtime, an appropriate non-login execution context,
fresh project quota, target and staging absence, and destination-filesystem identity.
Only after that evidence is reviewed may a separate execution authorization create or
submit a bounded acquisition worker.

## Safety boundary

Approval and preparation perform no SSH connection, provider request, remote write,
model download, large-file hash, job submission, GPU allocation, publication,
registry update, backend activation, inference or training.
