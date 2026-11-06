"""Hidden and sensitive files must never reach the index; Git access is read-only."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from repomind.errors import RepoMindError
from repomind.ingest.filters import classify_content, classify_path, find_secret_rules, is_test_path
from repomind.ingest.git_source import GitRepository


@pytest.mark.parametrize(
    ("path", "reason"),
    [
        (".env", "hidden"),
        (".env.local.js", "hidden"),
        ("config/.secrets/keys.ts", "hidden"),
        (".github/scripts/release.js", "hidden"),
        ("src/.hidden.ts", "hidden"),
        (".git/hooks/pre-commit.js", "hidden"),
        ("node_modules/lodash/index.js", "excluded_directory"),
        ("apps/web/node_modules/x/y.ts", "excluded_directory"),
        ("target/classes/App.java", "excluded_directory"),
        ("dist/index.js", "excluded_directory"),
        ("build/out.js", "excluded_directory"),
        ("vendor/lib.js", "excluded_directory"),
        ("src/secrets.ts", "sensitive_name"),
        ("config/credentials.js", "sensitive_name"),
        ("config/Secret.java", "sensitive_name"),
        ("config/prod.env.js", "sensitive_name"),
        ("README.md", "unsupported_language"),
        ("Dockerfile", "unsupported_language"),
        ("server.pem", "unsupported_language"),
        ("types/index.d.ts", "generated"),
        ("public/app.min.js", "generated"),
    ],
)
def test_excluded_paths(path, reason):
    decision = classify_path(path)
    assert not decision.included
    assert decision.skip_reason == reason


@pytest.mark.parametrize(
    ("path", "language"),
    [
        ("src/main/java/A.java", "java"),
        ("apps/api/src/main.ts", "typescript"),
        ("apps/web/src/app.tsx", "tsx"),
        ("scripts/run.mjs", "javascript"),
        ("src/SecretRotationService.java", "java"),
        ("src/environment.ts", "typescript"),
    ],
)
def test_included_paths(path, language):
    decision = classify_path(path)
    assert decision.included
    assert decision.language == language


def test_oversized_file_is_rejected_without_reading_it():
    assert classify_path("src/huge.js", size=2_000_000).skip_reason == "too_large"


@pytest.mark.parametrize(
    "secret",
    [
        "-----BEGIN RSA PRIVATE KEY-----",
        "-----BEGIN PRIVATE KEY-----",
        "AKIAIOSFODNN7EXAMPLE",
        "ghp_" + "a" * 36,
        "sk-ant-" + "b" * 30,
        "sk-" + "c" * 40,
    ],
)
def test_content_with_a_secret_is_rejected(secret):
    text, reason = classify_content(f"const key = '{secret}';\n".encode())
    assert text is None
    assert reason == "contains_secret"


def test_secret_rules_do_not_flag_ordinary_code():
    assert find_secret_rules("const secretKey = process.env.JWT_SECRET;\nconst task = 'sk-short';\n") == []


def test_binary_non_utf8_and_minified_content_are_rejected():
    assert classify_content(b"abc\0def")[1] == "binary"
    assert classify_content(b"caf\xe9 = 1;\n")[1] == "not_utf8"
    assert classify_content(b"a" * 6000)[1] == "minified"


def test_is_test_path():
    assert is_test_path("src/test/java/com/AppTest.java")
    assert is_test_path("apps/api/src/auth.spec.ts")
    assert is_test_path("apps/api/test/notes.int-spec.ts")
    assert is_test_path("apps/web/e2e/helpers.ts")
    assert not is_test_path("src/main/java/com/Contest.java")
    assert not is_test_path("apps/api/src/latest.ts")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "sample"
    root.mkdir()
    _git(root, "init", "-b", "main")
    (root / "src").mkdir()
    (root / "src" / "app.ts").write_text("export function app() {\n  return 1;\n}\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "first commit")
    return root


def test_reads_committed_content_not_the_working_tree(repo: Path):
    git = GitRepository(repo)
    commit = git.resolve_commit()
    (repo / "src" / "app.ts").write_text("// uncommitted edit\n", encoding="utf-8")
    (repo / "src" / "untracked.ts").write_text("export const x = 1;\n", encoding="utf-8")

    entries = git.list_files(commit)
    assert [e.path for e in entries] == ["src/app.ts"]
    assert next(git.read_blobs(entries))[1] == b"export function app() {\n  return 1;\n}\n"
    assert git.read_file(commit, "src/untracked.ts") is None


def test_commit_info_and_default_name(repo: Path):
    git = GitRepository(repo)
    info = git.commit_info(git.resolve_commit())
    assert info.subject == "first commit"
    assert len(info.sha) == 40
    assert git.default_name() == "sample"
    _git(repo, "remote", "add", "origin", "https://github.com/acme/My-Project.git")
    assert git.default_name() == "my-project"


def test_reading_does_not_modify_the_repository(repo: Path):
    git = GitRepository(repo)
    commit = git.resolve_commit()
    git_dir = repo / ".git"
    before = {p: p.stat().st_mtime_ns for p in git_dir.rglob("*") if p.is_file()}
    list(git.read_blobs(git.list_files(commit)))
    git.commit_info(commit)
    after = {p: p.stat().st_mtime_ns for p in git_dir.rglob("*") if p.is_file()}
    assert before == after


def test_symlinks_are_not_listed(repo: Path):
    git = GitRepository(repo)
    blob = _git(repo, "rev-parse", "HEAD:src/app.ts")
    _git(repo, "update-index", "--add", "--cacheinfo", f"120000,{blob},link.ts")
    _git(repo, "commit", "-m", "add symlink")
    assert [e.path for e in git.list_files(git.resolve_commit())] == ["src/app.ts"]


def test_clear_errors_for_bad_input(tmp_path: Path, repo: Path):
    with pytest.raises(RepoMindError, match="does not exist"):
        GitRepository(tmp_path / "missing")
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(RepoMindError, match="Not a readable Git repository"):
        GitRepository(plain)
    with pytest.raises(RepoMindError, match="Cannot resolve commit"):
        GitRepository(repo).resolve_commit("no-such-ref")
