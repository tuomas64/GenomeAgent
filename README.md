# GenomeAgent

**An evidence-grounded AI assistant for computational genomics on HPC systems.**

GenomeAgent builds a persistent understanding of research projects by connecting datasets, software executions, computational tasks, validation evidence, and storage locations into evolving, reproducible workflow graphs.

It observes current computational state, preserves project history, learns from successful and failed executions, and converts that understanding into safe, reviewable workflow proposals.

**Motto: Understand first. Act second.**

---

## Design philosophy

Before an AI assistant can contribute safely to a research project, it must first understand that project.

GenomeAgent therefore separates:

- **observation** from modification;
- **dataset identity** from storage location;
- **scientific provenance** from transient runtime state;
- **knowledge** from current task state;
- **proposal generation** from execution authority;
- **AI reasoning** from deterministic safety checks; and
- **scientific decisions** from repetitive computational work.

The researcher remains in control.

---

## Central model

Datasets are the durable backbone of GenomeAgent.

A dataset has one scientific identity, but it may have:

- multiple producing or consuming workflow relationships;
- multiple major-task, run, task, or stage links;
- complete or partial copies on different systems;
- several software-version-specific workflow uses; and
- a lifecycle spanning creation, validation, archival, removal, restoration, and reuse.

```text
Dataset
├── scientific identity and version
├── produced by
│   ├── major task / run / task / stage
│   ├── software and version
│   ├── command and parameters
│   └── successful execution evidence
├── consumed by
│   └── one or more downstream tasks
├── validation evidence
└── locations
    ├── Puhti
    ├── Roihu CPU
    ├── Roihu GPU
    └── Allas
```

The same FASTQ dataset can be used by both linear and graph-based variant discovery. The same assembly collection can be consumed by Minigraph-Cactus, PGGB, and SVIM-asm. GenomeAgent records these as multiple scientific links to one dataset rather than creating duplicate dataset identities.

---

## Architecture

```text
                               Research project
                                      │
                                      ▼
                                  Explorer
                        read-only project observation
                                      │
                                      ▼
                          Project Knowledge / Brain
                   workflows, paths, software, lessons, protocols
                                      │
                    ┌─────────────────┴──────────────────┐
                    │                                    │
                    ▼                                    ▼
             Dataset Registry                       Task Catalog
      identity, provenance, locations       major tasks, runs, tasks, stages
                    │                                    │
                    └─────────────────┬──────────────────┘
                                      │
                    ┌─────────────────┴──────────────────┐
                    │                                    │
                    ▼                                    ▼
               Task Scanner                  Resource Evidence Core
          current workflow evidence          scheduler/accounting evidence
                    │                                    │
                    ▼                                    │
             Task State Bridge                           │
       persistent state, history and                     │
           safe recommendations                          │
                    └─────────────────┬──────────────────┘
                                      ▼
                               Proposal Core
             deterministic, evidence-bound workflow adaptation
                                      │
                                      ▼
                             Researcher review
                                      │
                                      ▼
                   Controlled execution and validation
                          formal Executor in development
                                      │
                  ┌───────────────────┴───────────────────┐
                  ▼                                       ▼
            Allas Tools                              New evidence
       archive and restore lifecycle                       │
                  │                                        │
                  └────────────────────► learning cycle ◄───┘
```

Local and external AI backends can assist with interpretation and proposal construction. Exact state classification, safety gates, provenance checks, registry validation, and execution authority remain deterministic.

---

## Current capabilities

| Capability | Status |
|---|---|
| Full-project read-only exploration | ✅ |
| HPC environment discovery | ✅ |
| Persistent project knowledge | ✅ |
| Dataset identity and lifecycle registry | ✅ |
| Many-to-many dataset–workflow relationships | ✅ |
| Puhti, Roihu, and Allas location modeling | ✅ |
| Complete and partial dataset materialization | ✅ |
| Retrospective registration of historical datasets | ✅ |
| Interactive major-task and task assignment | ✅ |
| Incremental task-catalog authoring | ✅ |
| Managed and ad-hoc Allas upload planning | ✅ |
| Allas restore and verification lifecycle | ✅ |
| Generic task scanning | ✅ |
| Persistent task state and history | ✅ |
| Resource evidence and learning | ✅ |
| Deterministic Proposal Core | ✅ |
| Evidence-bound proposal validation | ✅ |
| Immutable, content-addressed proposal bundles | ✅ |
| Local AI backend registry and acquisition controls | ✅ |
| Controlled local-model benchmarking | ✅ |
| Automatic workflow reconstruction from dataset provenance | Foundation implemented; enrichment in development |
| Software-version-aware execution provenance | In development |
| Formal researcher-controlled Executor | In development |
| Autonomous execution | Planned |

