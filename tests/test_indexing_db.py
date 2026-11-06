"""End-to-end indexing and search on a real pgvector database."""

from __future__ import annotations

from pathlib import Path

import pytest

from repomind import cli
from repomind.config import Settings
from repomind.errors import RepoMindError
from repomind.evaluation.citations import CitationVerifier
from repomind.indexing import embed_missing, index_repository
from repomind.ingest.git_source import GitRepository
from repomind.search.engine import SearchConfig, SearchEngine
from repomind.store.database import Store
from tests.conftest import commit_all, git, write

pytestmark = pytest.mark.db


def snapshot_of(store, name="shop"):
    return store.active_snapshots([name])[0]


def test_writes_survive_a_new_connection(store, sample_repo):
    """Regression: writes were once silently rolled back when the connection closed."""
    index_repository(store, str(sample_repo), name="shop")
    url = store.connection.info.dsn
    password = Settings.load().database_url.split(":")[2].split("@")[0]
    with Store(f"{url} password={password}") as second:
        assert second.active_snapshots(["shop"])[0].stats["chunks"] > 0


def test_metadata_is_complete(store, sample_repo):
    report = index_repository(store, str(sample_repo), name="shop")
    commit = git(sample_repo, "rev-parse", "HEAD")
    assert report.commit == commit
    chunks = store.load_chunks([snapshot_of(store).id])
    assert chunks
    for chunk in chunks:
        assert chunk.commit_sha == commit
        assert chunk.start_line >= 1
        assert chunk.end_line >= chunk.start_line
    symbols = store.connection.execute("SELECT qualified_name FROM symbols").fetchall()
    assert {"OrderService", "OrderService.cancelOrder", "refundPayment"} <= {s["qualified_name"] for s in symbols}
    imports = store.connection.execute("SELECT module FROM imports").fetchall()
    assert {"java.util.UUID", "./gateway"} <= {i["module"] for i in imports}


def test_every_stored_chunk_matches_the_commit(store, sample_repo):
    index_repository(store, str(sample_repo), name="shop")
    snapshot = snapshot_of(store)
    verifier = CitationVerifier(GitRepository(sample_repo), snapshot.commit_sha)
    for chunk in store.load_chunks([snapshot.id]):
        assert verifier.check(chunk.path, chunk.start_line, chunk.end_line, chunk.content).valid


