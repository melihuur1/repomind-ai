"""Decides which committed files may be indexed.

Path rules run first, on Git metadata alone, so an excluded file's content is
never even read. Content rules then catch binaries, minified bundles and files
that contain high-confidence credentials: a committed secret must not reach the
index or, later, an LLM.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath

MAX_FILE_BYTES = 1_000_000
MAX_LINE_LENGTH = 5_000
MAX_AVERAGE_LINE_LENGTH = 500

LANGUAGE_BY_EXTENSION = {
    ".java": "java",
    ".ts": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".tsx": "tsx",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".jsx": "javascript",
}

EXCLUDED_DIRECTORIES = frozenset(
    {
        "node_modules",
        "bower_components",
        "jspm_packages",
        "vendor",
        "site-packages",
        "venv",
        "target",
        "dist",
        "build",
        "out",
        "coverage",
        "__pycache__",
        "__generated__",
    }
)

GENERATED_SUFFIXES = (".d.ts", ".min.js", ".min.mjs", ".bundle.js", ".chunk.js")

SENSITIVE_FILE_STEMS = frozenset({"secret", "secrets", "credential", "credentials", "creds"})

_SECRET_PATTERNS = {
    "private-key": re.compile(r"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY-----"),
    "aws-access-key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "github-token": re.compile(r"\b(?:ghp|gho|ghs|ghu|ghr)_[A-Za-z0-9]{36}\b|\bgithub_pat_[A-Za-z0-9_]{40,}\b"),
    "anthropic-key": re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b"),
    "openai-key": re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{32,}\b"),
    "google-api-key": re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    "slack-token": re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
}


@dataclass(frozen=True)
class PathDecision:
    language: str | None
    skip_reason: str | None

    @property
    def included(self) -> bool:
        return self.skip_reason is None


def classify_path(path: str, size: int = 0) -> PathDecision:
    pure = PurePosixPath(path)
    for part in pure.parts:
        if part.startswith("."):
            return PathDecision(None, "hidden")
    for part in pure.parts[:-1]:
        if part.lower() in EXCLUDED_DIRECTORIES:
            return PathDecision(None, "excluded_directory")

    name = pure.name.lower()
    language = LANGUAGE_BY_EXTENSION.get(pure.suffix.lower())
    name_parts = name.split(".")
    if name_parts[0] in SENSITIVE_FILE_STEMS or "env" in name_parts[:-1]:
        return PathDecision(None, "sensitive_name")
    if language is None:
        return PathDecision(None, "unsupported_language")
    if name.endswith(GENERATED_SUFFIXES):
        return PathDecision(None, "generated")
    if size > MAX_FILE_BYTES:
        return PathDecision(None, "too_large")
    return PathDecision(language, None)


def find_secret_rules(text: str) -> list[str]:
    """Names of the secret rules that match. The secret itself is never returned."""
    return [name for name, pattern in _SECRET_PATTERNS.items() if pattern.search(text)]


def normalize_source(raw: bytes) -> str | None:
    """Decode a blob into the canonical text all line numbers refer to.

    Only ``\\n`` separates lines. That matches both Tree-sitter's row counting and
    ``git show | sed -n``, whereas ``str.splitlines`` would also break on form
    feeds and U+2028 and silently shift every later line number.
    """
    if b"\0" in raw:
        return None
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None
    return text.replace("\r\n", "\n")


def classify_content(raw: bytes) -> tuple[str | None, str | None]:
    """Return ``(text, None)`` when indexable, otherwise ``(None, skip_reason)``."""
    if b"\0" in raw:
        return None, "binary"
    text = normalize_source(raw)
    if text is None:
        return None, "not_utf8"
    lines = text.split("\n")
    if max((len(line) for line in lines), default=0) > MAX_LINE_LENGTH:
        return None, "minified"
    if len(text) / max(len(lines), 1) > MAX_AVERAGE_LINE_LENGTH:
        return None, "minified"
    if find_secret_rules(text):
        return None, "contains_secret"
    return text, None


_TEST_DIRECTORIES = frozenset({"test", "tests", "__tests__", "__mocks__", "e2e", "spec", "specs"})
_TEST_FILE = re.compile(r"(\.(spec|test|int-spec|e2e-spec)\.[cm]?[jt]sx?$)|((Test|Tests|IT|ITCase)\.java$)")


def is_test_path(path: str) -> bool:
    pure = PurePosixPath(path)
    if any(part.lower() in _TEST_DIRECTORIES for part in pure.parts[:-1]):
        return True
    return bool(_TEST_FILE.search(pure.name))
