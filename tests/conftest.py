from __future__ import annotations

import hashlib
import os
import re
import subprocess
from pathlib import Path

import numpy as np
import psycopg
import pytest

from repomind.config import Settings
from repomind.errors import RepoMindError
from repomind.search.tokenizer import tokenize
from repomind.store.database import Store

TEST_DATABASE = "repomind_test"

# Every model setting RepoMind reads, taken from the code so that a new one is never missed.
MODEL_SETTINGS = sorted(set(re.findall(r"REPOMIND_LLM_[A-Z_]+", (Path(__file__).resolve().parents[1] / "repomind" / "qa" / "llm.py").read_text(encoding="utf-8"))))


@pytest.fixture(autouse=True)
def _no_model_settings_from_the_developers_env(request, monkeypatch):
    """The model settings of the developer's own .env and shell never reach a test: they may hold a real API key.

    A test that needs a setting sets it itself. Only the opt-in real-model test reads them, when
    REPOMIND_RUN_LLM_TESTS=1 asks for it.
    """
    if request.node.get_closest_marker("llm") and os.environ.get("REPOMIND_RUN_LLM_TESTS") == "1":
        return
    for name in MODEL_SETTINGS:
        monkeypatch.setenv(name, "")


class FakeEmbedder:
    """Deterministic bag-of-words vectors that record what they were asked to embed."""

    model_name = "fake/bag-of-words"
    dim = 128

    def __init__(self) -> None:
        self.embedded: list[str] = []

    def _vector(self, text: str) -> np.ndarray:
        vector = np.zeros(self.dim, dtype=np.float32)
        for token in tokenize(text):
            vector[int(hashlib.md5(token.encode()).hexdigest(), 16) % self.dim] += 1.0
        norm = np.linalg.norm(vector)
        return vector / norm if norm else vector + 1.0 / np.sqrt(self.dim)

    def embed_passages(self, texts):
        self.embedded.extend(texts)
        return [self._vector(text) for text in texts]

    def embed_query(self, text):
        return self._vector(text)


@pytest.fixture
def fake_embedder() -> FakeEmbedder:
    return FakeEmbedder()


@pytest.fixture
def store():
    """A separate database in RepoMind's container, emptied before each test."""
    url = Settings.load().database_url
    try:
        with psycopg.connect(url, autocommit=True, connect_timeout=3) as admin:
            exists = admin.execute("SELECT 1 FROM pg_database WHERE datname = %s", (TEST_DATABASE,)).fetchone()
            if not exists:
                admin.execute(f"CREATE DATABASE {TEST_DATABASE}")
    except psycopg.OperationalError:
        pytest.skip("RepoMind PostgreSQL is not running (docker compose up -d db)")
    test_url = url.rsplit("/", 1)[0] + "/" + TEST_DATABASE
    try:
        database = Store(test_url)
    except RepoMindError:
        pytest.skip("RepoMind PostgreSQL is not reachable")
    database.connection.execute(
        "TRUNCATE repositories, snapshots, files, chunks, symbols, imports, embeddings, llm_usage, index_jobs RESTART IDENTITY CASCADE"
    )
    yield database
    database.close()


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def commit_all(repo: Path, message: str) -> str:
    git(repo, "add", "-A", "-f")
    git(repo, "commit", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def write(root: Path, relative: str, content: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content.encode("utf-8"))


@pytest.fixture
def sample_repo(tmp_path: Path) -> Path:
    root = tmp_path / "shop"
    root.mkdir()
    git(root, "init", "-b", "main")
    write(
        root,
        "src/main/java/shop/OrderService.java",
        "package shop;\n\nimport java.util.UUID;\n\n"
        "public class OrderService {\n"
        "    /** Cancels an order and releases its stock. */\n"
        "    public void cancelOrder(UUID id) {\n"
        "        ledger.release(id);\n"
        "    }\n"
        "}\n",
    )
    write(
        root,
        "src/payment.ts",
        "import { charge } from './gateway';\n\n"
        "export function refundPayment(id: string) {\n"
        "  return charge(id, -1);\n"
        "}\n",
    )
    write(root, "src/test/java/shop/OrderServiceTest.java", "class OrderServiceTest {\n  void cancelOrderWorks() {}\n}\n")
    commit_all(root, "initial commit")
    return root
