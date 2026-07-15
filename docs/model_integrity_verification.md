# Staged Model Integrity Verification v1

GenomeAgent v0.19 verifies a downloaded model without installing, publishing or
running it. Metadata observation and content hashing are separate authority layers.

## Read-only inventory

The collector connects through the registered `roihu` CPU alias and observes:

- the exact staging directory and absent final installation path;
- every non-cache regular-file relative path and size;
- symlinks, special entries and bounded traversal health;
- Hugging Face `.cache` metadata counts and bytes separately;
- system Python, Slurm commands and availability of the `small` CPU partition.

It does not open model files, calculate hashes, write remotely or submit a job.
Evidence expires after 30 minutes and changes to any bound local artifact make it
stale.

## Explicit hashing authorization

Authorization is valid for at most ten minutes and binds the direct immutable
inventory snapshot, reconstructed state, acquisition bundle, completed-download
evidence, policy, researcher and exact resources. It permits one job only:

| Resource | Request |
|---|---:|
| Partition | `small` |
| CPUs | 1 |
| Memory | 4 GiB |
| Time | 02:00:00 |
| GPUs | 0 |

The submission launcher records intent before calling `sbatch`. If the SSH connection
is lost between intent and receipt, GenomeAgent refuses automatic resubmission rather
than risking a duplicate job.

## Worker validation

The worker repeats inventory validation on the compute node, rejects every staged
symlink or special entry, opens approved files with `O_NOFOLLOW`, hashes in 8 MiB
chunks and verifies that device, inode, size and modification time remain unchanged
during each read. It computes local SHA-256 for every approved file and compares
provider LFS SHA-256 where available.

The manifest candidate is written to the verification control directory, not the
model directory. Successful verification is
`verified_ready_for_publication_review`.

## Usage

```bash
python3 scripts/model_integrity_verification.py collect roihu_qwen3_coder \
  --bundle-id <bundle-id> \
  --download-execution-id <download-execution-id>
python3 scripts/model_integrity_verification.py ingest roihu_qwen3_coder \
  --bundle-id <bundle-id>

python3 scripts/model_integrity_verification.py authorize roihu_qwen3_coder \
  --bundle-id <bundle-id> \
  --download-execution-id <download-execution-id> \
  --inventory-evidence-id <evidence-id> \
  --reviewer <researcher-id> \
  --confirm-staging-hash-verification

python3 scripts/model_integrity_verification.py launch roihu_qwen3_coder \
  --bundle-id <bundle-id> \
  --authorization-id <authorization-id> \
  --confirm-submit-hash-verification

python3 scripts/model_integrity_verification.py status roihu_qwen3_coder \
  --bundle-id <bundle-id> \
  --authorization-id <authorization-id>
```

## Deliberate stopping point

Neither collection, authorization, submission nor status removes `.cache`, writes the
final manifest into staging, renames the directory, edits the backend registry,
allocates a GPU or runs inference. Those operations require a separate publication
and activation component after successful verification evidence is reviewed.
