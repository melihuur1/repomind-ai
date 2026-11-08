"""SQL and YAML: exact lines, structure-aware chunks, and nothing is ever executed."""

from __future__ import annotations

import textwrap

import pytest

from repomind.indexing import index_repository
from repomind.ingest.filters import classify_content, classify_path, credential_lines, find_config_secret
from repomind.parsing.chunker import parse_source
from tests.conftest import commit_all, git, write

SQL = textwrap.dedent(
    """\
    -- Stock lives apart from products.
    CREATE TABLE stock_levels (
        product_id UUID PRIMARY KEY,
        -- a semicolon here; must not split the statement
        note TEXT DEFAULT 'a;b',
        CONSTRAINT ck CHECK (available >= 0)
    );

    /* Partial index for the sweeper. */
    CREATE INDEX ix_pending ON reservations (expires_at) WHERE status = 'PENDING';

    CREATE FUNCTION guard() RETURNS trigger AS $$
    BEGIN
        RAISE EXCEPTION 'no; way';
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;

    CREATE TRIGGER tg_guard
        BEFORE UPDATE ON reservations
        FOR EACH ROW EXECUTE FUNCTION guard();

    ALTER TABLE reservations ADD COLUMN extra TEXT;
    INSERT INTO settings VALUES ('x');
    -- trailing comment
    """
)

YAML = textwrap.dedent(
    """\
    # Top comment
    spring:
      application:
        name: demo
      datasource:
        url: jdbc:postgresql://localhost/demo
        password: ${DB_PASSWORD:}
        # Keep above the deadlock timeout.
        init-sql: "SET lock_timeout = '3s'"
    server:
      port: 8080
    app:
      a: 1
      b: 2
      c: 3
      d: 4
      e: 5
      f: 6
      g: 7
      h: 8
      i: 9
      j: 10
      k: 11
      l: 12
      m: 13
      nested:
        x: 1
        y: 2
    """
)


def lines_match(text, parsed):
    lines = text.split("\n")
    for chunk in parsed.chunks:
        assert chunk.content == "\n".join(lines[chunk.start_line - 1 : chunk.end_line])


class TestSql:
    parsed = parse_source("db/V1__init.sql", "sql", SQL)

    def by_symbol(self, name):
        return next(c for c in self.parsed.chunks if c.symbol == name)

    def test_lines_are_exact(self):
        lines_match(SQL, self.parsed)

    def test_objects_become_symbols(self):
        kinds = {s.name: s.kind for s in self.parsed.symbols}
        assert kinds == {
            "stock_levels": "table",
            "ix_pending": "index",
            "guard": "function",
            "tg_guard": "trigger",
            "reservations": "table",
        }

    def test_semicolons_in_strings_comments_and_dollar_quotes_do_not_split(self):
        assert self.by_symbol("stock_levels").content.endswith(");")
        function = self.by_symbol("guard")
        assert "RETURN NEW;" in function.content
        assert function.content.endswith("$$ LANGUAGE plpgsql;")

    def test_comment_stays_with_its_statement(self):
        assert self.by_symbol("stock_levels").content.startswith("-- Stock lives apart")
        assert self.by_symbol("ix_pending").content.startswith("/* Partial index")

    def test_other_statements_and_comments_are_kept(self):
        contents = [c.content for c in self.parsed.chunks]
        assert any("INSERT INTO settings" in c for c in contents)
        assert any(c == "-- trailing comment" for c in contents)

    def test_oversized_statement_is_split_with_exact_lines(self):
        columns = ",\n".join(f"    column_number_{i} VARCHAR(255) NOT NULL DEFAULT 'value'" for i in range(90))
        text = f"CREATE TABLE wide (\n{columns}\n);\n"
        parsed = parse_source("wide.sql", "sql", text)
        assert len(parsed.chunks) > 1
        assert all(c.symbol == "wide" for c in parsed.chunks)
        lines_match(text, parsed)

    def test_unterminated_statement_is_kept(self):
        parsed = parse_source("x.sql", "sql", "SELECT 1\n")
        assert parsed.chunks[0].content == "SELECT 1"


