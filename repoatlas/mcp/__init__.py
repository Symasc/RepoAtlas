"""The MCP stdio transport adapter (RepoAtlas V2, Slice J).

A protocol binding over the frozen capability layer, and the top of the stack::

    MCP stdio  ->  CapabilityService  ->  V2 capabilities  ->  V1 substrate

The arrow runs one way. This package may name the capability service, the V2
request and response models, the public error envelope and the MCP SDK. It may
not name a store, a retriever, a traversal, a linker, a git client, a ledger or a
lock, and ``tests/test_layering.py`` asserts that rather than leaving it to
review. Nothing below this package may import it.

The rule that decides what belongs here: if a tool needed to reproduce retrieval,
inspection, traversal, dependency, grounding, sync, context, evidence-ledger,
freshness or error semantics, then the capability contract would be the thing
that is incomplete, and the missing piece belongs below. A transport that starts
interpreting is a second implementation of RepoAtlas reachable only over a
socket, and the two would disagree.
"""

from repoatlas.mcp.server import serve
from repoatlas.mcp.tools import TOOLS, ToolSpec

__all__ = ["TOOLS", "ToolSpec", "serve"]
