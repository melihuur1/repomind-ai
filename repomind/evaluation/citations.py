"""Verifies that a citation points at real code at the indexed commit."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from repomind.ingest.filters import normalize_source
from repomind.ingest.git_source import GitRepository


@dataclass(frozen=True)
class CitationCheck:
    valid: bool
    reason: str | None = None


class CitationVerifier:
    """Compares a cited line range with the blob stored in Git, not with our own database."""

    def __init__(self, git: GitRepository, commit: str) -> None:
        self.git = git
        self.commit = commit
        self._read = lru_cache(maxsize=None)(self._read_lines)

    def _read_lines(self, path: str) -> tuple[str, ...] | None:
        raw = self.git.read_file(self.commit, path)
        if raw is None:
            return None
        text = normalize_source(raw)
        return None if text is None else tuple(text.split("\n"))

    def check(self, path: str, start_line: int, end_line: int, expected_content: str | None = None) -> CitationCheck:
        lines = self._read(path)
        if lines is None:
            return CitationCheck(False, "file does not exist at this commit")
        if start_line < 1 or end_line < start_line or end_line > len(lines):
            return CitationCheck(False, f"lines {start_line}-{end_line} are outside the file (1-{len(lines)})")
        if expected_content is not None and "\n".join(lines[start_line - 1 : end_line]) != expected_content:
            return CitationCheck(False, "content differs from the file at this commit")
        return CitationCheck(True)
