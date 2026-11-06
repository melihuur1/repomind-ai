from __future__ import annotations

import hashlib
from dataclasses import dataclass, field


@dataclass(frozen=True)
class SymbolInfo:
    kind: str
    name: str
    qualified_name: str
    parent: str | None
    start_line: int
    end_line: int
    signature: str


@dataclass(frozen=True)
class ImportInfo:
    module: str
    names: tuple[str, ...]
    line: int


@dataclass(frozen=True)
class Chunk:
    """A contiguous, 1-indexed, inclusive line range of a file.

    ``content`` is always exactly those lines, joined by ``\\n``, so a citation can
    be verified by comparing it with the file at the indexed commit.
    """

    path: str
    language: str
    kind: str
    start_line: int
    end_line: int
    content: str
    symbol: str | None = None
    parent: str | None = None
    signature: str | None = None
    symbols: tuple[str, ...] = ()
    part_index: int = 1
    part_total: int = 1
    is_test: bool = False

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()


@dataclass
class ParsedFile:
    path: str
    language: str
    line_count: int
    has_syntax_errors: bool
    symbols: list[SymbolInfo] = field(default_factory=list)
    imports: list[ImportInfo] = field(default_factory=list)
    chunks: list[Chunk] = field(default_factory=list)
    withheld_chunks: int = 0  # configuration chunks left out because they hold a credential
