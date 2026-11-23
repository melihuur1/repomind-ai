"""A read-only session on the one database the configuration names, checked before anything is read."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from repomind.errors import RepoMindError
from repomind.mcp_server.config import DATABASE_SETTING, McpConfig
from repomind.store.database import Store


class DatabaseUnavailable(Exception):
    """The database cannot be read safely. The message never holds an address, a user name or a password."""


@contextmanager
def read_only_store(config: McpConfig) -> Iterator[Store]:
    """A Store whose session refuses every write, on the database the configuration names.

    The schema is not applied. The session is made read-only before anything is read, and the server
    checks that it is connected to the named database and that the session really refuses writes.
    A separate database role that may only read would be a second guard: see docs/architecture.md.
    """
    try:
        store = Store(config.database_url, apply_schema=False)
    except RepoMindError:
        raise DatabaseUnavailable(
            f"RepoMind's database cannot be reached, or the database {DATABASE_SETTING} names does not exist. "
            "Start it with: docker compose up -d db"
        ) from None
    except Exception:  # noqa: BLE001 - whatever stops it, the cause may name the server, and is not repeated
        raise DatabaseUnavailable("RepoMind's database cannot be read.") from None
    try:
        connection = store.connection
        connection.execute("SET default_transaction_read_only = on")
        row = connection.execute(
            "SELECT current_database() AS name, current_setting('default_transaction_read_only') AS read_only"
        ).fetchone()
        if row["name"] != config.database:
            raise DatabaseUnavailable(f"The session is not on the database {DATABASE_SETTING} names, so nothing was read.")
        if row["read_only"] != "on":
            raise DatabaseUnavailable("The session could not be made read-only, so nothing was read.")
        yield store
    finally:
        store.close()


def check_database(config: McpConfig) -> None:
    """Opens the read-only session once, so that a server that cannot read safely does not start."""
    with read_only_store(config):
        pass