---

## Core components

### Explorer

Explorer performs read-only observation of complete research project trees and computing environments. It records files, directories, sizes, timestamps, software context, and other evidence without modifying the remote project.

### Project Knowledge / GenomeAgent Brain

Project Knowledge preserves validated understanding across sessions:

- major workflows and stages;
- important paths and reference data;
- software, modules, and versions;
- reusable scripts and conventions;
- known failures and successful corrections;
- scientific and operational decisions; and
- project-specific validation rules.

The Brain can use AI to interpret project evidence, but stored knowledge remains inspectable and evidence-grounded.

### Dataset Registry

Dataset Registry is the Git-backed scientific data model for GenomeAgent. It records:

- what each dataset represents;
- which tasks produced and consumed it;
- complete and partial materializations;
- current and historical storage locations;
- upload and restore links;
- sample sets and expected counts; and
- lifecycle state.

Dataset identity is independent of location. Moving a dataset from Puhti to Allas, restoring 50 of 458 samples to Roihu, or retaining copies on several systems does not create a new scientific dataset.

### Task Catalog

Task Catalog defines currently evidenced major tasks, runs, tasks, and stages.

Workflow-style major tasks can describe established stage sequences. Incremental major tasks grow only when real datasets or successful execution evidence support new components. This avoids inventing speculative workflows.

### Dataset Assignment

Dataset Assignment connects existing Puhti, Roihu, or Allas data to the Task Catalog.

Interactive assignment lists all current major tasks and their runs, tasks, and stages. A dataset can receive several scientific links in one session, which supports shared inputs and outputs used across multiple workflows.

### Allas Tools

Allas Tools manages dataset archival and restoration while keeping the local GenomeAgent repository as the permanent control plane.

It supports:

- managed dataset uploads;
- ad-hoc folder uploads;
- retrospective transfer assignment;
- remote inventory and verification;
- complete or partial restoration;
- synchronization with Dataset Registry; and
- explicit protection against automatic local deletion.

Credentials and authentication tokens are never stored in Git or generated manifests.

### Task Scanner

Task Scanner answers a narrower question than Explorer:

> What is the current state of this specific computational task?

It creates immutable timestamped scan bundles containing outputs, scheduler records, failures, missing files, software evidence, and stage-specific validation.

### Task State Bridge

Task State Bridge replays Task Scans into persistent canonical state:

- current workflow status;
- current stage;
- append-only history;
- recommendations;
- provenance;
- dataset availability; and
- safety-relevant conditions.

It does not submit jobs or modify remote data.

### Resource Evidence and Learning Core

Resource Evidence Core records bounded scheduler accounting for explicit jobs and learns empirical runtime, memory, and efficiency profiles.

Failed and cancelled attempts remain censored evidence rather than being discarded. Resource recommendations are proposal-only and cannot change allocations automatically.

### Proposal Core

Proposal Core converts validated knowledge, Dataset Registry relationships, and current Task State into immutable workflow proposals.

A proposal can contain:

- an evidence snapshot;
- ordered input manifests;
- proposed Slurm and shell scripts;
- validation rules;
- provisional or learned resource recommendations;
- provenance;
- checksums; and
- an explicit authority boundary.

Proposal Core has no independent execution, deletion, or knowledge-update authority.

### AI backends

GenomeAgent includes controlled infrastructure for:

- backend registration;
- pinned model acquisition planning;
- model integrity verification;
- environment evidence;
- bounded GPU benchmarks; and
- deterministic evaluation.

Local and external models are intended to assist with bounded interpretation, coding, and workflow-proposal tasks while deterministic components enforce exact contracts and safety policy.

---

## Workflow reconstruction

Once datasets record their producing and consuming relationships, GenomeAgent can reconstruct complete workflows from any registered dataset.

```text
trimmed FASTQs
    ↓ vg Giraffe
original GAMs
    ↓ GAM duplicate removal
deduplicated GAMs
    ↓ vg pack
pack files
    ↓ vg call -a
per-sample all-site VCFs
    ↓ merge and filtering
joint structural-variant dataset
```

Each edge can be enriched with:

- exact software version;
- module, environment, or container;
- command and parameters;
- reference and index assets;
- scheduler job;
- successful and failed attempts;
- resource use; and
- validation results.

New software versions create new execution and dataset versions without overwriting historical provenance.

---

## Quick start

List the scientific task catalog:

```bash
bin/genomeagent-tasks list
bin/genomeagent-tasks show graph_sv_genotyping
```

Inspect registered datasets:

```bash
bin/genomeagent-datasets list
bin/genomeagent-datasets list-unassigned
bin/genomeagent-datasets show <dataset-id>
```

