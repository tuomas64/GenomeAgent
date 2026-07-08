# Explorer Core

Explorer Core is the first read-only sensing layer of GenomeAgent.

## Purpose

Explorer Core observes the HPC environment without interpretation or action.

It:

- connects to the cluster through SSH,
- records the login directory,
- changes explicitly to the configured workspace,
- runs safe read-only commands,
- saves raw observations as JSON.

## Safety

Explorer Core does not:

- delete files,
- move files,
- submit jobs,
- cancel jobs,
- install software,
- modify remote data.

## Current Puhti workspace

```text
/scratch/project_2001113
```

The login directory is usually:

```text
/users/tuomas64
```

Explorer Core therefore explicitly changes to the scratch workspace before observing project files.

## Observation principle

Explorer Core stores observations, not conclusions.

Interpretation will be added later by separate modules.
