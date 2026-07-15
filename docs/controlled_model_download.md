# Controlled Public Model Staging Download v1

This core is GenomeAgent's first intentionally mutating AI-backend action. Its scope is
one exact public-model download into one exact hidden staging directory. It is not
model installation or execution.

## Authorization

Authorization requires:

- the exact current acquisition bundle and all four bundle-artifact digests;
- a current backend and download policy;
- one preflight observation whose direct immutable snapshot and derived state agree;
- preflight status `ready_for_execution_authorization_review`;
- an expiry time that has not passed;
- an explicit researcher identifier and `--confirm-public-model-download`.

Authorization is content-addressed and valid for at most ten minutes. Configuration,
bundle or evidence changes block launch.

## Remote mutation allow-list

The authorized launcher may only:

1. create a control directory under the configured project scratch root;
2. write execution metadata and a bounded log there;
3. create missing confined model-parent directories;
4. create the exact bundle staging directory;
5. call `huggingface_hub.snapshot_download` for the approved repository and immutable
   revision with `token=False` and at most two workers.

Existing symlinked or non-directory path components, a pre-existing staging path, or a
pre-existing final installation path reject launch. The background worker removes
provider-token variables and never receives project data.

## Usage

First collect and ingest a fresh v0.17 preflight. Then:

```bash
python3 scripts/model_acquisition_download.py authorize roihu_qwen3_coder \
  --bundle-id <bundle-id> \
  --preflight-evidence-id <evidence-id> \
  --reviewer <researcher-id> \
  --confirm-public-model-download
```

Launch before the printed authorization expiry:

```bash
python3 scripts/model_acquisition_download.py launch roihu_qwen3_coder \
  --bundle-id <bundle-id> \
  --authorization-id <authorization-id> \
  --confirm-execute-approved-download
```

The SSH command returns after starting the background transfer. Monitor it with:

```bash
python3 scripts/model_acquisition_download.py status roihu_qwen3_coder \
  --bundle-id <bundle-id> \
  --authorization-id <authorization-id>
```

Expected terminal transfer states are `download_completed_unverified` and
`download_failed`. The status command is read-only and reads only small control JSON
and a bounded log tail, never model files.

## Deliberate stopping point

The download worker performs no path/size acceptance, SHA-256, provider-digest
comparison, cleanup, manifest creation or publication. Those operations can be more
computationally and I/O intensive and belong in a separately authorized Slurm
verification stage. The final model path remains absent after v0.18 completes.
