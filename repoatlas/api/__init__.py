"""The local HTTP API: RepoAtlas capabilities for a local app's server, on loopback.

A second transport beside the MCP adapter, under the same rule: it carries
requests to the capability service and responses back, and holds no repository
intelligence of its own. See ``docs/local-api.md`` for the contract.
"""
