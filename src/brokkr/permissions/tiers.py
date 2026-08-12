"""Permission tiers every proposed command belongs to.

The enum shape follows a common pattern from an earlier, read-only local
agent project of the author's, which scaffolded these same five tiers
"for later stages that add real system/file/cloud actions" but never
enforced anything past READ_ONLY. brokkr is that later stage, in a
separate project rather than a retrofit, so the tiers' *meaning* here is
inverted at the point of use: that earlier project hard-rejects anything
that isn't READ_ONLY; brokkr's approval flow instead ROUTES
REQUIRES_APPROVAL actions through confirmation, and hard-rejects only
PROHIBITED ones (see permissions/policy.py).
"""

from __future__ import annotations

from enum import Enum


class PermissionTier(str, Enum):
    READ_ONLY = "read_only"
    SAFE = "safe"
    REQUIRES_APPROVAL = "requires_approval"
    DESTRUCTIVE = "destructive"
    PROHIBITED = "prohibited"
