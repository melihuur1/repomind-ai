"""A local, read-only MCP server over RepoMind's index.

Run with `python -m repomind.mcp_server` (or `repomind-mcp`). It speaks MCP over stdio only, opens no
network port, and reads one database that REPOMIND_MCP_DATABASE names, showing only the repositories
REPOMIND_MCP_REPOSITORIES lists. It never reads the model settings and never asks a model.
"""
