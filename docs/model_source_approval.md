# Explicit Model Source and License Approval Core v1

The approval core records a researcher's review of one exact model-source observation
and applies only the verified source values to the versioned acquisition specification.
It is the controlled boundary between read-only source evidence and authoritative
acquisition configuration.

## Required explicit inputs

Approval requires all of the following on the command line:

- the registered backend;
- the latest current source-evidence ID;
- a safe researcher identifier;
- the exact observed license identifier;
- `--confirm-license-review`.

For example:

```bash
python3 scripts/model_source_metadata.py approve roihu_qwen3_coder \
  --evidence-id 20260715T094807016620Z \
  --reviewer tuomas64 \
  --accept-license Apache-2.0 \
  --confirm-license-review
```

The command deterministically rebuilds current source state before applying anything.
It blocks stale evidence, a non-latest evidence ID, a license mismatch, an unreviewable
source, a rejected license, a symlinked specification, unsafe provenance or changed
configuration.

## Narrow configuration change

Only `config/ai/acquisition/<backend_id>.json` is updated. The source object receives:

- immutable resolved revision;
- canonical source-inventory SHA-256;
- complete source byte size;
- exact license identifier and `reviewed_accepted` status;
- structured license approval provenance.

The provenance contains the approval ID, evidence ID and SHA-256, reviewer, UTC
timestamp, reviewed license URL, license identifier and resolved revision. The entire
candidate acquisition specification is validated in a temporary directory before an
atomic local replacement. All target, storage, representation, integrity, approval and
safety policies remain unchanged.

An immutable companion record is written under:

```text
workspace/model_source_approvals/<backend_id>/<approval_id>.json
workspace/model_source_approvals/<backend_id>/<approval_id>.md
```

The approval ID is content-addressed from the evidence, reviewer, license, review URL
and approved values. Repeating the same command after successful application returns
`approved_source_identity_already_recorded` without changing the specification.

## Authority boundary

The core explicitly disables:

- provider authentication and repository or weight download;
- Roihu access or remote writes;
- Slurm submission and GPU allocation;
- backend registry update or activation;
- inference and training.

After approval, rerun:

```bash
python3 scripts/model_acquisition.py plan roihu_qwen3_coder
```

If current environment capacity evidence is sufficient, the resulting status can
advance to `ready_for_researcher_acquisition_review`. That remains a non-executable
plan; model acquisition is a separate future component.
