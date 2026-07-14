"""Task-specific, read-only observation profiles for GenomeAgent."""

from .gam_deduplication import GamDeduplicationProfile
from .scattered_joint_calling import ScatteredJointCallingProfile

__all__ = ["GamDeduplicationProfile", "ScatteredJointCallingProfile"]
