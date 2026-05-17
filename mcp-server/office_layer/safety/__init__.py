"""Safety layer — §9.10, §17.7 independent value.

Operations are classified before they run; high-risk ones either block or
demand explicit user confirmation. Every important call writes to AuditLog.
"""

from .risk import RiskClassifier, classify_operation
from .audit import AuditLogger
from .pretool import InterceptResult, build_refusal, intercept

__all__ = [
    "RiskClassifier",
    "AuditLogger",
    "classify_operation",
    "intercept",
    "InterceptResult",
    "build_refusal",
]
