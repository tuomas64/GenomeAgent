# Installed Model Evidence and Controlled Backend Registration v1

GenomeAgent v0.21 connects a successfully published model installation to the AI
backend registry without rerunning a 61 GB hash job and without treating the mere
presence of a manifest as proof of integrity.

## Evidence chain

The collector requires all of the following to agree:

- the approved acquisition specification and immutable model revision;
- the exact acquisition bundle;
- the successful full-file integrity verification status;
- the atomic publication launch and terminal publication status;
- a fresh read-only observation of the final installation directory; and
- the SHA-256 and contents of the installed GenomeAgent manifest.

It recursively compares installed relative paths and sizes with the previously
verified file inventory. It reads only the small installed manifest. Model-weight
contents are not opened or hashed, because the installed-manifest digest is bound to
the full SHA-256 verification and the publication worker's immediate re-verification.
Any changed manifest, unexpected file, symlink, special entry, remaining `.cache`,
path mismatch or changed local source artifact blocks registration.

## Collection and deterministic replay

```bash
python3 scripts/model_registration.py collect roihu_qwen3_coder \
  --bundle-id <bundle-id> \
  --publication-id <publication-id>

python3 scripts/model_registration.py ingest roihu_qwen3_coder
```

Collection uses the registered `roihu` CPU login alias. It performs no remote write,
large-file hash, Slurm submission, GPU allocation, inference or training. Ingestion is
entirely local and writes a review-only proposal under
`workspace/installed_model_state/<backend>/`.

## Explicit scoped registration

After reviewing the proposal, the researcher may approve the exact evidence:

```bash
python3 scripts/model_registration.py approve roihu_qwen3_coder \
  --evidence-id <installed-model-evidence-id> \
  --reviewer <researcher-id> \
  --confirm-register-verified-installation
```

This is the only mutation in v0.21. It atomically updates only the backend's:

- pinned model revision;
- verified inventory digest and its explicit digest semantics;
- installed-manifest digest and installation status;
- publication, verification and researcher provenance; and
- candidate status, while keeping benchmark status at `not_run`.

The `model.weights_sha256` value is the SHA-256 of the verified model-candidate
manifest. That manifest commits to the path, size and locally computed SHA-256 of
every approved regular file; it is not represented as a hash of concatenated model
weights.

After registration, deterministic registry validation must report
`inference_not_benchmarked` as the only model-readiness blocker. Registration does
not activate the backend or authorize a GPU job. A fresh normal backend observation
must confirm that the installed-manifest SHA-256 still matches the registered value
before a separately approved bounded inference benchmark can be prepared.

## Safety properties

- A manifest that merely exists is never trusted.
- Old evidence becomes stale when any bound source artifact changes.
- Approval is content-addressed, tamper-evident and idempotent.
- Automatic registry updates and backend activation remain disabled.
- Project data is not supplied to the model.
- GPU inference and model-generated code execution remain separate gates.
