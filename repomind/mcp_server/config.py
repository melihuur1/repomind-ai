"""What the MCP server may read: one database, named, and the repositories listed. Both are required."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from repomind.config import Settings, get_setting

DATABASE_SETTING = "REPOMIND_MCP_DATABASE"
REPOSITORIES_SETTING = "REPOMIND_MCP_REPOSITORIES"
MAX_REPOSITORIES = 20
# A PostgreSQL name as RepoMind's databases are named: no quoting, nothing that could reach another server.
_DATABASE_NAME = re.compile(r"[a-z_][a-z0-9_]{0,62}")
# Names as indexing stores them: lower case, and owner/name for a repository added from GitHub.
_REPOSITORY_NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,99}(?:/[a-z0-9][a-z0-9._-]{0,99})?")


class ConfigurationError(Exception):
    """Why the server will not start. The message never holds an address, a user name or a password."""


@dataclass(frozen=True)
class McpConfig:
    database: str
    repositories: tuple[str, ...]
    # Holds the user name and password of the database. Never in repr, a log line or a message.
    database_url: str = field(repr=False)
    # The local embedding model semantic and hybrid search use, and the folder it is kept in. Nothing is downloaded.
    embedding_model: str = ""
    models_dir: Path | None = field(default=None, repr=False)


def load_config() -> McpConfig:
    """The database and repositories the server may read, or a ConfigurationError saying what is missing."""
    database = (get_setting(DATABASE_SETTING) or "").strip()
    if not database:
        raise ConfigurationError(f"{DATABASE_SETTING} is not set. Name the one database to read, such as repomind_openai_trial.")
    if not _DATABASE_NAME.fullmatch(database):
        raise ConfigurationError(f"{DATABASE_SETTING} must be a database name: lower-case letters, digits and underscores.")
    names = list(dict.fromkeys(name.strip().lower() for name in (get_setting(REPOSITORIES_SETTING) or "").split(",") if name.strip()))
    if not names:
        raise ConfigurationError(
            f"{REPOSITORIES_SETTING} is not set. List the repositories the server may show, separated by commas, such as library."
        )
    if any(not _REPOSITORY_NAME.fullmatch(name) for name in names):
        # The name is not repeated: a value pasted into the wrong setting could be a secret.
        raise ConfigurationError(f"{REPOSITORIES_SETTING} holds something that is not a repository name.")
    if len(names) > MAX_REPOSITORIES:
        raise ConfigurationError(f"{REPOSITORIES_SETTING} lists more than {MAX_REPOSITORIES} repositories.")
    settings = Settings.load()
    return McpConfig(database, tuple(names), _with_database(settings.database_url, database), settings.embedding_model, settings.models_dir)


def _with_database(url: str, database: str) -> str:
    """RepoMind's database address with another database's name. Host, user and password stay as set."""
    try:
        parts = urlsplit(url)
        valid = parts.scheme in ("postgresql", "postgres") and bool(parts.hostname)
    except ValueError:
        valid = False
    if not valid:
        raise ConfigurationError("REPOMIND_DATABASE_URL is not a PostgreSQL address the server can use.")
    return urlunsplit(parts._replace(path="/" + database))