Register an existing Puhti or Roihu path:

```bash
bin/genomeagent-datasets assign-path \
  --host puhti \
  --dataset-id <dataset-id> \
  /absolute/path/to/dataset
```

When task identifiers are omitted, GenomeAgent opens a guided selector that lists the live Task Catalog. Several scientific links can be added to one dataset.

Add another relationship later:

```bash
bin/genomeagent-datasets add-task-link <dataset-id>
```

Plan and submit an Allas folder upload:

```bash
bin/genomeagent-allas plan-folder-upload /absolute/puhti/path
bin/genomeagent-allas submit-folder-upload /absolute/puhti/path
```

Monitor and verify using either the generated transfer task ID or the original source path:

```bash
bin/genomeagent-allas scan-folder-upload <task-id-or-source-path>
bin/genomeagent-allas verify-folder-upload <task-id-or-source-path>
```

Allas verification updates the linked Dataset Registry record without changing dataset identity.

---

## Repository layout

```text
bin/                         user-facing commands
genomeagent/                 core Python modules
scripts/                     command implementations
tools/allas/                 Allas controller and runtime generation
config/major_tasks/          major-task and workflow definitions
config/dataset_assignment/   assignment rules
data_registry/               Git-backed scientific dataset metadata
docs/                        architecture and tool documentation
tests/                       deterministic regression tests
workspace/                   generated local runtime state; not committed
```

Scientific registry metadata may be committed. Credentials, authentication material, generated transfer runs, scheduler logs, large archives, and restored scientific files must not be committed.

---

## Safety model

GenomeAgent follows several non-negotiable rules:

- observation is read-only by default;
- current state must be proven from evidence;
- dataset identity is not inferred from location alone;
- missing local data is not assumed to be archived without evidence;
- completed work must not be resubmitted;
- proposal inputs must be grounded in observed or registered paths;
- stale proposals are rejected;
- automatic execution is disabled;
- automatic local deletion is disabled;
- destructive actions require explicit researcher approval;
- credentials and tokens are excluded from Git and logs;
- final outputs and archives require validation; and
- failed attempts remain part of the learning record.

AI output does not bypass these rules.

---

## Why GenomeAgent?

| Typical AI assistant or pipeline tool | GenomeAgent |
|---|---|
| Relies mainly on the current conversation or a predefined workflow | Systematically explores and learns the evolving research project |
| Treats a path as the identity of data | Separates dataset identity, scientific provenance, and storage location |
| Represents one dataset inside one pipeline | Supports shared datasets across multiple major tasks and analyses |
| Loses context when data are archived or moved | Tracks Puhti, Roihu, Allas, complete copies, and partial restores |
| Starts each interaction with limited project state | Maintains persistent knowledge, dataset provenance, and task history |
| Generates isolated scripts | Understands workflows, dependencies, versions, and completion state |
| May suggest rerunning completed work | Checks canonical task and dataset state before proposing action |
| Treats software upgrades as replacements | Preserves historical provenance while allowing versioned reruns |
| Treats failures as temporary context | Preserves failures as reusable evidence |
| Mixes reasoning and execution | Separates observation, proposal, approval, execution, and validation |
| Uses AI output directly | Applies deterministic validation and authority boundaries |

---

## Adaptation

Research projects often outlive the computing environments and software versions in which they begin.

GenomeAgent is designed to preserve project understanding while learning new clusters, processor architectures, module systems, storage services, scheduler behavior, and software installations.

Current development includes:

- CSC Puhti;
- CSC Roihu CPU and GPU environments;
- Allas object storage;
- linear and graph-based genomics workflows;
- GATK joint calling;
- pangenome and structural-variant workflows; and
- local and external AI backends.

---

## Development status

GenomeAgent is active research software under rapid development.

The current foundation includes the Dataset Registry, Task Catalog, interactive assignment, Allas lifecycle tools, Task Scanner, Task State Bridge, Resource Evidence Core, and Proposal Core.

The next major steps are:

1. connect shared task scanners directly to Dataset Registry relationships;
2. extract exact software, version, command, and parameter evidence automatically;
3. reconstruct and visualize complete workflow graphs from registered datasets;
4. integrate learned resource profiles into proposal generation;
5. build a formal researcher-controlled Executor;
6. capture execution and validation evidence automatically; and
7. extend the architecture beyond computational genomics.

---

## Vision

GenomeAgent aims to become an AI research companion that develops an increasingly deep, evidence-grounded understanding of computational projects and uses that understanding to improve reproducibility, efficiency, adaptability, and researcher control.

Although GenomeAgent is currently developed and validated for computational genomics, its architecture is intended to support other HPC-based scientific domains.
