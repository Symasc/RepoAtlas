"""Neutral repositories for external services, redirects and permitted callbacks.

``portal`` serves a login page, an OAuth callback and a profile API, and its compose file publishes
it at ``http://portal:8080``, so the source links requests to it. ``worker`` asks an external token
endpoint for an access token and then, separately, calls the portal's API. ``webapp`` opens the
portal's login page and, separately, the external provider's authorize page. The two external
hosts (``auth.example.test``, ``login.example.test``) are named by no deployment and served by no
indexed repository, so the join leaves those requests ``http-service-unmatched``. Every name is
invented.
"""

from __future__ import annotations

from textwrap import dedent

PYPROJECT = '[project]\nname = "{name}"\nversion = "1.0.0"\ndependencies = ["{dep}"]\n'

PORTAL_APP = dedent("""\
    from fastapi import FastAPI

    app = FastAPI()


    @app.get("/login")
    def login() -> dict:
        return {}


    @app.get("/auth/callback")
    def callback() -> dict:
        return {}


    @app.get("/v1/profile")
    def profile() -> dict:
        return {}
""")
PORTAL_COMPOSE = {
    "docker-compose.yml": dedent("""\
        services:
          portal:
            build:
              context: .
              dockerfile: Dockerfile
            ports:
              - "8080:8080"
    """),
    "Dockerfile": (
        "FROM python:3.12-slim\nWORKDIR /srv\nCOPY portal /srv/portal\n"
        'CMD ["uvicorn", "portal.main:app", "--host", "0.0.0.0", "--port", "8080"]\n'
    ),
}
#: Asks the external provider for an access token, then calls the portal's API: two requests, and
#: no redirect anywhere.
WORKER = dedent("""\
    import requests


    def refresh_profile() -> dict:
        grant = requests.post(
            "https://auth.example.test/oauth/token",
            data={"grant_type": "client_credentials"},
            timeout=5,
        ).json()
        return requests.get(
            "http://portal:8080/v1/profile",
            headers={"Authorization": "Bearer " + grant["access_token"]},
            timeout=5,
        ).json()
""")
#: Opens the portal's login page (the portal answers it), and the provider's authorize page (no
#: indexed repository does).
WEBAPP = dedent("""\
    import requests


    def open_login() -> int:
        return requests.get("http://portal:8080/login", timeout=5).status_code


    def open_provider() -> int:
        return requests.get("https://login.example.test/authorize", timeout=5).status_code
""")


def repos() -> dict[str, dict[str, str]]:
    return {
        "portal": {
            "pyproject.toml": PYPROJECT.format(name="portal", dep="fastapi"),
            "portal/__init__.py": "",
            "portal/main.py": PORTAL_APP,
            **PORTAL_COMPOSE,
        },
        "worker": {
            "pyproject.toml": PYPROJECT.format(name="worker", dep="requests"),
            "worker/__init__.py": "",
            "worker/client.py": WORKER,
        },
        "webapp": {
            "pyproject.toml": PYPROJECT.format(name="webapp", dep="requests"),
            "webapp/__init__.py": "",
            "webapp/client.py": WEBAPP,
        },
    }
