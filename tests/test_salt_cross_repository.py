from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from repoatlas.capabilities.service import CapabilityService
from repoatlas.config.settings import Settings
from repoatlas.models.capability import CapabilityContext, CapabilityScope
from repoatlas.models.topology import LinkKind, TopologyOverviewRequest
from repoatlas.services.workspace import Workspace, open_workspace
from tests.impact_corpus import build

REPOSITORIES = {
    "ops": {
        "etc/salt/master": (
            "file_roots:\n"
            "  prod:\n"
            "    - /srv/salt/prod\n"
            "  local:\n"
            "    - /srv/local/state\n"
            "pillar_roots:\n"
            "  prod:\n"
            "    - /srv/pillar/prod\n"
            "  dev:\n"
            "    - /srv/pillar/dev\n"
            "  local:\n"
            "    - /srv/local/pillar\n"
        )
    },
    "consumer": {
        "states/top.sls": "prod:\n  web01:\n    - consumer\n",
        "states/consumer.sls": (
            "consume:\n  cmd.run:\n"
            "    - name: echo {{ pillar['service']['host'] }}\n"
            "    - unless: {{ pillar['missing']['value'] }}\n"
        ),
    },
    "provider": {
        "pillar/top.sls": (
            "prod:\n"
            "  web01:\n"
            "    - service.common\n"
            "  web02:\n"
            "    - service.other\n"
        ),
        "pillar/service/common.sls": "service:\n  host: prod-provider\n",
        "pillar/service/other.sls": "service:\n  host: other-minion\n",
        "pillar-dev/top.sls": "dev:\n  web01:\n    - service.common\n",
        "pillar-dev/service/common.sls": "service:\n  host: dev-provider\n",
    },
    "unrelated": {
        "pillar/top.sls": "prod:\n  web01:\n    - service.common\n",
        "pillar/service/common.sls": "service:\n  host: unrelated-provider\n",
    },
    "local": {
        "state/top.sls": "local:\n  web01:\n    - consumer\n",
        "state/consumer.sls": "consume:\n  cmd.run:\n    - name: {{ pillar['local']['value'] }}\n",
        "pillar/top.sls": "local:\n  web01:\n    - local-pillar\n",
        "pillar/local-pillar.sls": "local:\n  value: same-repository\n",
    },
}


def _workspace_settings(tmp_path: Path) -> Settings:
    return Settings.model_validate(
        {
            "storage": {"database": str(tmp_path / "state" / "atlas.db")},
            "salt": {
                "installations": [
                    {
                        "name": "production",
                        "master_config": {"repository": "ops", "path": "etc/salt/master"},
                        "roots": [
                            {
                                "repository": "consumer",
                                "path": "states",
                                "role": "state",
                                "environment": "prod",
                                "salt_path": "/srv/salt/prod",
                            },
                            {
                                "repository": "provider",
                                "path": "pillar",
                                "role": "pillar",
                                "environment": "prod",
                                "salt_path": "/srv/pillar/prod",
                            },
                            {
                                "repository": "provider",
                                "path": "pillar-dev",
                                "role": "pillar",
                                "environment": "dev",
                                "salt_path": "/srv/pillar/dev",
                            },
                            {
                                "repository": "unrelated",
                                "path": "pillar",
                                "role": "pillar",
                                "environment": "prod",
                                "salt_path": "/srv/pillar/unrelated",
                            },
                            {
                                "repository": "local",
                                "path": "state",
                                "role": "state",
                                "environment": "local",
                                "salt_path": "/srv/local/state",
                            },
                            {
                                "repository": "local",
                                "path": "pillar",
                                "role": "pillar",
                                "environment": "local",
                                "salt_path": "/srv/local/pillar",
                            },
                        ],
                    }
                ]
            },
        }
    )


@pytest.fixture(scope="module")
def indexed(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Workspace]:
    tmp_path = tmp_path_factory.mktemp("salt-cross-repository")
    workspace = open_workspace(settings=_workspace_settings(tmp_path), require_root=False)
    build(workspace, tmp_path, REPOSITORIES)
    yield workspace
    workspace.close()


def _edges(workspace: Workspace, edge_type: str) -> list[dict[str, Any]]:
    rows = workspace.database.execute(
        """
        SELECT e.type, e.line_start, e.resolution,
               source_repo.slug AS source_repo, source_file.rel_path AS source_path,
               source.name AS source_name, target_repo.slug AS target_repo,
               target_file.rel_path AS target_path, target.name AS target_name
        FROM edges e
        JOIN entities source ON source.id = e.src_id
        JOIN entities target ON target.id = e.dst_id
        LEFT JOIN repositories source_repo ON source_repo.id = source.repo_id
        LEFT JOIN repositories target_repo ON target_repo.id = target.repo_id
        LEFT JOIN files source_file ON source_file.id = source.file_id
        LEFT JOIN files target_file ON target_file.id = target.file_id
        WHERE e.type = ?
        """,
        (edge_type,),
    ).fetchall()
    return [dict(row) for row in rows]


