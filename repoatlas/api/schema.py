"""The local API's contract as one JSON Schema document: ``python -m repoatlas.api.schema``.

Read from the same registry the server routes with, so a path cannot be served
without being described or described without being served. It is the source a
client generates its types from.

Each schema is its own root, with its own ``$defs``: a request schema describes
the POST body, and a response schema describes ``result`` in a success envelope
when the request asked for the full presentation. A request that can ask for a
compact one says so in its own schema (``presentation``); compact payloads are
not described here, and a client that wants the described shape passes
``presentation: "full"``.
"""

from __future__ import annotations

import json
from typing import Any

from repoatlas.api.server import CONTRACT, ENDPOINTS, PREFIX, ApiIndex, ErrorEnvelope


def contract_schema() -> dict[str, Any]:
    return {
        "contract": CONTRACT,
        "index": {
            "method": "GET",
            "path": PREFIX,
            "response": ApiIndex.model_json_schema(mode="serialization"),
        },
        "endpoints": [
            {
                "name": endpoint.name,
                "method": "POST",
                "path": endpoint.path,
                "request": endpoint.request.model_json_schema(),
                "response": endpoint.response.model_json_schema(mode="serialization"),
            }
            for endpoint in ENDPOINTS
        ],
        "success": {
            "description": 'Every success is {"ok": true, "result": <response>}.',
        },
        "error": ErrorEnvelope.model_json_schema(mode="serialization"),
    }


def main() -> None:
    print(json.dumps(contract_schema(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
