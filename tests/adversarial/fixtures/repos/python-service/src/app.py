"""Application entry point."""

from src.config import build_config


class Application:
    def __init__(self) -> None:
        self.settings = build_config()

    def start(self) -> None:
        raise RuntimeError("service failed to bind to the configured host")


def main() -> None:
    Application().start()
