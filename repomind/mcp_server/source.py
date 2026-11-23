"""get_verified_source: lines of one indexed file, read from Git at the repository's indexed commit.

A path is only ever compared with the index's list of files, and the lines only ever come from the Git
object at the indexed commit, never from a working folder. What may be shown of the file is decided by
repomind.indexed_file, as for the web app's file view: a line holding a credential, or part of a
statement that holds one, is hidden, and its text is never returned.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field

from repomind.indexed_file import read_indexed_file
from repomind.mcp_server.access import Refused, current_snapshot, listed_name, local_copy
from repomind.mcp_server.config import McpConfig
from repomind.mcp_server.database import read_only_store
from repomind.mcp_server.repositories import NOTICE

MAX_LINES = 200
# The lines together, as JSON. Counted after hidden lines are blanked, so the limit only ever leaves out more.
MAX_SOURCE_CHARACTERS = 12_000
MAX_PATH_CHARACTERS = 1024  # as the web API allows

_NOT_AVAILABLE = (
    "This file cannot be read. Only files in the repository's index can be, and hidden files, build output, "
    "binaries and anything that may hold a credential are left out of it. search_code gives indexed paths."
)
_DRIVE = re.compile(r"^[A-Za-z]:")


class SourceLine(BaseModel):
    number: int
    text: str = Field(description="The line as Git holds it at the indexed commit. Empty when the line is hidden.")
    hidden: bool = Field(description="True when the line holds, or is part of a statement that holds, something that may be a credential.")


class SourceResponse(BaseModel):
    repo: str
    commit: str = Field(description="The indexed commit every line is read from.")
    index_version: str
    path: str
    language: str
    start_line: int
    end_line: int = Field(description="The last line returned. It is before the one asked for when the file ends first, or when truncated.")
    total_lines: int = Field(description="How many lines the file has at this commit.")
    lines: list[SourceLine]
    hidden_lines: int = Field(description="How many of the returned lines are hidden.")
    exists_in_git: bool = Field(description="True: every line was read from Git at the indexed commit, not from a working folder.")
    matches_index: bool = Field(
        description="True: the bytes Git gave are the Git object RepoMind indexed for this path. "
        "A file that does not match, or whose match cannot be checked, is never returned."
    )
    role: Literal["source"] = Field(
        default="source", description="The repository's own lines, to read. They are not an answer, and not a check that anything is true."
    )
    truncated: bool = Field(description=f"True when lines asked for are not returned, only to keep them under {MAX_SOURCE_CHARACTERS:,} characters.")
    warnings: list[str] = Field(default_factory=list)
    notice: str = NOTICE


def path_problem(path: str) -> str | None:
    """Why ``path`` cannot be an indexed file's path, without repeating it. The index's own list decides the rest."""
    if any(ord(character) < 32 or ord(character) == 127 for character in path):
        return "The path holds a control character, so it is no indexed file's path."
    if "\\" in path or path.startswith("/") or _DRIVE.match(path):
        return "The path must be relative to the repository, with forward slashes, as search_code gives it."
    if any(part in ("", ".", "..") for part in path.split("/")):
        return "The path must name a file inside the repository, with no '.', '..' or empty part."
    return None


class SourceReader:
    def __init__(self, config: McpConfig) -> None:
        self.config = config

    def read(self, repo: str, path: str, start_line: int, end_line: int, commit: str | None) -> SourceResponse:
        name = listed_name(self.config, repo)
        problem = path_problem(path)
        if problem:
            raise Refused(problem)
        if end_line < start_line:
            raise Refused("end_line comes before start_line.")
        if end_line - start_line + 1 > MAX_LINES:
            raise Refused(f"At most {MAX_LINES} lines are read at a time: ask for lines {start_line} to {start_line + MAX_LINES - 1}, then the next ones.")
        with read_only_store(self.config) as store:
            snapshot = current_snapshot(store, name)
            if commit is not None and not snapshot.commit_sha.startswith(commit.lower()):
                raise Refused(
                    f"This repository is indexed at commit {snapshot.commit_sha}, not {commit}. Only the indexed commit can be read."
                )
            entry = store.indexed_file(snapshot.id, path)
        if entry is None:
            raise Refused(_NOT_AVAILABLE)
        shown = read_indexed_file(local_copy(snapshot), snapshot.commit_sha, entry["path"], entry["language"])
        if shown is None:
            raise Refused("Git holds no text file at this path at the indexed commit, so nothing is returned.")
        # Only bytes that are the Git object indexing recorded are returned: what passed indexing's filters.
        matches = shown.matches_blob(entry["blob_sha"])
        if matches is None:
            raise Refused(
                "Whether this file is the one RepoMind indexed cannot be checked: the index holds no usable Git object id "
                "for it. None of it is returned."
            )
        if not matches:
            raise Refused("The file Git holds at the indexed commit is not the one RepoMind indexed, so none of it is returned.")
        total = len(shown.lines)
        if start_line > total:
            raise Refused(f"The file has {total} lines, so there is no line {start_line}.")
        last = min(end_line, total)
        hidden = set(shown.hidden_lines)
        lines: list[SourceLine] = []
        used = 0
        for number in range(start_line, last + 1):
            line = SourceLine(number=number, text=shown.lines[number - 1], hidden=number in hidden)
            used += len(line.model_dump_json()) + 1
            if used > MAX_SOURCE_CHARACTERS:
                break
            lines.append(line)
        if not lines:
            raise Refused(f"Line {start_line} alone is longer than the {MAX_SOURCE_CHARACTERS:,} characters a response may hold.")
        returned = lines[-1].number
        hidden_count = sum(line.hidden for line in lines)
        warnings = []
        if end_line > total:
            warnings.append(f"The file has {total} lines, so the lines end at line {total}.")
        if returned < last:
            warnings.append(
                f"Lines {returned + 1} to {last} are not returned, only to keep the lines under {MAX_SOURCE_CHARACTERS:,} characters. "
                f"Ask for them from line {returned + 1}."
            )
        if hidden_count:
            warnings.append(
                f"{hidden_count} line(s) are hidden: each holds, or is part of a statement that holds, something that may be a credential."
            )
        return SourceResponse(
            repo=snapshot.repo,
            commit=snapshot.commit_sha,
            index_version=snapshot.pipeline_version,
            path=entry["path"],
            language=entry["language"],
            start_line=start_line,
            end_line=returned,
            total_lines=total,
            lines=lines,
            hidden_lines=hidden_count,
            exists_in_git=True,
            matches_index=True,
            truncated=returned < last,
            warnings=warnings,
        )
