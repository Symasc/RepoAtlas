"""Application entry point."""

from src.config import load_config


class Application:
    """Starts the worker."""

    def __init__(self) -> None:
        self.config = load_config()

    def start(self) -> str:
        return f"listening on {self.config.host}:{self.config.port}"


def main() -> str:
    app = Application()
    return app.start()
