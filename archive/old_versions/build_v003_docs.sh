#!/usr/bin/env bash
set -euo pipefail

echo "GenomeAgent Build v0.3"
echo "This installer will create the documentation skeleton."
echo
echo "For this first installer, documentation files are generated."
echo "Subsequent versions will extend this structure."
echo

mkdir -p docs genomeagent tests examples scripts .github/workflows

cat > README.md << 'MD'
# GenomeAgent

**GenomeAgent is an AI-assisted computational genomics operating system for HPC clusters.**

**Motto:** Understand first. Act second.

This repository contains the documentation and software for GenomeAgent.
MD

cat > docs/index.md << 'MD'
# GenomeAgent

Welcome to the GenomeAgent documentation.

GenomeAgent learns computational research environments, maintains relationships
between datasets, software, workflows and results, and assists researchers in
performing reproducible genomics analyses on HPC systems.
MD

cat > docs/philosophy.md << 'MD'
# Philosophy

## Motto

**Understand first. Act second.**

GenomeAgent always seeks to understand computational relationships before
making recommendations or performing actions.
MD

cat > docs/roadmap.md << 'MD'
# Roadmap

- v0.1 SSH connectivity
- v0.2 Cluster abstraction
- v0.3 Documentation
- v0.4 Knowledge database
- v0.5 Discovery engine
- v1.0 AI-assisted genomics platform
MD

echo
echo "Documentation skeleton created successfully."
