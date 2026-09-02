"""Container delivery invariants (V1_SPEC §18).

Building an image needs a Docker daemon, which CI may not have, so these tests
assert the properties of the *delivery description* instead — the ones that stop
being true silently. Two of them are security guarantees rather than packaging
details, and neither is expressible anywhere else in the codebase:

* repositories are mounted **read-only**, so "V1 never writes to a repository" is
  enforced by the kernel rather than promised by the code (§17);
* RepoAtlas state lives on a **separate volume**, so the index is never written
  into the tree under inspection.

The build itself is exercised out of band; see the Slice E notes in V1_SPEC §23.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = ROOT / "Dockerfile"
COMPOSE = ROOT / "docker-compose.yml"


@pytest.fixture(scope="module")
def compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def dockerfile() -> str:
    return DOCKERFILE.read_text(encoding="utf-8")


# -- the two services §18 names ---------------------------------------------


def test_both_required_services_are_defined(compose: dict) -> None:
    assert set(compose["services"]) == {"repoatlas", "ollama"}


def test_no_research_infrastructure_is_present(compose: dict) -> None:
    """ "Do not add SearXNG or other research infrastructure in V1" (§18, §22).

    Checked against the images the stack actually runs rather than the file text,
    which names SearXNG in a comment explaining its absence.
    """
    images = " ".join(
        str(service.get("image", "")) for service in compose["services"].values()
    ).lower()
    for deferred in ("searx", "elastic", "qdrant", "weaviate", "redis", "postgres"):
        assert deferred not in images


# -- the read-only repository mount ------------------------------------------


def _repository_mount(compose: dict) -> dict:
    mounts = [
        mount
        for mount in compose["services"]["repoatlas"]["volumes"]
        if isinstance(mount, dict) and mount.get("target") == "/repos"
    ]
    assert len(mounts) == 1, "exactly one repository mount"
    return mounts[0]


def test_repositories_are_mounted_read_only(compose: dict) -> None:
    mount = _repository_mount(compose)
    assert mount["type"] == "bind"
    assert mount["read_only"] is True


def test_the_state_volume_is_separate_from_the_repositories(compose: dict) -> None:
    volumes = compose["services"]["repoatlas"]["volumes"]
    named = [mount for mount in volumes if isinstance(mount, str)]
    assert any(mount.startswith("repoatlas-state:") for mount in named)
    # The state path must not be inside the read-only repository mount, or the
    # first write would fail — and if it did not, the index would land in the
    # user's source tree.
    state = next(mount for mount in named if mount.startswith("repoatlas-state:"))
    assert not state.split(":", 1)[1].startswith("/repos")


def test_the_declared_state_paths_live_on_the_state_volume(dockerfile: str) -> None:
    for variable in ("REPOATLAS_CONFIG", "REPOATLAS_DB", "HF_HOME"):
        line = next(row for row in dockerfile.splitlines() if row.strip().startswith(variable))
        assert "/state" in line, variable


def test_the_state_volume_is_declared(compose: dict) -> None:
    assert "repoatlas-state" in compose["volumes"]


# -- container posture --------------------------------------------------------


def test_the_container_does_not_run_as_root(dockerfile: str) -> None:
    """Repository content is untrusted input (§17)."""
    users = [line.split()[1] for line in dockerfile.splitlines() if line.startswith("USER ")]
    assert users and users[-1] != "root"


def test_privilege_escalation_is_refused(compose: dict) -> None:
    service = compose["services"]["repoatlas"]
    assert "no-new-privileges:true" in service["security_opt"]
    assert service["cap_drop"] == ["ALL"]


def test_the_root_filesystem_is_read_only(compose: dict) -> None:
    service = compose["services"]["repoatlas"]
    assert service["read_only"] is True
    # With a read-only root, the writable paths must be exactly the state volume
    # and a tmpfs; anything else would be an unnoticed write surface.
    assert set(service["tmpfs"]) == {"/tmp", "/home/repoatlas"}  # noqa: S108 - container paths


def test_the_image_installs_from_the_committed_lockfile(dockerfile: str) -> None:
    """A build that re-resolves dependencies ships versions no test has seen."""
    assert "uv.lock" in dockerfile
    assert "--frozen" in dockerfile


def test_git_is_the_only_external_binary_installed(dockerfile: str) -> None:
    install = next(line for line in dockerfile.splitlines() if "apt-get install" in line)
    packages = install.split("-y", 1)[1].split("&&")[0].replace("\\", "").split()
    assert packages == ["git"]


def test_the_model_server_is_not_published_to_the_network(compose: dict) -> None:
    for published in compose["services"]["ollama"]["ports"]:
        assert str(published).startswith("127.0.0.1:")


def test_the_reasoning_endpoint_points_at_the_sibling_service(compose: dict) -> None:
    """``localhost`` is the container itself; the model lives next door."""
    environment = compose["services"]["repoatlas"]["environment"]
    assert environment["REPOATLAS_LLM_BASE_URL"] == "http://ollama:11434"


def test_the_documented_override_is_the_one_the_code_reads(
    monkeypatch: pytest.MonkeyPatch, compose: dict, tmp_path: Path
) -> None:
    """The compose file and the settings loader must agree, or the deployment
    silently talks to nothing."""
    from repoatlas.config.settings import load_settings

    name, value = next(iter(compose["services"]["repoatlas"]["environment"].items()))
    monkeypatch.setenv(name, value)
    assert load_settings(tmp_path / "absent.yaml").llm.base_url == value


def test_the_build_context_excludes_state_and_tests() -> None:
    ignored = set((ROOT / ".dockerignore").read_text(encoding="utf-8").split())
    assert {".git", ".venv", "tests"} <= ignored
