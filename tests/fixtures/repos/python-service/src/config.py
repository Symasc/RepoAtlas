"""Service configuration loaded from the environment."""

import os
from dataclasses import dataclass

DEFAULT_PORT = 8080


@dataclass
class ServiceConfig:
    host: str
    port: int
    timeout: float


def load_config() -> ServiceConfig:
    """Build configuration from environment variables."""
    host = os.getenv("SERVICE_HOST", "localhost")
    port = int(os.environ["SERVICE_PORT"])
    timeout = float(os.environ.get("SERVICE_TIMEOUT", "30"))
    return ServiceConfig(host=host, port=port, timeout=timeout)


async def reload_config() -> ServiceConfig:
    return load_config()
