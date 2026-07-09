"""Entry point: python -m casterbot"""
from .bot import run

# === MAINTAINABILITY / AGENTS AUDIT ANNOTATIONS ===
# Code smell: very thin launcher has no guarded startup diagnostics or configuration preflight.
# Code smell: startup failure context is delegated entirely to deeper modules, reducing locality.
# AUDIT COUNTS: format gate failed for this file; ruff findings=0; pyright findings=0.
# AUDIT SCOPE: launcher is structurally small, but startup validation is absent.

if __name__ == "__main__":
    run()
