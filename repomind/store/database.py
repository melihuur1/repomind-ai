"""All SQL lives here."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

import numpy as np
import psycopg
from pgvector.psycopg import register_vector
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from repomind import PIPELINE_VERSION
from repomind.errors import RepoMindError
from repomind.parsing.models import ParsedFile

_SCHEMA_PATH = Path(__file__).with_name("schema.sql")

_CHUNK_COLUMNS = """
    c.id, r.name AS repo, s.commit_sha, c.path, c.language, c.kind, c.symbol, c.parent, c.signature,
    c.symbols, c.start_line, c.end_line, c.part_index, c.part_total, c.is_test, c.content,
    c.content_hash, c.embed_text_hash
"""
_CHUNK_SOURCE = """
    FROM chunks c
    JOIN snapshots s ON s.id = c.snapshot_id
    JOIN repositories r ON r.id = s.repository_id
"""


@dataclass(frozen=True)
class ChunkRow:
    id: int
    repo: str
    commit_sha: str
    path: str
    language: str
    kind: str
    symbol: str | None
    parent: str | None
    signature: str | None
    symbols: list[str]
    start_line: int
    end_line: int
    part_index: int
    part_total: int
    is_test: bool
    content: str
    content_hash: str
    embed_text_hash: str


@dataclass(frozen=True)
class SnapshotInfo:
    id: int
    repo: str
    source_path: str
    commit_sha: str
    committed_at: object
    commit_subject: str
    stats: dict
    indexed_at: object
    origin_url: str | None = None
    pipeline_version: str = ""

    @property
    def is_current(self) -> bool:
        """Whether this version of RepoMind would store the same thing for the commit. See PIPELINE_VERSION."""
        return self.pipeline_version == PIPELINE_VERSION


class Store:
    def __init__(self, url: str, apply_schema: bool = True) -> None:
        """``apply_schema`` can be off for a short-lived connection, once something has applied it."""
        try:
            # Autocommit makes every `connection.transaction()` block a real transaction.
            # Otherwise an earlier SELECT opens an implicit one, the block degrades to a
            # savepoint, and all its writes vanish when the connection closes.
            self.connection = psycopg.connect(url, row_factory=dict_row, connect_timeout=5, autocommit=True)
        except psycopg.OperationalError as exc:
            raise RepoMindError(
                "Cannot connect to PostgreSQL.",
                hint=(
                    "Start RepoMind's own database with `docker compose up -d db` in the project folder, "
                    "and check REPOMIND_DATABASE_URL."
                ),
            ) from exc
        if apply_schema:
            self.connection.execute(_SCHEMA_PATH.read_text(encoding="utf-8"))
        register_vector(self.connection)

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ---------------------------------------------------------------- snapshots

    def upsert_repository(self, name: str, source_path: str) -> int:
        row = self.connection.execute(
            """
            INSERT INTO repositories (name, source_path) VALUES (%s, %s)
            ON CONFLICT (name) DO UPDATE SET source_path = EXCLUDED.source_path
            RETURNING id
            """,
            (name, source_path),
        ).fetchone()
        return row["id"]

    def find_snapshot(self, repository_id: int, commit_sha: str, pipeline_version: str) -> int | None:
        row = self.connection.execute(
            "SELECT id FROM snapshots WHERE repository_id = %s AND commit_sha = %s AND pipeline_version = %s",
            (repository_id, commit_sha, pipeline_version),
        ).fetchone()
        return row["id"] if row else None

    def replace_snapshot(
        self,
        repository_id: int,
        commit_sha: str,
        pipeline_version: str,
        committed_at: object,
        commit_subject: str,
        stats: dict,
        files: Sequence[tuple[ParsedFile, str]],
        embed_hashes: dict[int, str],
    ) -> int:
        """Atomically make this commit the repository's only snapshot.

        ``embed_hashes`` maps ``id(chunk)`` to the hash of its embedding text.
        """
        with self.connection.transaction():
            self.connection.execute("DELETE FROM snapshots WHERE repository_id = %s", (repository_id,))
            snapshot_id = self.connection.execute(
                """
                INSERT INTO snapshots
                    (repository_id, commit_sha, pipeline_version, committed_at, commit_subject, stats)
                VALUES (%s, %s, %s, %s, %s, %s) RETURNING id
                """,
                (repository_id, commit_sha, pipeline_version, committed_at, commit_subject, Jsonb(stats)),
            ).fetchone()["id"]
            with self.connection.cursor() as cursor:
                cursor.executemany(
                    """
                    INSERT INTO files (snapshot_id, path, language, blob_sha, line_count, has_syntax_errors)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    [
                        (snapshot_id, p.path, p.language, blob_sha, p.line_count, p.has_syntax_errors)
                        for p, blob_sha in files
                    ],
                )
                cursor.executemany(
                    """
                    INSERT INTO chunks
                        (snapshot_id, path, language, kind, symbol, parent, signature, symbols, start_line,
                         end_line, part_index, part_total, is_test, content, content_hash, embed_text_hash)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    [
                        (
                            snapshot_id, c.path, c.language, c.kind, c.symbol, c.parent, c.signature,
                            list(c.symbols), c.start_line, c.end_line, c.part_index, c.part_total, c.is_test,
                            c.content, c.content_hash, embed_hashes[id(c)],
                        )
                        for p, _ in files
                        for c in p.chunks
                    ],
                )
                cursor.executemany(
                    """
                    INSERT INTO symbols
                        (snapshot_id, path, kind, name, qualified_name, parent, start_line, end_line, signature)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    [
                        (snapshot_id, p.path, s.kind, s.name, s.qualified_name, s.parent, s.start_line, s.end_line, s.signature)
                        for p, _ in files
                        for s in p.symbols
                    ],
                )
                cursor.executemany(
                    "INSERT INTO imports (snapshot_id, path, module, names, line) VALUES (%s, %s, %s, %s, %s)",
                    [(snapshot_id, p.path, i.module, list(i.names), i.line) for p, _ in files for i in p.imports],
                )
        return snapshot_id

    def active_snapshots(self, repos: Iterable[str] | None = None) -> list[SnapshotInfo]:
        names = list(repos) if repos else None
        rows = self.connection.execute(
            """
            SELECT s.id, r.name AS repo, r.source_path, s.commit_sha, s.committed_at, s.commit_subject,
                   s.stats, s.indexed_at, r.origin_url, s.pipeline_version
              FROM snapshots s JOIN repositories r ON r.id = s.repository_id
             WHERE s.is_active AND (%(names)s::text[] IS NULL OR r.name = ANY(%(names)s))
             ORDER BY r.name
            """,
            {"names": names},
        ).fetchall()
        return [SnapshotInfo(**row) for row in rows]

    def snapshot_counts(self, snapshot_id: int) -> dict[str, int]:
        row = self.connection.execute(
            """
            SELECT (SELECT COUNT(*) FROM files WHERE snapshot_id = %(id)s) AS files,
                   (SELECT COUNT(*) FROM chunks WHERE snapshot_id = %(id)s) AS chunks,
                   (SELECT COUNT(*) FROM symbols WHERE snapshot_id = %(id)s) AS symbols,
                   (SELECT COUNT(*) FROM imports WHERE snapshot_id = %(id)s) AS imports
            """,
            {"id": snapshot_id},
        ).fetchone()
        return dict(row)

    def find_snapshot_is_current(self, snapshot_id: int, pipeline_version: str) -> bool:
        row = self.connection.execute("SELECT pipeline_version FROM snapshots WHERE id = %s", (snapshot_id,)).fetchone()
        return bool(row) and row["pipeline_version"] == pipeline_version

    def set_origin_url(self, name: str, url: str) -> None:
        self.connection.execute("UPDATE repositories SET origin_url = %s WHERE name = %s", (url, name))

    def delete_repository(self, name: str) -> None:
        """Removes what RepoMind stored about it. Embeddings stay, because they are a cache keyed by text."""
        self.connection.execute("DELETE FROM repositories WHERE name = %s", (name,))

    # ----------------------------------------------------------------- index jobs

    def create_job(self, repo: str, url: str) -> tuple[dict, bool]:
        """The job and whether it is new. A repository with an unfinished job keeps that one."""
        created = self.connection.execute(
            """
            INSERT INTO index_jobs (repo, url, status, phase) VALUES (%s, %s, 'queued', 'queued')
            ON CONFLICT (repo) WHERE status IN ('queued', 'running') DO NOTHING RETURNING *
            """,
            (repo, url),
        ).fetchone()
        return (created, True) if created else (self.active_job(repo), False)

    _JOB_FIELDS = frozenset({"repo", "url", "status", "phase", "done", "total", "outcome", "commit_sha", "error_code", "message"})

    def update_job(self, job_id: int, **fields: object) -> None:
        unknown = set(fields) - self._JOB_FIELDS
        if unknown:
            raise ValueError(f"Unknown job fields: {unknown}")
        assignments = ", ".join(f"{name} = %s" for name in fields)
        self.connection.execute(
            f"UPDATE index_jobs SET {assignments}, updated_at = now() WHERE id = %s",
            (*fields.values(), job_id),
        )

    def job(self, job_id: int) -> dict | None:
        return self.connection.execute("SELECT * FROM index_jobs WHERE id = %s", (job_id,)).fetchone()

    def active_job(self, repo: str) -> dict | None:
        return self.connection.execute(
            "SELECT * FROM index_jobs WHERE repo = %s AND status IN ('queued', 'running')", (repo,)
        ).fetchone()

    def count_active_jobs(self) -> int:
        return self.connection.execute(
            "SELECT COUNT(*) AS count FROM index_jobs WHERE status IN ('queued', 'running')"
        ).fetchone()["count"]

    def recent_jobs(self, limit: int = 20) -> list[dict]:
        return self.connection.execute("SELECT * FROM index_jobs ORDER BY id DESC LIMIT %s", (limit,)).fetchall()

    def forget_finished_jobs(self, repo: str) -> None:
        self.connection.execute("DELETE FROM index_jobs WHERE repo = %s AND status IN ('done', 'failed')", (repo,))

    def fail_unfinished_jobs(self, message: str) -> None:
        self.connection.execute(
            """
            UPDATE index_jobs SET status = 'failed', phase = 'failed', error_code = 'interrupted', message = %s, updated_at = now()
             WHERE status IN ('queued', 'running')
            """,
            (message,),
        )

    # -------------------------------------------------------------------- files

    def indexed_files(self, snapshot_id: int) -> list[dict]:
        """Only files that passed every filter are here, so this is what may be shown."""
        return self.connection.execute(
            "SELECT path, language, line_count, has_syntax_errors FROM files WHERE snapshot_id = %s ORDER BY path",
            (snapshot_id,),
        ).fetchall()

    def indexed_file(self, snapshot_id: int, path: str) -> dict | None:
        return self.connection.execute(
            "SELECT path, language, line_count, has_syntax_errors FROM files WHERE snapshot_id = %s AND path = %s",
            (snapshot_id, path),
        ).fetchone()

    def file_chunks(self, snapshot_id: int, path: str) -> list[ChunkRow]:
        """Every indexed chunk of one file, by line. Withheld lines are in none of them."""
        rows = self.connection.execute(
            f"SELECT {_CHUNK_COLUMNS} {_CHUNK_SOURCE} WHERE c.snapshot_id = %s AND c.path = %s ORDER BY c.start_line",
            (snapshot_id, path),
        ).fetchall()
        return [ChunkRow(**row) for row in rows]

    def type_paths(self, snapshot_id: int, name: str) -> list[str]:
        """Files declaring a class, interface, enum or record called ``name``."""
        rows = self.connection.execute(
            """
            SELECT DISTINCT path FROM symbols
             WHERE snapshot_id = %s AND name = %s AND kind IN ('class', 'interface', 'enum', 'record')
             ORDER BY path
            """,
            (snapshot_id, name),
        ).fetchall()
        return [row["path"] for row in rows]

    # ------------------------------------------------------------------- chunks

    def load_chunks(self, snapshot_ids: Sequence[int], include_tests: bool = True) -> list[ChunkRow]:
        rows = self.connection.execute(
            f"""
            SELECT {_CHUNK_COLUMNS} {_CHUNK_SOURCE}
             WHERE c.snapshot_id = ANY(%s) AND (%s OR NOT c.is_test)
             ORDER BY c.id
            """,
            (list(snapshot_ids), include_tests),
        ).fetchall()
        return [ChunkRow(**row) for row in rows]

    # --------------------------------------------------------------- embeddings

    def chunks_missing_embedding(self, model: str, snapshot_ids: Sequence[int]) -> list[ChunkRow]:
        """One chunk per distinct embedding text that has no vector for ``model``."""
        rows = self.connection.execute(
            f"""
            SELECT DISTINCT ON (c.embed_text_hash) {_CHUNK_COLUMNS} {_CHUNK_SOURCE}
             WHERE c.snapshot_id = ANY(%(snapshots)s)
               AND NOT EXISTS (SELECT 1 FROM embeddings e
                                WHERE e.text_hash = c.embed_text_hash AND e.model = %(model)s)
             ORDER BY c.embed_text_hash, c.id
            """,
            {"snapshots": list(snapshot_ids), "model": model},
        ).fetchall()
        return [ChunkRow(**row) for row in rows]

    def save_embeddings(self, model: str, items: Sequence[tuple[str, np.ndarray]]) -> None:
        """Committed per batch, so an interrupted run resumes instead of starting over."""
        with self.connection.transaction(), self.connection.cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO embeddings (text_hash, model, dim, embedding) VALUES (%s, %s, %s, %s)
                ON CONFLICT (text_hash, model) DO NOTHING
                """,
                [(text_hash, model, len(vector), vector) for text_hash, vector in items],
            )

    def count_embedded_chunks(self, model: str, snapshot_ids: Sequence[int]) -> tuple[int, int]:
        row = self.connection.execute(
            """
            SELECT COUNT(*) AS total, COUNT(e.text_hash) AS embedded
              FROM chunks c
              LEFT JOIN embeddings e ON e.text_hash = c.embed_text_hash AND e.model = %s
             WHERE c.snapshot_id = ANY(%s)
            """,
            (model, list(snapshot_ids)),
        ).fetchone()
        return row["embedded"], row["total"]

    def semantic_search(
        self,
        query_vector: np.ndarray,
        model: str,
        snapshot_ids: Sequence[int],
        limit: int,
        include_tests: bool = True,
    ) -> list[tuple[ChunkRow, float]]:
        """Exact cosine search, so the measured recall is not capped by an approximate index."""
        rows = self.connection.execute(
            f"""
            SELECT {_CHUNK_COLUMNS}, e.embedding <=> %(query)s AS distance
              {_CHUNK_SOURCE}
              JOIN embeddings e ON e.text_hash = c.embed_text_hash AND e.model = %(model)s
             WHERE c.snapshot_id = ANY(%(snapshots)s) AND (%(tests)s OR NOT c.is_test)
             ORDER BY distance, c.id
             LIMIT %(limit)s
            """,
            {
                "query": query_vector,
                "model": model,
                "snapshots": list(snapshot_ids),
                "tests": include_tests,
                "limit": limit,
            },
        ).fetchall()
        results = []
        for row in rows:
            distance = float(row.pop("distance"))
            results.append((ChunkRow(**row), 1.0 - distance))
        return results

    # ---------------------------------------------------------------- LLM usage

    def record_llm_usage(self, base_url: str, model: str, input_tokens: int, output_tokens: int, cost_usd: float) -> None:
        self.connection.execute(
            "INSERT INTO llm_usage (base_url, model, input_tokens, output_tokens, cost_usd) VALUES (%s, %s, %s, %s, %s)",
            (base_url, model, input_tokens, output_tokens, cost_usd),
        )

    def total_llm_cost(self) -> float:
        row = self.connection.execute("SELECT COALESCE(SUM(cost_usd), 0) AS total FROM llm_usage").fetchone()
        return float(row["total"])
