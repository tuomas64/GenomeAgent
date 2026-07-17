from genomeagent.task_profiles.graph_sv_genotyping import (
    GraphSvGenotypingProfile,
    validate_config,
)


def config():
    return {
        "project_root": "/scratch/project",
        "expected_samples": 2,
        "sample_manifest_candidates": ["/scratch/project/samples.txt"],
        "command_evidence_globs": [],
        "sample_stage_globs": {
            "input_gam": ["input/*.gam"],
            "dedup_gam": ["dedup/*.gam"],
            "pack": ["pack/*.pack"],
            "sample_vcf": ["vcf/*.vcf.gz"],
        },
        "product_globs": {
            "merged_allsite_vcf": ["merged.vcf.gz"],
            "biallelic_sv50_vcf": ["sv50.vcf.gz"],
            "maf_geno_unpruned_vcf": ["unpruned.vcf.gz"],
            "ldpruned_pca_vcf": ["ld.vcf.gz"],
            "pca_eigenvec": ["pca.eigenvec"],
            "pca_eigenval": ["pca.eigenval"],
            "burden_long": ["burden.long.tsv"],
            "burden_wide": ["burden.wide.tsv"],
        },
    }


def test_validate_config():
    validate_config(config())


def test_complete_workflow_keeps_qc_warning_separate():
    profile = GraphSvGenotypingProfile()
    data = {
        "samples": [
            {
                "sample": "A", "state": "sample_vcf_validatable",
                "input_gam_present": True, "dedup_gam_present": True,
                "pack_present": True, "sample_vcf_present": True,
                "sample_vcf_index_present": True,
            },
            {
                "sample": "B", "state": "sample_vcf_validatable",
                "input_gam_present": True, "dedup_gam_present": True,
                "pack_present": True, "sample_vcf_present": True,
                "sample_vcf_index_present": True,
            },
        ],
        "manifest": {"exists": True, "duplicate_samples": []},
        "command_evidence": {
            "vg_call_a_confirmed": True,
            "vg_call_without_a_hits": [],
        },
        "products": {
            name: {"exists": True, "size_bytes": 1}
            for name in (
                "merged_allsite_vcf", "biallelic_sv50_vcf",
                "maf_geno_unpruned_vcf", "ldpruned_pca_vcf",
                "pca_eigenvec", "pca_eigenval",
                "burden_long", "burden_wide",
            )
        },
        "vcf_summaries": {
            "merged_allsite_vcf": {"sample_count": 2},
            "biallelic_sv50_vcf": {"record_count": 118206},
            "maf_geno_unpruned_vcf": {"record_count": 16601},
            "ldpruned_pca_vcf": {"record_count": 10664},
        },
        "jobs": {"running": [], "recent": []},
    }
    status = profile.interpret(data, config())
    assert status["overall_status"] == "workflow_complete_with_dataset_qc_warning"
    assert status["workflow_validation"] == "passed"
    assert status["dataset_comparability"] == "attention_required"
    assert status["automatic_execution_allowed"] is False
