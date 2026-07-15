# GenomeAgent

**An evidence-grounded AI assistant for computational genomics on HPC systems.**

GenomeAgent builds a persistent understanding of research projects, observes their current computational state, learns from workflow and scheduler evidence, and converts that knowledge into safe, reviewable, environment-specific workflow proposals.

**Motto: Understand first. Act second.**

---

## Production milestone

On **15 July 2026**, GenomeAgent completed its first production proposal cycle on the CSC Puhti supercomputer.

Starting from read-only observations of an active GATK joint-calling workflow, GenomeAgent:

- reconstructed the current task state;
- verified that all **886 scattered interval VCF/index pairs** were complete;
- generated a deterministic, reference-ordered proposal;
- gathered the intervals into **seven retained chromosome VCFs**;
- gathered those chromosome files into one **38 GB whole-genome VCF** containing **455 samples**;
- created and verified the final index;
- validated sample identity, chromosome order, file readability, statistics and checksums; and
- supported researcher-controlled Slurm submission directly from a Mac.

The final production output passed validation.

This demonstrates GenomeAgent's central operating model:

```text
Observe
  ↓
Learn
  ↓
Track current state
  ↓
Propose
  ↓
Researcher review
  ↓
Controlled execution
  ↓
Validate
  ↓
Learn from evidence
```

The production trial also showed how GenomeAgent improves from real execution evidence. Puhti environment initialization, Slurm spool-path behavior, explicit VCF indexing and linear-reference contig validation were incorporated into deterministic guards and regression tests.

---

## Design philosophy

Before an AI assistant can contribute safely to a research project, it must first understand that project.

GenomeAgent therefore separates:

- **observation** from modification;
- **knowledge** from current task state;
- **proposal generation** from execution authority;
- **AI reasoning** from deterministic safety checks; and
- **scientific decisions** from repetitive computational work.

The researcher remains in control.

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
                                ▼
                         New evidence
                                │
                                └──────────► learning cycle
```

Local and external AI backends can assist with reasoning and proposal construction. Exact state classification, safety gates, provenance checks and execution authority remain deterministic.

---

## Current capabilities

| Capability | Status |
|---|---|
| Full-project read-only exploration | ✅ |
| HPC environment discovery | ✅ |
| Persistent project knowledge | ✅ |
| Workflow understanding | ✅ |
| Generic task scanning | ✅ |
| Persistent task state and history | ✅ |
| Resource evidence and learning | ✅ |
| Deterministic Proposal Core | ✅ Production validated |
| Evidence-bound proposal validation | ✅ |
| Immutable, content-addressed proposal bundles | ✅ |
| Mac-controlled Puhti execution | ✅ Production demonstrated |
| Local AI backend registry and acquisition controls | ✅ |
| Controlled local-model benchmarking | ✅ |
| AI-assisted proposal construction | 🚧 Evaluation stage |
| Formal researcher-controlled Executor | 🚧 In development |
| Autonomous execution | 📋 Planned |

---

## Core components

### Explorer

Explorer performs read-only observation of complete research project trees and computing environments. It records files, directories, sizes, timestamps, software context and other evidence without modifying the remote project.

### Project Knowledge / GenomeAgent Brain

Project Knowledge preserves validated understanding across sessions:

- major workflows and stages;
- important paths and reference data;
- software, modules and versions;
- reusable scripts and conventions;
- known failures and successful corrections;
- scientific and operational decisions; and
- project-specific validation rules.

The Brain can use AI to interpret project evidence, but stored knowledge remains inspectable and evidence-grounded.

### Task Scanner

Task Scanner answers a narrower question than Explorer:

> What is the current state of this specific computational task?

It creates immutable timestamped scan bundles containing current outputs, scheduler records, failures, missing files and stage-specific evidence.

### Task State Bridge

Task State Bridge replays Task Scans into persistent canonical state:

- current workflow status;
- current stage;
- append-only history;
- recommendations;
- provenance; and
- safety-relevant conditions.

It does not submit jobs or modify remote data.

### Resource Evidence and Learning Core

Resource Evidence Core records bounded scheduler accounting for explicit jobs and learns empirical runtime, memory and efficiency profiles.

Failed and cancelled attempts remain censored evidence rather than being discarded. Resource recommendations are proposal-only and cannot change allocations automatically.

### Proposal Core

Proposal Core converts validated knowledge and current Task State into immutable workflow proposals.

A proposal can contain:

- an evidence snapshot;
- ordered input manifests;
- proposed Slurm and shell scripts;
- validation rules;
- provisional or learned resource recommendations;
- provenance;
- checksums; and
- an explicit authority boundary.

Proposal Core has no SSH, staging, Slurm submission, execution, deletion or knowledge-update authority.

### AI backends

GenomeAgent includes controlled infrastructure for:

- backend registration;
- pinned model acquisition planning;
- model integrity verification;
- environment evidence;
- bounded GPU benchmarks; and
- deterministic evaluation.

The first local backend tested on CSC Roihu uses **Qwen3-Coder-30B-A3B-Instruct**. Local models are intended to assist with bounded workflow and coding tasks while deterministic components enforce exact contracts and safety policy.

---

## Proposal Core example

The first supported production action is `gather_or_merge` for scattered joint calling:

```text
validated interval VCF/index pairs
        ↓