class TestReindexing:
    def test_same_commit_is_a_no_op(self, store, sample_repo, fake_embedder):
        first = index_repository(store, str(sample_repo), fake_embedder, name="shop")
        assert not first.unchanged
        assert first.embedding.computed == len(fake_embedder.embedded) > 0
        ids_before = [c.id for c in store.load_chunks([snapshot_of(store).id])]
        fake_embedder.embedded.clear()

        second = index_repository(store, str(sample_repo), fake_embedder, name="shop")

        assert second.unchanged
        assert second.chunks == first.chunks
        assert fake_embedder.embedded == []
        assert second.embedding.computed == 0
        assert second.embedding.reused == first.chunks
        assert [c.id for c in store.load_chunks([snapshot_of(store).id])] == ids_before

    def test_force_reparses_but_still_does_not_reembed(self, store, sample_repo, fake_embedder):
        index_repository(store, str(sample_repo), fake_embedder, name="shop")
        ids_before = {c.id for c in store.load_chunks([snapshot_of(store).id])}
        fake_embedder.embedded.clear()

        report = index_repository(store, str(sample_repo), fake_embedder, name="shop", force=True)

        assert not report.unchanged
        assert fake_embedder.embedded == []
        assert ids_before.isdisjoint(c.id for c in store.load_chunks([snapshot_of(store).id]))

    def test_new_commit_embeds_only_what_changed(self, store, sample_repo, fake_embedder):
        first = index_repository(store, str(sample_repo), fake_embedder, name="shop")
        fake_embedder.embedded.clear()
        write(sample_repo, "src/payment.ts", "import { charge } from './gateway';\n\nexport function refundPayment(id: string) {\n  return charge(id, -2);\n}\n")
        new_commit = commit_all(sample_repo, "change refund")

        report = index_repository(store, str(sample_repo), fake_embedder, name="shop")

        assert report.commit == new_commit
        assert report.embedding.computed == 1
        assert len(fake_embedder.embedded) == 1
        assert "charge(id, -2)" in fake_embedder.embedded[0]
        assert report.embedding.reused == first.chunks - 1

    def test_only_one_active_snapshot_remains(self, store, sample_repo):
        index_repository(store, str(sample_repo), name="shop")
        write(sample_repo, "src/extra.ts", "export const extra = 1;\n")
        new_commit = commit_all(sample_repo, "second")
        index_repository(store, str(sample_repo), name="shop")
        rows = store.connection.execute("SELECT commit_sha FROM snapshots").fetchall()
        assert [r["commit_sha"] for r in rows] == [new_commit]

    def test_can_index_an_older_commit_by_ref(self, store, sample_repo):
        old = git(sample_repo, "rev-parse", "HEAD")
        write(sample_repo, "src/extra.ts", "export const extra = 1;\n")
        commit_all(sample_repo, "second")
        report = index_repository(store, str(sample_repo), name="shop", ref=old)
        assert report.commit == old
        paths = {c.path for c in store.load_chunks([snapshot_of(store).id])}
        assert "src/extra.ts" not in paths

    def test_identical_text_is_embedded_once_across_repositories(self, store, sample_repo, fake_embedder, tmp_path):
        index_repository(store, str(sample_repo), fake_embedder, name="shop")
        fake_embedder.embedded.clear()
        index_repository(store, str(sample_repo), fake_embedder, name="shop-copy")
        assert fake_embedder.embedded == []

    def test_embedding_resumes_after_interruption(self, store, sample_repo, fake_embedder):
        report = index_repository(store, str(sample_repo), name="shop")
        snapshot = snapshot_of(store)
        pending = store.chunks_missing_embedding(fake_embedder.model_name, [snapshot.id])
        assert len(pending) == report.chunks
        first = pending[0]
        store.save_embeddings(fake_embedder.model_name, [(first.embed_text_hash, fake_embedder.embed_query("x"))])

        resumed = embed_missing(store, fake_embedder, [snapshot.id])

        assert resumed.computed == report.chunks - 1
        assert resumed.reused == 1


class TestSecrets:
    @pytest.fixture
    def leaky_repo(self, tmp_path: Path) -> Path:
        root = tmp_path / "leaky"
        root.mkdir()
        git(root, "init", "-b", "main")
        write(root, "src/app.ts", "export function visible() {\n  return 'SAFE_MARKER';\n}\n")
        write(root, ".env", "API_KEY=DOTENV_MARKER\n")
        write(root, ".env.production.js", "export const key = 'DOTENV_JS_MARKER';\n")
        write(root, ".config/hidden.ts", "export const a = 'HIDDEN_DIR_MARKER';\n")
        write(root, "src/.private.ts", "export const a = 'HIDDEN_FILE_MARKER';\n")
        write(root, "src/secrets.ts", "export const a = 'SECRETS_NAME_MARKER';\n")
        write(root, "src/credentials.js", "export const a = 'CREDENTIALS_NAME_MARKER';\n")
        write(root, "config/prod.env.js", "export const a = 'ENV_NAME_MARKER';\n")
        write(root, "node_modules/lib/index.js", "export const a = 'NODE_MODULES_MARKER';\n")
        write(root, "dist/bundle.js", "export const a = 'DIST_MARKER';\n")
        write(root, "build/out.js", "export const a = 'BUILD_MARKER';\n")
        write(root, "target/Gen.java", "class Gen { String a = \"TARGET_MARKER\"; }\n")
        write(root, "vendor/lib.js", "export const a = 'VENDOR_MARKER';\n")
        write(root, "src/key.ts", "export const pem = '-----BEGIN RSA PRIVATE KEY-----KEY_MARKER';\n")
        write(root, "src/token.ts", "export const t = 'ghp_" + "a" * 36 + "';\n")
        commit_all(root, "everything committed, even what should not be")
        return root

    def test_nothing_sensitive_is_stored_anywhere(self, store, leaky_repo, fake_embedder):
        report = index_repository(store, str(leaky_repo), fake_embedder, name="leaky")
        rows = store.connection.execute(
            """
            SELECT path, content AS text FROM chunks
            UNION ALL SELECT path, path FROM files
            UNION ALL SELECT path, signature FROM symbols
            UNION ALL SELECT path, module FROM imports
            """
        ).fetchall()
        everything = "\n".join(f"{r['path']} {r['text']}" for r in rows) + "\n".join(fake_embedder.embedded)

        assert "SAFE_MARKER" in everything
        for marker in (
            "DOTENV", "HIDDEN_DIR", "HIDDEN_FILE", "SECRETS_NAME", "CREDENTIALS_NAME", "ENV_NAME",
            "NODE_MODULES", "DIST_", "BUILD_", "TARGET_", "VENDOR_", "KEY_MARKER", "ghp_",
        ):
            assert marker not in everything, f"{marker} leaked into the index"

        assert report.files_indexed == 1
        assert sorted(report.skipped_for_secrets) == ["src/key.ts", "src/token.ts"]

    def test_secret_values_are_not_in_the_report(self, store, leaky_repo):
        report = index_repository(store, str(leaky_repo), name="leaky")
        assert "KEY_MARKER" not in str(report)


