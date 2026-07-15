# Controlled Model Publication v1

GenomeAgent v0.20 publishes a verified staged model without activating or running it.
Publication remains a separate authority layer from download, integrity verification,
backend registry updates and GPU inference.

## Fresh read-only preflight

The preflight connects through the registered `roihu` CPU alias and confirms:

- the exact staging directory still exists and the final installation path is absent;
- staging and the final parent are on the same filesystem;
- staged relative paths and sizes still match the approved bundle;
- no staged symlink or special file is present;
- the successful verification result and manifest candidate are intact and bound to
  the requested verification ID;
- the bounded transfer-cache inventory and serial CPU publication context are known.

It reads only filesystem metadata plus the small verification result and manifest
candidate. It does not read model contents, write remotely, submit a job or publish.
Evidence expires after 30 minutes.

## Explicit publication authorization

Authorization binds the direct preflight snapshot, reconstructed state, acquisition
bundle, successful verification evidence, manifest digest, policy, researcher and
exact Slurm request. It expires after at most ten minutes and requires two separate
confirmations:

- publish the exact model by an atomic directory rename;
- remove only the registered `.cache` transfer directory after re-verification.

An existing final path, a changed source artifact, stale evidence or a different host
blocks submission. Submission intent is recorded before the remote `sbatch` call;
ambiguous transport loss cannot cause an automatic duplicate submission.

## Publication worker

The authorized job uses Roihu's `small` CPU partition with one CPU, 4 GiB memory and a
two-hour limit. Immediately before mutation it repeats the exact path-and-size scan and
computes SHA-256 for every approved regular file, comparing each value with the
verified manifest. Files are opened with `O_NOFOLLOW` and changes during reading are
detected from device, inode, size and modification time.

Only after every hash matches does the worker:

1. validate and remove the exact symlink-free `.cache` transfer directory;
2. write and fsync `.genomeagent-model-manifest.json` inside staging;
3. recheck that the final path is absent and the filesystem is unchanged;
4. atomically rename the exact staging directory to the registered installation path.

The worker never overwrites an installation, publishes across filesystems, allocates a
GPU, runs inference or training, edits the backend registry or activates the backend.

## Usage

```bash
python3 scripts/model_publication.py collect roihu_qwen3_coder \
  --bundle-id <bundle-id> \
  --verification-id <verification-id>

python3 scripts/model_publication.py ingest roihu_qwen3_coder \
  --bundle-id <bundle-id>

python3 scripts/model_publication.py authorize roihu_qwen3_coder \
  --bundle-id <bundle-id> \
  --verification-id <verification-id> \
  --publication-evidence-id <fresh-evidence-id> \
  --reviewer <researcher-id> \
  --confirm-atomic-model-publication \
  --confirm-remove-download-cache

python3 scripts/model_publication.py launch roihu_qwen3_coder \
  --bundle-id <bundle-id> \
  --authorization-id <authorization-id> \
  --confirm-submit-model-publication

python3 scripts/model_publication.py status roihu_qwen3_coder \
  --bundle-id <bundle-id> \
  --authorization-id <authorization-id>
```

The terminal success state is
`published_ready_for_installed_model_evidence`. The status observation then confirms
that staging is absent, the final directory and installed manifest exist, the transfer
cache is absent and exact installed paths and sizes match the verified files.

## Deliberate stopping point

Publication does not make the backend trusted for inference. The next stages are a
fresh read-only installed-model observation, explicit backend-registry review and a
bounded GH200 inference benchmark. Training and fine-tuning remain out of scope.
