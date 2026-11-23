"""An indexed file as it may be shown: read from Git at the indexed commit, with every credential line blank.

The web API's file view and the MCP server read a file only through this, so that what may be shown of
a file is decided in one place. The path must already be one the index lists: nothing here checks that.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

from repomind.ingest.filters import normalize_source
from repomind.ingest.git_source import GitRepository
from repomind.parsing.credentials import hidden_lines

_OBJECT_ID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


@dataclass(frozen=True)
class ShownFile:
    lines: list[str] = field(repr=False)
    """Every line of the file, without the empty one after a final newline. A hidden line is empty."""
    hidden_lines: list[int]
    """1-indexed. Found over the whole file, as indexing finds them, and widened to each statement."""
    _object_ids: tuple[str, str] = field(repr=False)

    def matches_blob(self, blob_sha: str) -> bool | None:
        """Whether Git's bytes are the Git object the index recorded. None when the record is no object id."""
        recorded = (blob_sha or "").strip().lower()
        if not _OBJECT_ID.fullmatch(recorded):
            return None
        return recorded in self._object_ids


def read_indexed_file(git: GitRepository, commit: str, path: str, language: str | None) -> ShownFile | None:
    """The file at ``commit``, or None when Git holds no text file at that path."""
    raw = git.read_file(commit, path)
    text = normalize_source(raw) if raw is not None else None
    if text is None:
        return None
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    hidden = hidden_lines(language, text)
    for number in hidden:
        lines[number - 1] = ""
    # The object id Git gives these bytes, in either of its hash formats, to compare with the index's record.
    header = b"blob %d\0" % len(raw)
    object_ids = (hashlib.sha1(header + raw).hexdigest(), hashlib.sha256(header + raw).hexdigest())
    return ShownFile(lines, hidden, object_ids)
