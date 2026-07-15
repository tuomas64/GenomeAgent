# GenomeAgent

**An AI assistant for computational genomics that continuously learns research projects, adapts to new computing environments, and uses that understanding to reason about, improve, and eventually execute computational workflows.**

GenomeAgent is an open-source framework for AI-assisted computational genomics that builds an evolving understanding of research projects through continuous learning, enabling more efficient, transparent, and reproducible computational research.

<p align="center">
  <img src="docs/images/genomeagent_architecture.svg"
       alt="GenomeAgent architecture"
       width="900">
</p>

<p align="center">
  <em>GenomeAgent continuously explores research projects, builds persistent project knowledge, reasons about computational workflows, and assists researchers with reproducible computational genomics.</em>
</p>

---

# Design Philosophy

Before an AI assistant can effectively contribute to a research project, it must first understand that project.

GenomeAgent therefore follows a simple cognitive cycle:

**Observe → Learn → Reason → Act**

Current development focuses on the first three stages while maintaining full transparency and researcher oversight.

---

# Architecture

The architecture consists of three major components.

### Explorer

The **Explorer** systematically observes research projects and their computing environments. It collects information about project structure, software, workflows, storage, and HPC resources without modifying the project.

The reusable **Task Scanner Core** extends the Explorer with focused, knowledge-guided observation of active workflows. Task profiles deterministically compare manifests, expected outputs, scheduler state and validation evidence while remaining read-only. Current profiles monitor GAM duplicate removal across the 458-sample *Fragaria vesca* graph-mapping cohort and interval-scattered GATK joint calling across the 455-sample linear-reference cohort.

The **Task State Bridge** replays those immutable observations into canonical current state, transition history, provenance and safety-gated recommendations. It keeps operational state separate from curated scientific memory and provides a trustworthy input boundary for the future Execution Engine.

The **Resource Evidence and Learning Core** records bounded Slurm accounting for explicit jobs and deterministically learns empirical runtime, peak-memory and efficiency profiles. It preserves failed attempts as censored evidence, flags cross-attempt anomalies and produces proposal-only recommendations that cannot change scheduler resources or execute jobs.

The **AI Backend Registry and Evaluation Core** versions candidate inference backends, prompts and non-sensitive benchmark suites. It prepares content-addressed run packages and scores returned model responses deterministically while keeping model download, Slurm submission, generated-code execution, external fallback and Brain knowledge promotion disabled.

The **Read-only AI Backend Evidence Collector** checks a registered backend against its actual HPC environment using one bounded SSH observation. It verifies architecture, module metadata, scheduler partition metadata, storage and a shallow model-path inventory, then rebuilds current readiness locally without downloading models, allocating a GPU or editing the registry.

The **Pinned Model Acquisition Planning Core** converts that evidence into an immutable,
review-only source identity, storage and integrity plan. Unknown model revisions,
inventories, sizes and licenses remain blocking unknowns; the planner does not contact a
provider, download weights or grant execution authority.

The **Read-only Model Source Metadata Collector** resolves a registered public model's
symbolic revision to an immutable commit and records its provider inventory, byte size,
license metadata and canonical inventory digest. It performs exactly two bounded public
metadata requests and produces a review-only acquisition-specification proposal; it
does not download repository files, accept a license or update configuration.

The **Explicit Model Source and License Approval Core** converts a researcher's
review into an immutable, evidence-bound approval record. It applies only the verified
source identity and structured licence provenance to the acquisition specification;
all remote, download, scheduler, GPU, registry and activation authority remains off.

### GenomeAgent Brain

The **GenomeAgent Brain** is the cognitive center of the system. Brain v2 promotes provenance-backed operational facts into immutable, versioned knowledge and keeps AI-derived interpretations in a separate researcher-review queue. Versioned workflow templates preserve portable workflow contracts, while the Workflow Transfer Core checks target software, environment bindings and resource gates without executing anything.

Its core functions include:

* Learn project structure
* Understand computational workflows
* Reason about analyses and alternatives
* Design improved workflows
* Adapt to new computing environments

### Execution Engine *(future)*

The future **Execution Engine** will safely perform computational analyses under researcher supervision while maintaining reproducibility and complete provenance.

---

# Current Capabilities

