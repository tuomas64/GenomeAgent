# GenomeAgent Allas Tools v0.3

Allas Tools v0.3 keeps the Mac repository as the permanent control plane and
uses Puhti only for generated runtime scripts and Slurm logs.

## Dataset Registry integration

- Existing ad-hoc transfers can be assigned retrospectively with
  `bin/genomeagent-datasets assign-transfer`.
- Folder commands accept either the generated transfer task ID or the original
  source path for `show`, `scan`, verification, and restore operations.
- Upload and restore verification synchronize linked Dataset Registry records.
- The same dataset identity is retained across Puhti, Roihu, and Allas.
- Automatic local deletion remains disabled.

## Examples

```bash
bin/genomeagent-allas list-folder-uploads
bin/genomeagent-allas show-folder-upload folder_final_assemblies_ca9a84679e
bin/genomeagent-allas show-folder-upload \
  /scratch/project_2001113/pangenome/minigraph_cactus_final_assemblies_nofilter_giraffe/Final_assemblies
```

After verification:

```bash
bin/genomeagent-allas verify-folder-upload folder_final_assemblies_ca9a84679e
bin/genomeagent-datasets show pangenome_final_assemblies_29
```
