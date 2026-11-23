"""Starts the MCP server over stdio. stdout carries only MCP messages; every log line goes to stderr."""

from __future__ import annotations

import logging
import sys

from repomind.mcp_server.config import ConfigurationError, load_config
from repomind.mcp_server.database import DatabaseUnavailable, check_database

log = logging.getLogger("repomind.mcp_server")


def main() -> int:
    # Before anything else, so that the SDK's own logging setup finds stderr already in place.
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="repomind-mcp %(levelname)s: %(message)s")
    try:
        config = load_config()
        check_database(config)
    except (ConfigurationError, DatabaseUnavailable) as exc:
        log.error("not starting: %s", exc)
        return 2

    from repomind.mcp_server.server import build_server

    log.info(
        "serving %d listed repositories from the database %s, read-only, over stdio",
        len(config.repositories),
        config.database,
    )
    try:
        build_server(config).run(transport="stdio")
    except KeyboardInterrupt:
        pass
    log.info("stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