| Capability                  | Status                    |
| --------------------------- | ------------------------- |
| Project Exploration         | ✅                         |
| HPC Environment Discovery   | ✅                         |
| GenomeAgent Brain           | ✅                         |
| Workflow Understanding      | ✅                         |
| Read-only Task Monitoring   | ✅ Initial reusable core   |
| Operational State Bridge    | ✅ Initial reusable core   |
| Resource Evidence Learning  | ✅ Initial reusable core   |
| Brain v2 Knowledge Promotion| ✅ Initial reusable core   |
| Workflow Transfer Planning  | ✅ Initial reusable core   |
| AI Backend Evaluation       | ✅ Initial reusable core   |
| AI Backend Evidence         | ✅ Initial reusable core   |
| Model Acquisition Planning  | ✅ Initial reusable core   |
| Model Source Metadata       | ✅ Initial reusable core   |
| Model Source Approval       | ✅ Initial reusable core   |
| Continuous Project Learning | ✅ Initial implementation  |
| AI-assisted Workflow Design | 🚧 Initial implementation |
| Safe Execution Engine       | 📋 Planned                |

---

# Task Scanner

Run the read-only GAM duplicate-removal profile from the GenomeAgent repository on the Mac:

```bash
python3 scripts/task_scan.py gam_deduplication
```

Monitor the 250 kb interval-scattered GenotypeGVCFs workflow with:

```bash
python3 scripts/task_scan.py scattered_joint_calling
```

The scanner connects through the `puhti` SSH alias and writes timestamped local reports under `workspace/task_scans/<profile>/`. It does not submit jobs or read complete GAM or VCF contents. See the [Task Scanner Core documentation](docs/task_scanner_core.md) for configuration, outputs and safety boundaries.

Ingest completed scan bundles into canonical operational knowledge with:

```bash
python3 scripts/task_state.py ingest scattered_joint_calling
python3 scripts/task_state.py ingest gam_deduplication
```

The bridge writes local state under `workspace/task_state/<profile>/` and never connects to Puhti or executes recommendations. See the [Task State Bridge documentation](docs/task_state_bridge.md).

Collect bounded read-only Slurm evidence for explicit completed or failed jobs, then rebuild deterministic resource knowledge with:

```bash
python3 scripts/task_resources.py collect scattered_joint_calling \
  --job-id 35442372_16 \
  --job-id 35452993_16 \
  --profile-key scattered_genotypegvcfs_250kb \
  --unit 35442372_16=16 \
  --unit 35452993_16=16

python3 scripts/task_resources.py ingest scattered_joint_calling
```

The collector only queries `sacct`; ingestion is entirely local. Profiles and non-executable recommendations are written under `workspace/task_resources/<profile>/`. See the [Resource Evidence and Learning Core documentation](docs/resource_evidence_and_learning_core.md).

After later Task Scanner runs, newly terminal array attempts can be discovered without entering job IDs manually:

```bash
python3 scripts/task_resources.py collect-new scattered_joint_calling --dry-run
python3 scripts/task_resources.py collect-new scattered_joint_calling
python3 scripts/task_resources.py ingest scattered_joint_calling
```

Automatic collection is capped at 20 previously unseen terminal attempts per invocation. The dry run is entirely local; collection performs only a bounded read-only `sacct` query and writes immutable local evidence.

Resource policy v1.2 aggregates widespread low-CPU behavior into cohort-level evidence, withholds scattered-joint-calling proposals until successful observations cover at least three scatter batches and three chromosomes, and isolates empirical profiles by source host. The GAM scanner expands accounting array records so completed worker elements can be discovered independently of array-parent summaries.

Build a deterministic target-environment resource decision without remote access or job execution:

```bash
python3 scripts/task_resources.py ingest scattered_joint_calling

python3 scripts/task_plan.py resources \
  scattered_joint_calling \
  --target-environment roihu
```

The planner prefers target-environment evidence, reduces cross-environment proposals to low-confidence pilot-only guidance, and explicitly withholds values when evidence is missing or blocked. Evidence availability is reported separately from allocation availability, so substantial but narrow measurements remain visible even when no transferable proposal is allowed. It writes canonical plans under `workspace/task_plans/<task>/<target_environment>/<profile_key>/`. See the [Resource Decision and Transfer Core documentation](docs/resource_decision_and_transfer_core.md).

Promote the deterministic operational artifacts into versioned Brain v2 knowledge:

```bash
python3 scripts/brain.py ingest scattered_joint_calling
python3 scripts/brain.py ingest gam_deduplication
```

Then evaluate a versioned workflow contract against a target environment:

```bash
python3 scripts/brain.py plan-workflow scattered_joint_calling \
  --target-environment roihu
```

