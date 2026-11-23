"""PIPELINE_VERSION says what indexing stores for a commit. It has to change when that does.

The first test parses fixed files in every indexed language and compares a fingerprint of all that
would be stored with the one recorded for the current version. A change to parsing, chunking or
withholding changes the fingerprint. Then the version has to be bumped and the new fingerprint
recorded under it, in the same commit, or an index made before the change is taken for a current
one and never refreshed. Every credential below is made up.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict

import pytest

from repomind import PIPELINE_VERSION
from repomind.indexing import index_repository
from repomind.parsing.chunker import parse_source
from repomind.search.engine import SearchEngine
from tests.conftest import commit_all, git, write

# One line per version, never edited afterwards. The key is PIPELINE_VERSION when the fingerprint was recorded.
# "2" was computed from the published code at 4e4a71f, for the record. Between the two, a change to YAML
# chunking gave these files the fingerprint 608b5a70... while the version stayed 2. That is the mistake
# this test is here to stop.
FINGERPRINTS = {
    "2": "753cb7b3b0f7cb2752c809eef042e4609b96924ea314bf96b98ac12ba59742f8",
    "3": "1b3136472b6d874faefc477935fab214fdd1bf12c92f6f1035d0c2bb63054493",
}

LONG_METHOD = "".join(f"        total += weigh(items.get({n}), {n});\n" for n in range(140))
SAMPLES = {
    "src/main/java/shop/orders/OrderService.java": (
        "java",
        "package shop.orders;\n\nimport java.util.UUID;\nimport shop.stock.Ledger;\n\n"
        "/** Places and cancels orders. */\npublic class OrderService {\n"
        "    private static final int MAX_ITEMS = 50;\n"
        '    private static final String API_TOKEN = "fake-token-0123456789";\n'
        "    private final Ledger ledger;\n\n"
        "    public OrderService(Ledger ledger) {\n        this.ledger = ledger;\n    }\n\n"
        "    /** Cancels an order and releases its stock. */\n    public void cancelOrder(UUID id) {\n        ledger.release(id);\n    }\n\n"
        "    public boolean full(int count) {\n        return count >= MAX_ITEMS;\n    }\n\n"
        "    public int weight(java.util.List<Item> items) {\n        int total = 0;\n" + LONG_METHOD + "        return total;\n    }\n\n"
        "    enum Status { PLACED, CANCELLED }\n}\n",
    ),
    "src/test/java/shop/orders/OrderServiceTest.java": (
        "java",
        "package shop.orders;\n\nclass OrderServiceTest {\n    @Test\n    void cancelOrderReleasesStock() {\n        service.cancelOrder(ID);\n    }\n}\n",
    ),
    "web/src/orders.ts": (
        "typescript",
        "import { Ledger } from './ledger';\n\nexport const PAGE_SIZE = 20;\n"
        "export const SETTINGS = {\n  region: 'eu',\n  apiKey: 'fake-key-0123456789',\n  retries: 3,\n};\n\n"
        "export interface Order {\n  id: string;\n  total: number;\n}\n\n"
        "export class OrderList {\n  constructor(private readonly ledger: Ledger) {}\n\n"
        "  page(orders: Order[], index: number): Order[] {\n    return orders.slice(index * PAGE_SIZE, (index + 1) * PAGE_SIZE);\n  }\n}\n\n"
        "export function total(orders: Order[]): number {\n  return orders.reduce((sum, order) => sum + order.total, 0);\n}\n",
    ),
    "web/src/OrderRow.tsx": (
        "tsx",
        "import type { Order } from './orders';\n\nexport function OrderRow({ order }: { order: Order }) {\n"
        "  return (\n    <tr>\n      <td>{order.id}</td>\n      <td>{order.total}</td>\n    </tr>\n  );\n}\n",
    ),
    "scripts/seed.js": (
        "javascript",
        "const { connect } = require('./db');\n\nconst BATCH = 100;\n\n"
        "async function seed(rows) {\n  const db = await connect({ user: 'app', password: 'fake-password-0123456789' });\n"
        "  for (let i = 0; i < rows.length; i += BATCH) {\n    await db.insert(rows.slice(i, i + BATCH));\n  }\n}\n\nmodule.exports = { seed };\n",
    ),
    "db/migrations/V1__orders.sql": (
        "sql",
        "CREATE TABLE orders (\n    id UUID PRIMARY KEY,\n    total NUMERIC(12, 2) NOT NULL,\n    placed_at TIMESTAMPTZ NOT NULL DEFAULT now()\n);\n\n"
        "CREATE INDEX orders_placed_at ON orders (placed_at);\n\n"
        "CREATE TABLE order_items (\n    order_id UUID NOT NULL REFERENCES orders (id),\n    sku TEXT NOT NULL,\n    quantity INT NOT NULL CHECK (quantity > 0)\n);\n",
    ),
    "compose.yaml": (
        "yaml",
        "name: shop\nservices:\n  db:\n    image: postgres:16\n    environment:\n      POSTGRES_USER: shop\n      POSTGRES_PASSWORD: fakepass\n      POSTGRES_DB: shop\n"
        "    ports:\n      - \"5432:5432\"\n  api:\n    build: ./api\n    depends_on:\n      - db\n    environment:\n"
        "      DATABASE_URL: postgresql://shop:fakepass@db:5432/shop\n      LOG_LEVEL: info\n      JWT_SECRET: ${JWT_SECRET}\n"
        "volumes:\n  data: {}\n",
    ),
    "deploy/api.yml": (
        "yaml",
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: api\nspec:\n  replicas: 2\n  template:\n    spec:\n      containers:\n"
        "        - name: api\n          image: shop/api:1.4\n          env:\n            - name: LOG_LEVEL\n              value: info\n",
    ),
}


def fingerprint() -> str:
    stored = [asdict(parse_source(path, language, text)) for path, (language, text) in SAMPLES.items()]
    return hashlib.sha256(json.dumps(stored, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def test_the_version_changes_whenever_what_indexing_stores_does():
    now = fingerprint()
    recorded = FINGERPRINTS.get(PIPELINE_VERSION)
    assert recorded is not None, (
        f"PIPELINE_VERSION is {PIPELINE_VERSION}, and no fingerprint is recorded for it. Add this line to FINGERPRINTS: \"{PIPELINE_VERSION}\": \"{now}\""
    )
    assert now == recorded, (
        "What indexing stores for these files has changed, and PIPELINE_VERSION has not. An index made before this change would be "
        "taken for a current one. Bump PIPELINE_VERSION in repomind/__init__.py, say there what changed, and add a line to FINGERPRINTS "
        f"for the new version: \"{now}\". Do not edit the line of an older version."
    )


def test_no_two_versions_share_a_fingerprint():
    assert len(set(FINGERPRINTS.values())) == len(FINGERPRINTS), "a version that stores the same thing as another is not a new version"


def test_the_samples_reach_what_the_version_is_about():
    parsed = {path: parse_source(path, language, text) for path, (language, text) in SAMPLES.items()}
    stored = json.dumps([asdict(item) for item in parsed.values()])
    assert "fake-" not in stored and "fakepass" not in stored, "withholding is part of the output"
    assert {path for path, item in parsed.items() if item.withheld_chunks} == {
        "src/main/java/shop/orders/OrderService.java", "web/src/orders.ts", "scripts/seed.js", "compose.yaml",
    }
    assert any(chunk.part_total > 1 for item in parsed.values() for chunk in item.chunks), "a unit too long for one chunk is split"
    assert any(chunk.is_test for item in parsed.values() for chunk in item.chunks)
    assert {item.language for item in parsed.values()} == {"java", "typescript", "tsx", "javascript", "sql", "yaml"}


@pytest.fixture
def shop(store, tmp_path, fake_embedder):
    root = tmp_path / "shop"
    root.mkdir()
    git(root, "init", "-b", "main")
    for path, (_, text) in SAMPLES.items():
        write(root, path, text)
    commit_all(root, "shop")
    index_repository(store, str(root), fake_embedder, name="shop")
    return root


def make_older(store) -> None:
    """What an index made by an earlier version looks like to this one."""
    store.connection.execute("UPDATE snapshots SET pipeline_version = '2'")


@pytest.mark.db
class TestAnIndexMadeByAnOlderVersion:
    def test_the_same_commit_is_skipped_under_the_same_version_and_read_again_under_a_newer_one(self, store, shop, fake_embedder):
        assert index_repository(store, str(shop), fake_embedder, name="shop").unchanged is True
        assert store.active_snapshots(["shop"])[0].is_current
        make_older(store)
        assert not store.active_snapshots(["shop"])[0].is_current
        embedded_before = len(fake_embedder.embedded)
        report = index_repository(store, str(shop), fake_embedder, name="shop")
        assert report.unchanged is False, "no --force is needed: the version alone makes it read the commit again"
        snapshot = store.active_snapshots(["shop"])[0]
        assert snapshot.is_current and snapshot.pipeline_version == PIPELINE_VERSION
        assert len(fake_embedder.embedded) == embedded_before and report.embedding.computed == 0, "vectors are kept for text that did not change"
        assert index_repository(store, str(shop), fake_embedder, name="shop").unchanged is True

    def test_search_and_ask_say_so_until_it_is_indexed_again(self, store, shop, fake_embedder):
        from repomind.qa.engine import QAEngine

        search = SearchEngine(store, fake_embedder)
        outdated = "shop was indexed by an older version of RepoMind"
        assert not any(outdated in warning for warning in search.search("cancel order", "hybrid", ["shop"]).warnings)
        make_older(store)
        found = search.search("cancel order", "hybrid", ["shop"])
        assert found.hits, "it still answers"
        assert [warning for warning in found.warnings if outdated in warning and "Index it again" in warning]
        asked = QAEngine(store, search, None, None).ask("Where is an order cancelled and its stock released?", ["shop"])
        assert any(outdated in warning for warning in asked.warnings)
        index_repository(store, str(shop), fake_embedder, name="shop")
        assert not any(outdated in warning for warning in search.search("cancel order", "hybrid", ["shop"]).warnings)

    def test_the_list_of_repositories_says_which_one_to_index_again(self, store, shop, capsys, monkeypatch):
        from repomind import cli
        from repomind.config import Settings

        settings = Settings.load()
        monkeypatch.setattr(cli, "_store", lambda _settings: _Borrowed(store))
        cli.command_repos(None, settings)
        assert "OUTDATED" not in capsys.readouterr().out
        make_older(store)
        cli.command_repos(None, settings)
        shown = capsys.readouterr().out
        assert "OUTDATED" in shown and f"this version is {PIPELINE_VERSION}" in shown and "--name shop" in shown


class _Borrowed:
    """The test's own store, for a command that would open and close one."""

    def __init__(self, store) -> None:
        self.store = store

    def __enter__(self):
        return self.store

    def __exit__(self, *_exc) -> None:
        return None
