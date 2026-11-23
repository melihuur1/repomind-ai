"""What every tool that reads a repository checks first: that it is listed, current, and in its local copy.

search_code and get_verified_source both go through these, so that one never allows what the other refuses.
"""

from __future__ import annotations

import re

from repomind import PIPELINE_VERSION
from repomind.errors import RepoMindError
from repomind.ingest.git_source import GitRepository
from repomind.mcp_server.config import McpConfig
from repomind.mcp_server.repositories import LOCAL_PATH
from repomind.store.database import SnapshotInfo, Store

# An address of any kind, as a database address can appear in a lower layer's message.
_ADDRESS = re.compile(r"\b[a-z][a-z0-9+.-]*://\S+", re.IGNORECASE)


class Refused(Exception):
    """Why a tool did nothing, or could not finish. The message holds no path, address, secret or file content."""


def clean(text: str) -> str:
    """A lower layer's message, without local folders or addresses."""
    return LOCAL_PATH.sub("<local path>", _ADDRESS.sub("<address>", text))


def listed_name(config: McpConfig, repo: str) -> str:
    name = repo.strip().lower()
    if name not in config.repositories:
        # Whether it exists in the database is not said: only listed repositories are anyone's business.
        raise Refused("This repository is not one this server may show. list_repositories names those it may.")
    return name


def current_snapshot(store: Store, name: str) -> SnapshotInfo:
    snapshots = [snapshot for snapshot in store.active_snapshots([name]) if snapshot.repo == name]
    if not snapshots:
        raise Refused("This repository is not indexed in the database the server reads.")
    snapshot = snapshots[0]
    if not snapshot.is_current:
        raise Refused(
            f"This repository was indexed by an older version of RepoMind (index version {snapshot.pipeline_version}; "
            f"this version reads {PIPELINE_VERSION}). It cannot be read until it is indexed again."
        )
    return snapshot


def local_copy(snapshot: SnapshotInfo) -> GitRepository:
    """The repository's local copy, which must hold the indexed commit. Nothing is ever read from its working tree."""
    try:
        git = GitRepository(snapshot.source_path)
        resolved = git.resolve_commit(snapshot.commit_sha)
    except RepoMindError:
        raise Refused("The local copy of this repository cannot be read, so nothing can be checked against Git.") from None
    if resolved != snapshot.commit_sha:
        raise Refused("The indexed commit is not in the local copy of this repository, so nothing can be checked against Git.")
    return git