Knowledge snapshots are content-addressed under `workspace/brain_knowledge/<task>/`. The transfer planner reports compatible, unknown and incompatible requirements separately and consumes the existing deterministic resource decision as a gate. Brain v1 AI output, when present, remains a review-required candidate and is never silently promoted. See the [Brain v2 documentation](docs/brain_knowledge_and_workflow_transfer_v2.md).

Validate the AI backend, prompt and benchmark registries with:

```bash
python3 scripts/ai_benchmark.py validate
```

Prepare an immutable, non-executable benchmark package for the planned Roihu-GPU
candidate with:

```bash
python3 scripts/ai_benchmark.py prepare \
  --backend roihu_qwen3_coder \
  --suite genomeagent_core_v1
```

This records what would need to be evaluated but does not connect to Roihu, download a
model, create a runnable Slurm script or execute inference. Returned JSONL evidence can
later be scored with `scripts/ai_benchmark.py evaluate`; even a passing suite remains
review-only. See the [AI Backend Registry and Evaluation Core documentation](docs/ai_backend_registry_and_evaluation_core.md).

Collect current read-only evidence from the registered Roihu-GPU environment and then
derive its canonical state locally:

```bash
python3 scripts/ai_backend_evidence.py collect roihu_qwen3_coder
python3 scripts/ai_backend_evidence.py ingest roihu_qwen3_coder
```

The collector uses one bounded SSH session and performs no remote writes, model
downloads, GPU allocations, Slurm submissions, recursive model scans or large-file
hashing. Evidence and current state are stored separately under
`workspace/ai_backend_evidence/` and `workspace/ai_backend_state/`. See the
[AI Backend Evidence Collector documentation](docs/ai_backend_evidence_collector.md).

Build a deterministic, non-executable model acquisition plan from the registry and
saved backend evidence with:

```bash
python3 scripts/model_acquisition.py plan roihu_qwen3_coder
```

The **Pinned Model Acquisition Planning Core** separates source identity, storage,
integrity, approval and post-acquisition benchmark gates. It derives only a clearly
labelled parameter-byte lower bound until an immutable source revision and complete
provider inventory have been reviewed. It does not contact the model provider or
Roihu, download weights, hash large files, submit jobs, update the backend registry or
activate a model. See the
[model acquisition planning documentation](docs/pinned_model_acquisition_planner.md).

Resolve the registered public source identity and rebuild its local review state with:

```bash
python3 scripts/model_source_metadata.py collect roihu_qwen3_coder
python3 scripts/model_source_metadata.py ingest roihu_qwen3_coder
```

The collector makes one bounded symbolic-revision request and one immutable-revision
confirmation request to the registered Hugging Face repository. It records file
metadata, not file contents, and writes an acquisition-specification proposal without
applying it. License acceptance, model download, Roihu access, Slurm submission, GPU
allocation and backend activation remain disabled. See the
[model source metadata documentation](docs/model_source_metadata_collector.md).

After reviewing the exact license URL, record explicit acceptance for a specific
evidence snapshot with:

```bash
python3 scripts/model_source_metadata.py approve roihu_qwen3_coder \
  --evidence-id 20260715T094807016620Z \
  --reviewer tuomas64 \
  --accept-license Apache-2.0 \
  --confirm-license-review
```

The command is the only source-configuration mutation in this workflow. It writes an
immutable local approval record and updates the versioned acquisition specification
with the exact revision, inventory, size and licence provenance. It remains unable to
download a model or contact Roihu. See the
[model source approval documentation](docs/model_source_approval.md).

---

# Why GenomeAgent?

| Typical AI assistant                    | GenomeAgent                                                |
| --------------------------------------- | ---------------------------------------------------------- |
| Relies on user-provided context         | Systematically explores research projects                  |
| Starts each interaction from scratch    | Continuously learns complete research projects             |
| Limited project context                 | Builds persistent project knowledge                        |
| Assumes a generic computing environment | Learns and adapts to the local HPC environment             |
| Generates isolated code                 | Understands, improves, and designs computational workflows |

---

# Adaptation

Research projects often outlive the computing environments in which they begin.

GenomeAgent is designed to preserve its understanding of a research project while continuously learning new HPC environments as software, storage systems, and computing infrastructure evolve.

---

# Vision

GenomeAgent aims to become an AI research companion that develops an increasingly deep understanding of computational genomics projects through continuous learning and adaptive reasoning, enabling more effective, transparent, and reproducible computational research.

Although GenomeAgent is currently developed and validated for computational genomics, its underlying architecture is designed to be extensible to other HPC-based scientific domains.