class TestSearch:
    @pytest.fixture
    def engine(self, store, sample_repo, fake_embedder):
        index_repository(store, str(sample_repo), fake_embedder, name="shop")
        return SearchEngine(store, fake_embedder)

    def test_keyword_finds_symbol(self, engine):
        hits = engine.search("cancelOrder", "keyword").hits
        assert hits[0].chunk.path.endswith("OrderService.java") or hits[0].chunk.is_test
        assert any("OrderService.java" in h.chunk.path and not h.chunk.is_test for h in hits)
        assert "cancelorder" in hits[0].explanation["matched_terms"]

    def test_semantic_orders_by_cosine_similarity(self, engine):
        hits = engine.search("refund payment charge", "semantic", limit=10).hits
        assert hits[0].chunk.path == "src/payment.ts"
        similarities = [h.score for h in hits]
        assert similarities == sorted(similarities, reverse=True)
        assert all(-1.0001 <= s <= 1.0001 for s in similarities)

    def test_hybrid_explains_both_ranks(self, engine):
        hit = engine.search("refund payment", "hybrid").hits[0]
        assert hit.chunk.path == "src/payment.ts"
        assert set(hit.explanation["ranks"]) == {"keyword", "semantic"}
        assert hit.score == pytest.approx(
            sum(1 / (60 + rank) for rank in hit.explanation["ranks"].values())
        )

    def test_hybrid_matches_rrf_of_the_two_lists(self, engine):
        keyword = [h.chunk.id for h in engine.search("cancel order", "keyword", limit=50).hits]
        semantic = [h.chunk.id for h in engine.search("cancel order", "semantic", limit=50).hits]
        for hit in engine.search("cancel order", "hybrid", limit=50).hits:
            expected = 0.0
            if hit.chunk.id in keyword:
                expected += 1 / (60 + keyword.index(hit.chunk.id) + 1)
            if hit.chunk.id in semantic:
                expected += 1 / (60 + semantic.index(hit.chunk.id) + 1)
            assert hit.score == pytest.approx(expected)

    def test_tests_are_left_out_unless_the_query_asks_for_them(self, engine):
        for mode in ("keyword", "semantic", "hybrid"):
            assert not any(h.chunk.is_test for h in engine.search("cancelOrder", mode, limit=20).hits)
            assert any(h.chunk.is_test for h in engine.search("cancelOrder tests", mode, limit=20).hits)

    def test_tests_can_be_forced_either_way(self, store, sample_repo, fake_embedder):
        index_repository(store, str(sample_repo), fake_embedder, name="shop")
        forced_in = SearchEngine(store, fake_embedder, SearchConfig(include_tests=True))
        forced_out = SearchEngine(store, fake_embedder, SearchConfig(include_tests=False))
        assert any(h.chunk.is_test for h in forced_in.search("cancelOrder", "hybrid", limit=20).hits)
        assert not any(h.chunk.is_test for h in forced_out.search("cancelOrder tests", "hybrid", limit=20).hits)

    def test_turkish_query_adds_a_translated_list(self, store, sample_repo, fake_embedder):
        class Translator:
            calls = 0

            def translate(self, text):
                Translator.calls += 1
                return "refund payment"

        index_repository(store, str(sample_repo), fake_embedder, name="shop")
        engine = SearchEngine(store, fake_embedder, translator=Translator())

        result = engine.search("Ödeme iadesi nasıl yapılıyor?", "hybrid")
        assert result.translated_query == "refund payment"
        assert result.hits[0].chunk.path == "src/payment.ts"
        assert set(result.hits[0].explanation["ranks"]) >= {"keyword", "semantic_translated"}

        english = engine.search("refund payment", "hybrid")
        assert english.translated_query is None
        assert Translator.calls == 1

    def test_keyword_mode_uses_the_translation_too(self, store, sample_repo):
        class Translator:
            def translate(self, text):
                return "refund payment"

        index_repository(store, str(sample_repo), name="shop")
        hits = SearchEngine(store, translator=Translator()).search("Ödeme iadesi nasıl?", "keyword").hits
        assert hits[0].chunk.path == "src/payment.ts"

    def test_search_survives_a_translator_failure(self, store, sample_repo):
        class Broken:
            def translate(self, text):
                raise RepoMindError("model missing")

        index_repository(store, str(sample_repo), name="shop")
        result = SearchEngine(store, translator=Broken()).search("refund nasıl", "keyword")
        assert result.hits
        assert "without query translation" in result.warnings[0]

    def test_repository_scope(self, store, sample_repo, fake_embedder):
        index_repository(store, str(sample_repo), fake_embedder, name="shop")
        index_repository(store, str(sample_repo), fake_embedder, name="other")
        engine = SearchEngine(store, fake_embedder)
        assert {h.chunk.repo for h in engine.search("cancel", "hybrid", repos=["other"], limit=20).hits} == {"other"}

    def test_clear_errors(self, store, fake_embedder):
        engine = SearchEngine(store, fake_embedder)
        with pytest.raises(RepoMindError, match="Nothing has been indexed"):
            engine.search("x", "keyword")
        with pytest.raises(RepoMindError, match="empty"):
            engine.search("   ", "keyword")

    def test_semantic_without_embeddings_says_so(self, store, sample_repo, fake_embedder):
        index_repository(store, str(sample_repo), name="shop")
        with pytest.raises(RepoMindError, match="No embeddings exist"):
            SearchEngine(store, fake_embedder).search("x", "semantic")

    def test_keyword_search_needs_no_embedder(self, store, sample_repo):
        index_repository(store, str(sample_repo), name="shop")
        assert SearchEngine(store).search("cancelOrder", "keyword").hits


class TestCli:
    def test_bad_path_is_a_readable_error(self, capsys, tmp_path):
        code = cli.main(["index", "--path", str(tmp_path / "missing"), "--no-embed"])
        captured = capsys.readouterr()
        assert code == 2
        assert "error:" in captured.err
        assert "Traceback" not in captured.err

    def test_control_characters_are_stripped(self):
        # Tabs and newlines stay: code needs its indentation.
        assert cli.safe_text("ok\x1b[31mred\x1b[0m\x07 done\ttab\n") == "okred done\ttab\n"

    def test_empty_query_is_a_readable_error(self, capsys, monkeypatch, store):
        monkeypatch.setattr(cli, "_store", lambda settings: store.__class__.__new__(store.__class__))
        monkeypatch.setattr(Store, "__enter__", lambda self: store)
        monkeypatch.setattr(Store, "__exit__", lambda self, *a: None)
        code = cli.main(["search", "   ", "--mode", "keyword"])
        assert code == 2
        assert "empty" in capsys.readouterr().err
