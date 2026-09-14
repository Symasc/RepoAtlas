"""What a published artifact is allowed to contain (RC audit, RCA-04).

The adversarial suite proves that a file named like a private key is never read,
opened or indexed, and the only honest way to prove that is with files named like
private keys. So the checkout contains ``id_rsa``, ``secret.pem``, ``private.key``
and several ``.env`` variants, each holding a sentinel where a secret would be.
They are doing real work and they stay.

They were also in the source distribution, because hatchling includes the tree by
default and nothing had said otherwise. The wheel never carried them. That is a
release-hygiene defect rather than a disclosure one --- the files are fake --- but
a published sdist is scanned, mirrored and unpacked by tooling that reads a
filename long before it reads a sentinel, so shipping them means shipping a
permanent false positive to everyone downstream, and the small chance that
somebody believes it.

Two guards, because a name rule alone is not enough. The exclusions are written
by shape, and shape is exactly what a rename escapes: the first version of this
correction matched ``.env`` and sailed straight past ``.env.production``. So the
second guard reads the *contents* of what shipped and looks for the fake-secret
sentinels themselves, which is what caught those two.

The scope is deliberately narrow. This is about files whose whole purpose is to
look like a credential, living in a fixture repository. Test source that merely
mentions a sentinel in a string literal is ordinary code and is not the subject.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tarfile
import tomllib
import zipfile
from collections.abc import Iterator
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent

#: Basenames and suffixes that a secret scanner reacts to. Matched against the
#: last path component, so a directory named ``keys`` is not swept up with them.
CREDENTIAL_NAMES = re.compile(
    r"""^(
          \.env(\..+)?
        | id_(rsa|dsa|ecdsa|ed25519)(\.pub)?
        | .+\.(pem|key|p12|pfx|ppk|jks|keystore)
        | \.netrc | \.pgpass | \.htpasswd
    )$""",
    re.VERBOSE | re.IGNORECASE,
)

#: What the fake credentials in the fixture repositories actually contain. A file
#: renamed out of the pattern above still carries one of these.
FIXTURE_SECRET_MARKERS = (
    b"REPOATLAS_FAKE_SECRET_SENTINEL_918273",
    b"super-secret-value-that-must-not-be-indexed",
    b"-----BEGIN OPENSSH PRIVATE KEY-----",
    b"-----BEGIN RSA PRIVATE KEY-----",
    b"-----BEGIN PRIVATE KEY-----",
)

#: Every secret-shaped fixture the checkout is expected to keep. Spelled out so
#: that deleting one to make this module pass is itself a failure.
SOURCE_FIXTURES = (
    "tests/adversarial/fixtures/repos/python-service/.env",
    "tests/adversarial/fixtures/repos/python-service/.env.production",
    "tests/adversarial/fixtures/repos/python-service/id_ed25519",
    "tests/adversarial/fixtures/repos/python-service/id_rsa",
    "tests/adversarial/fixtures/repos/python-service/private.key",
    "tests/adversarial/fixtures/repos/python-service/secret.pem",
    "tests/adversarial/fixtures/repos/python-service/src/nested/deeply/hidden/.env.local",
    "tests/adversarial/fixtures/repos/python-service/src/nested/deeply/hidden/service.pem",
    "tests/fixtures/repos/python-service/.env",
)

INSTALLED_SKILL = "repoatlas/skills/repoatlas-investigation/SKILL.md"
SOURCE_SKILL = "skills/repoatlas-investigation/SKILL.md"

requires_uv = pytest.mark.skipif(
    shutil.which("uv") is None, reason="uv is the project's packaging command and is not installed"
)


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The real wheel and sdist, built the way a release would build them."""
    out = tmp_path_factory.mktemp("dist")
    subprocess.run(
        ["uv", "build", "--out-dir", str(out)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return out


def wheel_names(built: Path) -> list[str]:
    with zipfile.ZipFile(next(built.glob("*.whl"))) as archive:
        return archive.namelist()


def sdist_names(built: Path) -> list[str]:
    with tarfile.open(next(built.glob("*.tar.gz"))) as archive:
        return archive.getnames()


def sdist_files(built: Path) -> Iterator[tuple[str, bytes]]:
    """Every regular file in the sdist, small ones read whole."""
    with tarfile.open(next(built.glob("*.tar.gz"))) as archive:
        for member in archive:
            if not member.isfile() or member.size > 1 << 20:
                continue
            handle = archive.extractfile(member)
            yield member.name, handle.read() if handle else b""


def credential_shaped(names: list[str]) -> list[str]:
    return [name for name in names if CREDENTIAL_NAMES.match(Path(name).name)]


# ---------------------------------------------------------------------------
# The checkout keeps every hostile fixture
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("relative", SOURCE_FIXTURES)
def test_the_source_checkout_still_has_its_hostile_fixtures(relative: str) -> None:
    """The cheap way to pass the artifact tests is to delete the fixtures.

    That would swap a real guarantee --- RepoAtlas never reads a file that looks
    like a private key --- for a cosmetic one, and the tests that prove it would
    go on passing with nothing left to prove. The correction is artifact hygiene
    and touches the checkout not at all.
    """
    path = ROOT / relative
    assert path.is_file(), relative
    assert path.stat().st_size > 0, relative


def test_the_hostile_fixtures_are_fakes() -> None:
    """Every one carries a sentinel where a real secret would be.

    Worth asserting rather than assuming: this module argues that shipping these
    is merely untidy, and that argument rests entirely on their being fake.
    """
    for relative in SOURCE_FIXTURES:
        data = (ROOT / relative).read_bytes()
        assert any(marker in data for marker in FIXTURE_SECRET_MARKERS), relative


# ---------------------------------------------------------------------------
# The artifacts do not
# ---------------------------------------------------------------------------


@requires_uv
def test_no_credential_shaped_file_ships_in_the_sdist(built: Path) -> None:
    found = credential_shaped(sdist_names(built))
    assert found == [], found


@requires_uv
def test_no_credential_shaped_file_ships_in_the_wheel(built: Path) -> None:
    """The wheel was already clean, and this keeps it that way.

    The force-include that maps the skill in is the one place a future packaging
    change could start pulling arbitrary trees into the wheel.
    """
    found = credential_shaped(wheel_names(built))
    assert found == [], found


@requires_uv
def test_no_fixture_secret_content_ships_in_the_sdist(built: Path) -> None:
    """The guard that catches a rename, which is what a name rule cannot.

    Scoped to fixture repositories: a test module holding ``-----BEGIN PRIVATE
    KEY-----`` in a string literal is source code doing its job, and excluding
    test sources is a different decision from this one.
    """
    offenders = [
        (name, marker.decode())
        for name, data in sdist_files(built)
        if "/fixtures/repos/" in name
        for marker in FIXTURE_SECRET_MARKERS
        if marker in data
    ]
    assert offenders == [], offenders


@requires_uv
def test_the_exclusions_are_declared_by_shape_not_by_listing(built: Path) -> None:
    """Read the configuration, so a misconfiguration names itself.

    A build result says "the file is not there". This says which setting was
    supposed to keep it out, which is the thing somebody has to fix.
    """
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    excluded = config["tool"]["hatch"]["build"]["targets"]["sdist"]["exclude"]

    assert excluded, "the sdist target must declare exclusions"
    for pattern in excluded:
        assert pattern.startswith("tests/"), pattern
        assert "*" in pattern, f"{pattern} names a file rather than a shape"


# ---------------------------------------------------------------------------
# ...and the artifacts still work
# ---------------------------------------------------------------------------


@requires_uv
def test_both_artifacts_still_carry_the_canonical_skill(built: Path) -> None:
    """An exclusion that removed the skill would pass every test above."""
    assert INSTALLED_SKILL in wheel_names(built)
    assert any(name.endswith(SOURCE_SKILL) for name in sdist_names(built))


@requires_uv
def test_the_runtime_package_is_intact(built: Path) -> None:
    """The wheel is what people install, and excluding by shape could overreach."""
    names = wheel_names(built)
    for expected in (
        "repoatlas/__init__.py",
        "repoatlas/mcp/server.py",
        "repoatlas/capabilities/service.py",
        "repoatlas/storage/sqlite/migrations/001_initial.sql",
    ):
        assert expected in names, expected


@requires_uv
def test_a_wheel_can_still_be_built_from_the_source_distribution(
    built: Path, tmp_path: Path
) -> None:
    """The point of an sdist, and the thing an exclusion could quietly break.

    ``uv build`` builds the wheel *through* the sdist, so a source artifact that
    had lost a build input would stop producing a runtime distribution at all. A
    tar listing that looks tidier is not the same as a release that still works.
    """
    unpacked = tmp_path / "unpacked"
    unpacked.mkdir()
    with tarfile.open(next(built.glob("*.tar.gz"))) as archive:
        archive.extractall(unpacked, filter="data")
    (source,) = list(unpacked.iterdir())

    out = tmp_path / "from-sdist"
    result = subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(out)],
        cwd=source,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr

    wheels = list(out.glob("*.whl"))
    assert len(wheels) == 1, wheels
    with zipfile.ZipFile(wheels[0]) as archive:
        rebuilt = archive.namelist()
    assert INSTALLED_SKILL in rebuilt
    assert "repoatlas/__init__.py" in rebuilt
    assert credential_shaped(rebuilt) == []


# ---------------------------------------------------------------------------
# The embedded reasoner is not in either artifact (legacy local-LLM removal)
# ---------------------------------------------------------------------------

#: Paths and names the deleted subsystem would occupy if any of it shipped.
REASONER_ARTIFACTS = (
    "repoatlas/llm/",
    "repoatlas/services/query_service.py",
    "llm/prompts/",
    "/ask.md",
    "/debug.md",
    "/repair.md",
    "/system.md",
)


@requires_uv
@pytest.mark.parametrize("fragment", REASONER_ARTIFACTS)
def test_no_reasoning_module_or_prompt_ships_in_either_artifact(built: Path, fragment: str) -> None:
    """CASE 14's static half. A deleted package can still ship.

    Removal is a source change; packaging is a separate one, and a
    ``force-include`` or a stale build directory is exactly how a module nobody
    can import goes on being published.
    """
    for name in wheel_names(built) + sdist_names(built):
        assert fragment not in name, f"{fragment} in {name}"


@requires_uv
def test_the_installed_product_runs_with_no_model_server_or_variable(
    built: Path, tmp_path: Path
) -> None:
    """CASE 14. Install the wheel into a clean environment and use it.

    The environment is built rather than inherited, so nothing from the developer
    machine reaches the subprocess: no ``REPOATLAS_LLM_BASE_URL``, no model
    server, no config file, no index. That is the state a new user is in, and the
    thing being asserted is that it is now a *working* state --- the product no
    longer has a prerequisite to install.

    ``REPOATLAS_LLM_BASE_URL`` is set anyway, to a URL that would be obvious if
    anything read it. It must change nothing.
    """
    venv = tmp_path / "venv"
    subprocess.run(["uv", "venv", str(venv)], check=True, capture_output=True, text=True)
    wheel = next(built.glob("*.whl"))
    subprocess.run(
        ["uv", "pip", "install", "--python", str(venv / "bin" / "python"), str(wheel)],
        check=True,
        capture_output=True,
        text=True,
    )

    environment = {
        "PATH": f"{venv / 'bin'}:/usr/bin:/bin",
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "REPOATLAS_LLM_BASE_URL": "http://sentinel.invalid:99999",
    }
    (tmp_path / "home").mkdir()

    help_text = subprocess.run(
        [str(venv / "bin" / "repoatlas"), "--help"],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    ).stdout
    for gone in (" ask ", " debug ", "--no-llm"):
        assert gone not in help_text, gone
    for kept in ("init", "sync", "status", "repos", "locate", "mcp"):
        assert kept in help_text, kept

    # The package is importable and carries no reasoner, from site-packages
    # rather than from the checkout.
    probe = subprocess.run(
        [
            str(venv / "bin" / "python"),
            "-c",
            "import importlib, repoatlas.capabilities.service, repoatlas.grounding;"
            "\nimport repoatlas.mcp.tools as t;"
            "\nprint(len(t.TOOLS));"
            "\ntry:\n importlib.import_module('repoatlas.llm')\n print('LEAKED')"
            "\nexcept ModuleNotFoundError:\n print('absent')",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    ).stdout.split()
    assert probe == ["8", "absent"], probe
