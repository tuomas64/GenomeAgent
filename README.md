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

### GenomeAgent Brain

The **GenomeAgent Brain** is the cognitive center of the system. It continuously learns from observations and combines accumulated project knowledge with AI reasoning to develop an evolving understanding of the research project.

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
