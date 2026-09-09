"""Dead code kept for reference.

GROUND TRUTH: this module contains no environment-variable access of any kind, and
the token the legacy notes attribute to it appears nowhere in this file. Nothing
imports this module. It exists so a document can be wrong about it.
"""

LEGACY_DEFAULTS = {"host": "legacy.internal", "port": 9000}


def legacy_loader() -> dict:
    return dict(LEGACY_DEFAULTS)
