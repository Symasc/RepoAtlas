"""Service configuration. GROUND TRUTH: this file READS SERVICE_HOST and DB_URL."""

import os

from common_lib.settings import load_config

DEFAULT_PORT = 8080


def build_config() -> dict:
    # SERVICE_HOST is read here, on line 12. Nowhere else in this repository.
    host = os.getenv("SERVICE_HOST", "localhost")
    database = os.environ["DB_URL"]
    retries = int(os.environ.get("CONNECT_RETRIES", "3"))
    return {"host": host, "database": database, "retries": retries, **load_config()}