class TestYaml:
    parsed = parse_source("config/application.yml", "yaml", YAML)

    def test_lines_are_exact(self):
        lines_match(YAML, self.parsed)

    def test_top_level_key_with_comment(self):
        spring = next(c for c in self.parsed.chunks if c.symbol == "spring")
        assert spring.content.startswith("# Top comment")
        assert "lock_timeout" in spring.content

    def test_large_mapping_is_split_by_key_path(self):
        symbols = {c.symbol for c in self.parsed.chunks}
        assert "app.nested" in symbols
        assert "app" in symbols
        assert "server" in symbols

    def test_key_paths_are_symbols(self):
        names = {s.qualified_name for s in self.parsed.symbols}
        assert {"spring", "spring.datasource", "spring.datasource.password", "app.nested.x"} <= names

    def test_symbols_never_hold_values(self):
        text = "db:\n  password: hunter2\n  host: h\n"
        parsed = parse_source("c.yml", "yaml", text)
        assert all("hunter2" not in f"{s.signature}{s.qualified_name}" for s in parsed.symbols)

    def test_tags_are_plain_text(self, tmp_path):
        marker = tmp_path / "executed"
        text = f"a: !!python/object/apply:os.system ['touch {marker}']\nb: 1\n"
        parsed = parse_source("evil.yml", "yaml", text)
        assert not marker.exists()
        assert any("!!python" in c.content for c in parsed.chunks)

    def test_scalar_document_is_kept(self):
        assert parse_source("s.yml", "yaml", "just text\n").chunks

    def test_several_documents(self):
        text = "a: 1\n---\nb: 2\n"
        parsed = parse_source("m.yml", "yaml", text)
        assert {c.symbol for c in parsed.chunks} == {"a", "b"}
        lines_match(text, parsed)


class TestSecretsInConfig:
    @pytest.mark.parametrize(
        "line",
        [
            "password: hunter2",
            "  POSTGRES_PASSWORD: syncnotes",
            "api_key: abc123",
            '  "client-secret": "xyz"',
            "  JWT_SECRET: ${JWT_SECRET:-dev-secret-value}",
            "  jwt-secret: ${JWT_SECRET:dev-secret-value}",
            "# password: hunter2",
            "url: postgresql://user:hunter2@db:5432/x",
        ],
    )
    def test_literal_credentials_are_detected(self, line):
        assert find_config_secret(line)

    @pytest.mark.parametrize(
        "line",
        [
            "password: ${DB_PASSWORD}",
            "password: ${DB_PASSWORD:}",
            "POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?Set it in .env}",
            "password:",
            "password: null",
            "token-ttl: 60m",
            "token_expiry: 3600",
            "secret-name: my-k8s-secret",
            "api-key-header: X-Api-Key",
            "password: {{ vault_password }}",
            "url: jdbc:postgresql://localhost:5442/stockguard",
            "url: ${DB_URL:jdbc:postgresql://localhost:5442/stockguard}",
            "image: postgres:17-alpine",
            "tokens: true",
        ],
    )
    def test_placeholders_and_ordinary_settings_are_not_flagged(self, line):
        assert find_config_secret(line) is None

    def test_only_the_chunk_with_a_credential_is_withheld(self):
        text = (
            "services:\n"
            "  db:\n"
            "    environment:\n"
            "      POSTGRES_PASSWORD: CHUNK_SECRET\n"
            "    image: postgres\n"
            "    a: 1\n    b: 2\n    c: 3\n    d: 4\n    e: 5\n    f: 6\n"
            "  cache:\n"
            "    command: ['redis-server', '--appendonly', 'yes']\n"
            "    a: 1\n    b: 2\n    c: 3\n    d: 4\n    e: 5\n"
        )
        parsed = parse_source("docker-compose.yml", "yaml", text)
        assert parsed.withheld_chunks == 1
        joined = "\n".join(c.content for c in parsed.chunks)
        assert "CHUNK_SECRET" not in joined
        assert "--appendonly" in joined
        assert "image: postgres" in joined, "the credential's own service stays searchable too"

    SHORT_SERVICES = (
        "services:\n"
        "  migrate:\n"
        "    build: { context: ., target: migrate }\n"
        "    environment:\n"
        "      DATABASE_URL: postgresql://app:URL_SECRET@db:5432/app\n"
        "    depends_on:\n"
        "      db: { condition: service_healthy }\n"
        "  api:\n"
        "    image: app/api\n"
        "    environment:\n"
        "      PORT: 4000\n"
        "      JWT_SECRET: ${JWT_SECRET:-DEFAULT_SECRET}\n"
        "      ORIGIN: http://localhost:8080\n"
        "    depends_on:\n"
        "      migrate: { condition: service_completed_successfully }\n"
    )

    def test_a_short_service_keeps_every_entry_but_the_one_with_the_credential(self):
        parsed = parse_source("docker-compose.yml", "yaml", self.SHORT_SERVICES)
        joined = "\n".join(c.content for c in parsed.chunks)
        assert "URL_SECRET" not in joined and "DEFAULT_SECRET" not in joined
        assert parsed.withheld_chunks == 2
        waits = next(c for c in parsed.chunks if "service_completed_successfully" in c.content)
        assert waits.symbol.startswith("services.api"), "the chunk still says which service waits"
        assert "target: migrate" in joined and "service_healthy" in joined

    def test_a_flat_list_of_settings_with_a_credential_is_withheld_whole_and_not_in_pieces(self):
        parsed = parse_source("docker-compose.yml", "yaml", self.SHORT_SERVICES)
        joined = "\n".join(c.content for c in parsed.chunks)
        assert "PORT: 4000" not in joined, "one variable alone says little, and would only crowd search"
        assert not any((c.symbol or "").endswith("environment") for c in parsed.chunks)

    @pytest.mark.parametrize(
        "text",
        [
            SHORT_SERVICES,
            "db:\n  # password: COMMENT_SECRET\n  pool:\n    size: 5\n  host: h\n",
            "a:\n  b:\n    c:\n      token: DEEP_SECRET\n      d: 1\n    e: 2\n  f: 3\n",
            "list:\n  - password: ITEM_SECRET\n    name: x\n  - name: y\n",
            "x: 1\npassword: TOP_SECRET\ny: 2\n",
        ],
    )
    def test_no_chunk_holds_a_line_with_a_credential(self, text):
        parsed = parse_source("config.yml", "yaml", text)
        flagged = set(credential_lines(text))
        assert flagged, "each text here holds a credential"
        for chunk in parsed.chunks:
            assert find_config_secret(chunk.content) is None
            assert not flagged & set(range(chunk.start_line, chunk.end_line + 1)), f"{chunk.symbol} holds a credential line"
        assert "_SECRET" not in "\n".join(c.content for c in parsed.chunks)


