"""AI Process Audit: a deterministic pipeline for scoring automation opportunities.

Every module in this package is deterministic except ai_process_audit.scoring.judge,
which is the single point where a model may be consulted. See README.md.
"""

__version__ = "0.1.0"
