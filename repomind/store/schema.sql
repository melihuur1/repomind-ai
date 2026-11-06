CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS repositories (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    source_path TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One indexed commit. Everything below it is replaced as a unit, in one transaction.
CREATE TABLE IF NOT EXISTS snapshots (
    id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    repository_id    BIGINT NOT NULL REFERENCES repositories (id) ON DELETE CASCADE,
    commit_sha       TEXT NOT NULL CHECK (length(commit_sha) = 40),
    pipeline_version TEXT NOT NULL,
    committed_at     TIMESTAMPTZ NOT NULL,
    commit_subject   TEXT NOT NULL,
    stats            JSONB NOT NULL,
    is_active        BOOLEAN NOT NULL DEFAULT TRUE,
    indexed_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (repository_id, commit_sha, pipeline_version)
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_snapshot_per_repository
    ON snapshots (repository_id) WHERE is_active;

CREATE TABLE IF NOT EXISTS files (
    id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    snapshot_id       BIGINT NOT NULL REFERENCES snapshots (id) ON DELETE CASCADE,
    path              TEXT NOT NULL,
    language          TEXT NOT NULL,
    blob_sha          TEXT NOT NULL,
    line_count        INTEGER NOT NULL,
    has_syntax_errors BOOLEAN NOT NULL,
    UNIQUE (snapshot_id, path)
);

CREATE TABLE IF NOT EXISTS chunks (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    snapshot_id     BIGINT NOT NULL REFERENCES snapshots (id) ON DELETE CASCADE,
    path            TEXT NOT NULL,
    language        TEXT NOT NULL,
    kind            TEXT NOT NULL,
    symbol          TEXT,
    parent          TEXT,
    signature       TEXT,
    symbols         TEXT[] NOT NULL,
    start_line      INTEGER NOT NULL CHECK (start_line >= 1),
    end_line        INTEGER NOT NULL,
    part_index      INTEGER NOT NULL,
    part_total      INTEGER NOT NULL,
    is_test         BOOLEAN NOT NULL,
    content         TEXT NOT NULL,
    content_hash    TEXT NOT NULL,
    embed_text_hash TEXT NOT NULL,
    CHECK (end_line >= start_line)
);
CREATE INDEX IF NOT EXISTS chunks_snapshot ON chunks (snapshot_id);
CREATE INDEX IF NOT EXISTS chunks_embed_text_hash ON chunks (embed_text_hash);

CREATE TABLE IF NOT EXISTS symbols (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    snapshot_id    BIGINT NOT NULL REFERENCES snapshots (id) ON DELETE CASCADE,
    path           TEXT NOT NULL,
    kind           TEXT NOT NULL,
    name           TEXT NOT NULL,
    qualified_name TEXT NOT NULL,
    parent         TEXT,
    start_line     INTEGER NOT NULL,
    end_line       INTEGER NOT NULL,
    signature      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS symbols_snapshot ON symbols (snapshot_id);

CREATE TABLE IF NOT EXISTS imports (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    snapshot_id BIGINT NOT NULL REFERENCES snapshots (id) ON DELETE CASCADE,
    path        TEXT NOT NULL,
    module      TEXT NOT NULL,
    names       TEXT[] NOT NULL,
    line        INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS imports_snapshot ON imports (snapshot_id);

-- A cache keyed by the exact embedded text and model, deliberately independent of
-- snapshots: unchanged code is never embedded twice, across commits and repositories.
-- The column has no fixed dimension so several models can be stored and compared.
CREATE TABLE IF NOT EXISTS embeddings (
    text_hash  TEXT NOT NULL,
    model      TEXT NOT NULL,
    dim        INTEGER NOT NULL,
    embedding  vector NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (text_hash, model)
);

CREATE TABLE IF NOT EXISTS llm_usage (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    base_url      TEXT NOT NULL,
    model         TEXT NOT NULL,
    input_tokens  INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cost_usd      NUMERIC(12, 6) NOT NULL
);
