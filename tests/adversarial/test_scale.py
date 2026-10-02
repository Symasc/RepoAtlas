"""Scale measurement (V1_SPEC §20).

**This is measurement, not optimization.** The instruction is to report first and
change nothing unless a result is obviously pathological, so the assertions are
deliberately loose: they catch growth that is *categorically* wrong — a per-query
full scan, a re-index of everything after one file changes — and say nothing about
constant factors.

Run with `-m scale -s` to see the numbers. Deselected by default because building
a fifty-repository tree costs real seconds.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from repoatlas.config.settings import Settings
from repoatlas.services.init_service import initialize
from repoatlas.services.sync_service import sync
from repoatlas.services.workspace import Workspace, open_workspace
from tests.conftest import init_git_repository

pytestmark = [pytest.mark.adversarial, pytest.mark.scale]


@dataclass
class Measurements:
    rows: list[tuple[str, float, str]] = field(default_factory=list)

    def record(self, label: str, seconds: float, detail: str = "") -> None:
        self.rows.append((label, seconds, detail))

    def render(self, title: str) -> str:
        lines = [f"\n{title}", "-" * len(title)]
        lines.extend(
            f"  {label:<28}{seconds * 1000:9.1f} ms  {detail}"
            for label, seconds, detail in self.rows
        )
        return "\n".join(lines)


def _synthetic_repository(path: Path, files: int) -> None:
    """A repository shaped like a real one: code, config, a manifest, a script."""
    (path / "src").mkdir(parents=True)
    name = path.name.replace("-", "_")
    for index in range(files):
        (path / "src" / f"module_{index}.py").write_text(
            f'"""Module {index}."""\n\n'
            "import os\n\n\n"
            f"def handler_{index}(value: str) -> str:\n"
            f'    host = os.getenv("SERVICE_HOST_{index % 7}", "localhost")\n'
            f'    return f"{{host}}:{{value}}:{index}"\n\n\n'
            f"class Component{index}:\n"
            f"    def run(self) -> str:\n"
            f"        return handler_{index}('x')\n"
        )
    (path / "pyproject.toml").write_text(
        f'[project]\nname = "{path.name}"\nversion = "1.0.0"\ndependencies = ["common-lib>=1.0"]\n'
    )
    (path / "deploy.sh").write_text(
        '#!/usr/bin/env bash\nsource ./env.sh\npython3 src/module_0.py\necho "${SERVICE_HOST_0}"\n'
    )
    (path / "env.sh").write_text(f"#!/usr/bin/env bash\nexport SERVICE_HOST_0={name}.example\n")
    (path / "config.yaml").write_text(f"service:\n  name: {path.name}\n  port: 8080\n")


def build_tree(root: Path, repositories: int, files_per_repository: int) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    # One provider so every synthetic repository has a real cross-repository edge.
    provider = root / "common-lib"
    provider.mkdir()
    (provider / "pyproject.toml").write_text('[project]\nname = "common-lib"\nversion = "1.0.0"\n')
    init_git_repository(provider)
    for index in range(repositories):
        repository = root / f"service-{index:03d}"
        _synthetic_repository(repository, files_per_repository)
        init_git_repository(repository)
    return root


Built = Callable[[int, int], tuple[Workspace, Path, Measurements]]


@pytest.fixture
def scaled(tmp_path: Path) -> Iterator[Built]:
    """A factory: build a tree of N repositories and index it, returning timings."""
    workspaces: list[Workspace] = []

    def build(repositories: int, files_per_repository: int) -> tuple[Workspace, Path, Measurements]:
        root = build_tree(tmp_path / f"tree-{repositories}", repositories, files_per_repository)
        settings = Settings.model_validate(
            {
                "storage": {"database": str(tmp_path / f"state-{repositories}" / "atlas.db")},
            }
        )
        workspace = open_workspace(settings=settings, require_root=False)
        workspaces.append(workspace)
        measurements = Measurements()

        started = time.perf_counter()
        initialize(workspace, root, config_path=tmp_path / f"config-{repositories}.yaml")
        measurements.record(
            "initial index", time.perf_counter() - started, f"{repositories + 1} repos"
        )
        return workspace, root, measurements

    yield build
    for workspace in workspaces:
        workspace.close()


def _database_bytes(workspace: Workspace) -> int:
    path = workspace.settings.storage.database
    return path.stat().st_size if path and path.exists() else 0


def _measure(workspace: Workspace, root: Path, measurements: Measurements) -> None:
    started = time.perf_counter()
    report = sync(workspace, workspace.resolver().resolve(root))
    measurements.record(
        "no-op sync", time.perf_counter() - started, f"{report.files_indexed} files reindexed"
    )
    assert report.files_indexed == 0, "an unchanged tree must reparse nothing"

    target = root / "service-000" / "src" / "module_0.py"
    target.write_text(target.read_text() + "\n\ndef added_symbol() -> int:\n    return 1\n")
    started = time.perf_counter()
    report = sync(workspace, workspace.resolver().resolve(root))
    measurements.record(
        "one-file sync", time.perf_counter() - started, f"{report.files_indexed} files reindexed"
    )
    assert report.files_indexed == 1, "one changed file must reparse one file"

    if report.link_trace is not None:
        measurements.record(
            "global link pass",
            report.link_trace.duration_ms / 1000.0,
            f"{report.link_trace.claims_considered} claims",
        )

    context = workspace.resolver().resolve(root / "service-000")
    for label, question in (
        ("exact retrieval", "SERVICE_HOST_0"),
        ("semantic retrieval", "how does this component obtain its address"),
        ("graph question", "Where is SERVICE_HOST_0 configured?"),
    ):
        started = time.perf_counter()
        evidence = workspace.retriever().retrieve(question, context)
        measurements.record(
            label, time.perf_counter() - started, f"{len(evidence.candidates)} candidates"
        )
        if evidence.trace is not None:
            graph = next((s for s in evidence.trace.stages if s.name == "graph"), None)
            if graph is not None and label == "graph question":
                measurements.record(
                    "  graph expansion",
                    graph.duration_ms / 1000.0,
                    f"{evidence.trace.graph_nodes_visited} nodes",
                )

    measurements.record("database size", 0.0, f"{_database_bytes(workspace) / 1_048_576:.1f} MB")


@pytest.mark.parametrize(("repositories", "files"), [(10, 20), (50, 20)])
def test_scale_measurements(
    scaled: Built, capsys: pytest.CaptureFixture[str], repositories: int, files: int
) -> None:
    workspace, root, measurements = scaled(repositories, files)
    _measure(workspace, root, measurements)
    with capsys.disabled():
        print(measurements.render(f"{repositories} repositories x {files} files"))

    timings = dict((label, seconds) for label, seconds, _ in measurements.rows)
    # A no-op sync must be far cheaper than the initial index. This is the shape
    # assertion behind "never re-index everything after one file changes"; the
    # margin is large so a loaded machine cannot trip it.
    assert timings["no-op sync"] < timings["initial index"] / 2
    assert timings["one-file sync"] < timings["initial index"] / 2
    # Retrieval is scoped, so it must not grow with the size of the managed set.
    assert timings["exact retrieval"] < 2.0
    assert timings["graph question"] < 2.0


def test_retrieval_does_not_scale_with_the_number_of_repositories(
    scaled: Built, capsys: pytest.CaptureFixture[str]
) -> None:
    """ "Never scan every repo for every question" (§20), measured rather than
    asserted: a scoped query over 51 repositories must not cost five times what
    the same query costs over 11."""
    small_workspace, small_root, _ = scaled(10, 10)
    large_workspace, large_root, _ = scaled(50, 10)

    def timed(workspace: Workspace, root: Path) -> float:
        context = workspace.resolver().resolve(root / "service-000")
        samples = []
        for _ in range(5):
            started = time.perf_counter()
            workspace.retriever().retrieve("SERVICE_HOST_0", context)
            samples.append(time.perf_counter() - started)
        return sorted(samples)[len(samples) // 2]

    small = timed(small_workspace, small_root)
    large = timed(large_workspace, large_root)
    with capsys.disabled():
        print(f"\nscoped retrieval: 11 repos {small * 1000:.1f} ms, 51 repos {large * 1000:.1f} ms")
    assert large < small * 5 + 0.05
