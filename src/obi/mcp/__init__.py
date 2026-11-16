"""The MCP tool surface (feature 101): obi's read-only tool registry for MCP clients.

A thin adapter in the same class as ``obi.chat`` / ``obi.web`` (Constitution Principle III):
no business logic lives here — the served tool list is derived from the one registry in
``obi.core.toolset`` and dispatch reuses chat's exact wiring. The package name does not shadow
the MCP SDK's top-level ``mcp`` package (absolute imports resolve that one).
"""