def test_same_repository_top_mapping_resolves_its_pillar_key(indexed: Workspace) -> None:
    reads = [
        edge
        for edge in _edges(indexed, "READS")
        if edge["source_repo"] == edge["target_repo"] == "local"
        and edge["target_path"] == "pillar/local-pillar.sls"
    ]
    assert len(reads) == 1
    assert reads[0]["source_path"] == "state/consumer.sls"


def test_cross_repository_pillar_mapping_creates_provider_to_consumer_edge(
    indexed: Workspace,
) -> None:
    generates = [
        edge
        for edge in _edges(indexed, "GENERATES")
        if edge["source_repo"] == "provider" and edge["target_repo"] == "consumer"
    ]
    assert len(generates) == 1
    assert generates[0]["source_path"] == "pillar/service/common.sls"
    assert generates[0]["target_path"] == "states/consumer.sls"
    assert generates[0]["line_start"] == 3
    resolution = json.loads(generates[0]["resolution"] or "{}")
    proof = resolution["salt_pillar"]
    assert proof["environment"] == "prod"
    assert proof["state_root"] == {
        "source": "master_config",
        "repository": "ops",
        "path": "etc/salt/master",
        "line": 3,
        "role": "state",
        "environment": "prod",
        "salt_path": "/srv/salt/prod",
    }
    assert proof["pillar_root"]["path"] == "etc/salt/master"
    assert proof["pillar_root"]["line"] == 8
    reads = [
        edge
        for edge in _edges(indexed, "READS")
        if edge["source_repo"] == "consumer"
        and edge["source_path"] == "states/consumer.sls"
        and edge["target_name"] == "pillar:service:host"
    ]
    assert len(reads) == 1 and reads[0]["target_repo"] is None

    overview = CapabilityService(indexed).topology_overview(
        TopologyOverviewRequest(
            context=CapabilityContext(scope=CapabilityScope.GLOBAL),
        )
    )
    [link] = [
        item
        for item in overview.links
        if item.source == "provider" and item.target == "consumer"
    ]
    assert link.kind is LinkKind.OBSERVED
    assert [(str(edge.type), edge.count) for edge in link.types] == [("GENERATES", 1)]
    assert not any(
        link.source == "consumer" and link.target == "provider" for link in overview.links
    )


def test_similarly_named_pillar_keys_outside_the_selected_mapping_do_not_link(
    indexed: Workspace,
) -> None:
    generates = _edges(indexed, "GENERATES")
    assert not any(edge["source_repo"] == "unrelated" for edge in generates)
    assert not any(edge["source_path"].startswith("pillar-dev/") for edge in generates)
    assert not any(edge["source_path"] == "pillar/service/other.sls" for edge in generates)


def test_unresolved_pillar_read_does_not_create_a_cross_repository_edge(
    indexed: Workspace,
) -> None:
    reads = [
        edge
        for edge in _edges(indexed, "READS")
        if edge["source_repo"] == "consumer"
        and edge["target_name"] == "pillar:missing:value"
    ]
    assert len(reads) == 1
    assert reads[0]["target_repo"] is None
    generates = _edges(indexed, "GENERATES")
    assert len(generates) == 1
    assert generates[0]["source_path"] == "pillar/service/common.sls"


def test_indexed_master_roots_inside_managed_repositories_need_no_root_overrides(
    tmp_path: Path,
) -> None:
    managed = tmp_path / "repos"
    state_root = (managed / "consumer" / "states").as_posix()
    pillar_root = (managed / "provider" / "pillar").as_posix()
    settings = Settings.model_validate(
        {
            "storage": {"database": str(tmp_path / "state" / "atlas.db")},
            "salt": {
                "installations": [
                    {
                        "name": "managed",
                        "master_config": {"repository": "ops", "path": "master"},
                    }
                ]
            },
        }
    )
    workspace = open_workspace(settings=settings, require_root=False)
    try:
        build(
            workspace,
            tmp_path,
            {
                "ops": {
                    "master": (
                        "file_roots:\n  prod:\n    - "
                        f"{state_root}\n"
                        "pillar_roots:\n  prod:\n    - "
                        f"{pillar_root}\n"
                    )
                },
                "consumer": {
                    "states/top.sls": "prod:\n  web01:\n    - consumer\n",
                    "states/consumer.sls": (
                        "consume:\n  cmd.run:\n"
                        "    - name: {{ pillar['service']['host'] }}\n"
                    ),
                },
                "provider": {
                    "pillar/top.sls": "prod:\n  web01:\n    - service.common\n",
                    "pillar/service/common.sls": "service:\n  host: managed-root\n",
                },
            },
        )

        generates = [
            edge
            for edge in _edges(workspace, "GENERATES")
            if edge["source_repo"] == "provider" and edge["target_repo"] == "consumer"
        ]
        assert len(generates) == 1
    finally:
        workspace.close()
