"""GROUND TRUTH: defines load_config, but NOTHING imports it.

Name equality with common_lib.settings.load_config must not create an edge.
"""


def load_config() -> dict:
    return {"source": "unrelated-repo"}
