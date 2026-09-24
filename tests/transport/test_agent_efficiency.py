"""New evidence modes and request refusals survive a real stdio process boundary."""

from pathlib import Path

from repoatlas.mcp.tools import TOOLS
from tests.test_http_structural_matching import CLIENT, SERVER, _index
from tests.transport.conftest import over_stdio, requires_console_script, structured

from mcp import ClientSession

pytestmark = requires_console_script


def test_preview_discovery_and_strict_filters_over_stdio(tmp_path: Path) -> None:
    source = 'class RosterService:\n    def remove(self, student):\n        return "removed"\n'
    ws, _ = _index(
        tmp_path,
        {
            "frontend": {"client.ts": CLIENT, "service.py": source},
            "backend-a": {"server.ts": SERVER},
            "backend-b": {"server.ts": SERVER},
        },
    )
    ws.close()

    async def body(session: ClientSession) -> None:
        listed = await session.list_tools()
        assert {tool.name: tool.inputSchema for tool in listed.tools} == {
            spec.name: spec.request.model_json_schema() for spec in TOOLS
        }
        searched = structured(
            await session.call_tool(
                "repoatlas_search",
                {
                    "context": {"scope": "repository", "repository": "frontend"},
                    "question": "RosterService.remove",
                    "source_preview": True,
                    "presentation": "compact",
                    "budget": {"max_items": 1},
                },
            )
        )
        item = searched["evidence"][0]
        assert item["text"] == "\n".join(source.splitlines()[1:])
        assert item["preview"] == {"source_span": [2, 3], "omitted_spans": []}
        assert not item["declaration"]
        discovered = structured(
            await session.call_tool(
                "repoatlas_http_operations",
                {
                    "context": {"scope": "global"},
                    "discover": True,
                    "route_prefix": "/v1/courses/:id/roster",
                    "method": "GET",
                    "include_provenance": True,
                    "investigation_id": searched["investigation_id"],
                },
            )
        )
        assert len(discovered["operations"]) == 4
        assert discovered["basis"]["status_scope"] == "source_repository"
        refs = {e["id"]: e["ref"]["repository_slug"] for e in discovered["evidence"]}
        for row in discovered["operations"]:
            assert refs[row["evidence_id"]] == row["repository"]
        refused = await session.call_tool(
            "repoatlas_dependencies", {"context": {"scope": "global"}, "package": "wrong"}
        )
        assert refused.isError and refused.structuredContent
        assert refused.structuredContent["error"]["code"] == "INVALID_REQUEST"

    over_stdio(tmp_path, body)