@pytest.mark.parametrize(
    ("path", "reason"),
    [
        (".env", "hidden"),
        (".github/workflows/ci.yml", "hidden"),
        ("config/secrets.yaml", "sensitive_name"),
        ("config/credentials.yml", "sensitive_name"),
        ("application-local.yml", "sensitive_name"),
        ("values.private.yaml", "sensitive_name"),
        ("docker-compose.override.yml", "sensitive_name"),
        ("pnpm-lock.yaml", "generated"),
        ("db/dump.sql", "data_dump"),
        ("db/seed_users.sql", "data_dump"),
        ("backup-2024.sql", "data_dump"),
    ],
)
def test_sensitive_and_generated_paths_stay_out(path, reason):
    assert classify_path(path).skip_reason == reason


@pytest.mark.parametrize(
    "path",
    ["compose.yaml", "src/main/resources/application.yml", "locale.yml", "db/migration/V1__users.sql", "schema.sql"],
)
def test_ordinary_config_and_migrations_are_indexed(path):
    assert classify_path(path).included


def test_sql_with_a_literal_password_is_rejected():
    raw = b"CREATE USER app WITH PASSWORD 'hunter2';\n"
    assert classify_content(raw, "sql")[1] == "contains_secret"
    assert classify_content(b"ALTER TABLE u ADD COLUMN password TEXT;\n", "sql")[1] is None


@pytest.mark.db
def test_indexing_stores_sql_and_yaml_as_text_and_runs_nothing(store, fake_embedder, tmp_path):
    root = tmp_path / "config-repo"
    root.mkdir()
    git(root, "init", "-b", "main")
    write(root, "db/V1.sql", "-- destructive on purpose\nDROP TABLE chunks;\nCREATE TABLE harmless (id INT);\n")
    write(root, "compose.yaml", "services:\n  cache:\n    image: redis\n  db:\n    environment:\n      POSTGRES_PASSWORD: STORED_SECRET\n")
    write(root, "secrets.yaml", "api_key: FILE_SECRET\n")
    write(root, "seed.sql", "INSERT INTO users VALUES ('person@example.com');\n")
    commit_all(root, "config")

    report = index_repository(store, str(root), fake_embedder, name="config-repo")

    # The chunks table survived the "DROP TABLE" and the statement is only text.
    rows = store.connection.execute("SELECT content FROM chunks").fetchall()
    joined = "\n".join(row["content"] for row in rows) + "\n".join(fake_embedder.embedded)
    assert "DROP TABLE chunks" in joined
    assert store.connection.execute("SELECT to_regclass('harmless') AS t").fetchone()["t"] is None
    assert "STORED_SECRET" not in joined
    assert "FILE_SECRET" not in joined
    assert "person@example.com" not in joined
    assert report.config_chunks_withheld == {"compose.yaml": 1}
    assert report.skipped["data_dump"] == 1
