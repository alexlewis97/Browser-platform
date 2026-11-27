"""Workspaces: named groups of workflows that act as one scoped assistant (and MCP server)."""

from browser_platform.workspaces.store import ALL, Workspace, WorkspaceStore, resolve_scope

__all__ = ["ALL", "Workspace", "WorkspaceStore", "resolve_scope"]