retained chromosome VCFs
        ↓
whole-genome VCF
        ↓
sample, contig, index and statistics validation
```

Prepare, validate and inspect a proposal:

```bash
python3 scripts/task_proposal.py prepare \
  scattered_joint_calling \
  --action gather_or_merge

python3 scripts/task_proposal.py validate \
  workspace/proposals/scattered_joint_calling/<proposal_id>

python3 scripts/task_proposal.py show \
  workspace/proposals/scattered_joint_calling/<proposal_id>
```

Generated proposals remain non-executable until separately reviewed and submitted by the researcher.

---

## Safety model

GenomeAgent follows several non-negotiable rules:

- observation is read-only by default;
- current state must be proven from evidence;
- completed work must not be resubmitted;
- proposal inputs must be grounded in observed paths;
- proposal IDs are content-addressed;
- stale proposals are rejected;
- generated scripts cannot submit other jobs;
- automatic execution is disabled;
- destructive actions require explicit researcher approval;
- final outputs require validation; and
- failed attempts remain part of the learning record.

AI output does not bypass these rules.

---

## Why GenomeAgent?

| Typical AI assistant | GenomeAgent |
|---|---|
| Relies mainly on context supplied in the current conversation | Systematically explores and learns the research project |
| Starts each interaction with limited project state | Maintains persistent knowledge and task history |
| Assumes a generic computing environment | Learns the actual HPC environment and its operational constraints |
| Generates isolated scripts | Understands workflows, dependencies and completion state |
| May suggest rerunning completed work | Checks canonical task state before proposing action |
| Mixes reasoning and execution | Separates observation, proposal, approval and execution |
| Treats failures as temporary conversation context | Preserves failures as reusable evidence |
| Uses AI output directly | Applies deterministic validation and authority boundaries |

---

## Adaptation

Research projects often outlive the computing environments in which they begin.

GenomeAgent is designed to preserve project understanding while continuously learning new clusters, architectures, module systems, storage services and scheduler behavior.

Current development spans:

- CSC Puhti;
- CSC Roihu CPU and GPU environments;
- linear and graph-based genomics workflows;
- GATK joint calling;
- pangenome and structural-variant workflows; and
- local and external AI backends.

---

## Development status

GenomeAgent is active research software under rapid development.

The next major steps are:

1. generalize Proposal Core to additional known workflows;
2. integrate learned resource profiles into proposal generation;
3. benchmark local AI on bounded workflow-proposal tasks;
4. build a formal researcher-controlled Executor;
5. capture execution evidence automatically; and
6. extend the architecture beyond computational genomics.

---

## Vision

GenomeAgent aims to become an AI research companion that develops an increasingly deep, evidence-grounded understanding of computational projects and uses that understanding to improve reproducibility, efficiency and researcher control.

Although GenomeAgent is currently developed and validated for computational genomics, its architecture is intended to support other HPC-based scientific domains.
