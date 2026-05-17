"""Safety layer — §9.10, §17.7 independent value.

Operations are classified before they run; high-risk ones either block or
demand explicit user confirmation. Every important call writes to AuditLog.
"""

from .risk import BULK_MODIFY_THRESHOLD, RiskClassifier, classify_operation
from .audit import AuditLogger
from .pretool import (
    InterceptResult,
    build_confirmation,
    build_refusal,
    intercept,
)
from .pii import PIIHit, scan as scan_pii

__all__ = [
    "RiskClassifier",
    "AuditLogger",
    "BULK_MODIFY_THRESHOLD",
    "classify_operation",
    "intercept",
    "InterceptResult",
    "build_confirmation",
    "build_refusal",
    "PIIHit",
    "scan_pii",
]
