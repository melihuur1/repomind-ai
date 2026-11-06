"""Read-only access to a repository's committed content.

Everything is read from Git's object database with plumbing commands, so the
working tree is never consulted. That gives four guarantees at once: the content
matches the commit SHA exactly, uncommitted files such as ``.env`` are out of
reach, nothing in the analyzed repository is executed, and nothing is written to
it (``GIT_OPTIONAL_LOCKS=0`` also stops Git from refreshing the index).
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

from repomind.errors import RepoMindError

_REGULAR_FILE_MODES = {"100644", "100755"}


@dataclass(frozen=True)
class BlobEntry:
    path: str
    sha: str
    size: int


@dataclass(frozen=True)
class CommitInfo:
    sha: str
    committed_at: datetime
    subject: str


def _git_env() -> dict[str, str]:
    env = dict(os.environ)
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        env.pop(name, None)
    env.update(GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0", GIT_NO_REPLACE_OBJECTS="1")
    return env


class GitRepository:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        if not self.path.is_dir():
            raise RepoMindError(f"Path does not exist or is not a directory: {self.path}")
        try:
            top_level = self._run("rev-parse", "--show-toplevel").decode().strip()
        except RepoMindError as exc:
            raise RepoMindError(
                f"Not a readable Git repository: {self.path}",
                hint=(
                    "RepoMind indexes commits so that every citation can be verified. "
                    f"Git said: {exc}"
                ),
            ) from exc
        self.path = Path(top_level)

    def _command(self, *args: str) -> list[str]:
        return ["git", "-c", "core.fsmonitor=false", "-c", "core.quotepath=false", "-C", str(self.path), *args]

    def _run(self, *args: str) -> bytes:
        try:
            result = subprocess.run(self._command(*args), capture_output=True, env=_git_env(), check=False)
        except FileNotFoundError as exc:
            raise RepoMindError("Git executable not found.", hint="Install Git and add it to PATH.") from exc
        if result.returncode != 0:
            raise RepoMindError(result.stderr.decode(errors="replace").strip() or f"git {args[0]} failed")
        return result.stdout

    def resolve_commit(self, ref: str = "HEAD") -> str:
        try:
            return self._run("rev-parse", "--verify", f"{ref}^{{commit}}").decode().strip()
        except RepoMindError as exc:
            raise RepoMindError(
                f"Cannot resolve commit '{ref}' in {self.path}",
                hint="The repository may have no commits yet, or the revision does not exist locally.",
            ) from exc

    def commit_info(self, sha: str) -> CommitInfo:
        raw = self._run("cat-file", "commit", sha).decode("utf-8", errors="replace")
        headers, _, message = raw.partition("\n\n")
        committed_at = datetime.fromtimestamp(0, tz=timezone.utc)
        for line in headers.splitlines():
            if line.startswith("committer "):
                committed_at = datetime.fromtimestamp(int(line.rsplit(" ", 2)[1]), tz=timezone.utc)
        return CommitInfo(sha=sha, committed_at=committed_at, subject=message.split("\n", 1)[0].strip())

    def default_name(self) -> str:
        """Repository name from the origin URL, falling back to the folder name."""
        try:
            url = self._run("config", "--get", "remote.origin.url").decode().strip()
        except RepoMindError:
            url = ""
        tail = url.rstrip("/").replace("\\", "/").rsplit("/", 1)[-1].rsplit(":", 1)[-1]
        name = tail.removesuffix(".git") or self.path.name
        return name.lower()

    def list_files(self, commit: str) -> list[BlobEntry]:
        """Regular files at ``commit``. Symlinks and submodules are never followed."""
        output = self._run("ls-tree", "-r", "-l", "-z", commit)
        entries: list[BlobEntry] = []
        for record in output.split(b"\0"):
            if not record:
                continue
            meta, _, raw_path = record.partition(b"\t")
            mode, kind, sha, size = meta.split()
            if kind == b"blob" and mode.decode() in _REGULAR_FILE_MODES:
                entries.append(BlobEntry(raw_path.decode("utf-8", "surrogateescape"), sha.decode(), int(size)))
        return entries

    def read_blobs(self, entries: Iterable[BlobEntry]) -> Iterator[tuple[BlobEntry, bytes]]:
        process = subprocess.Popen(
            self._command("cat-file", "--batch"),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=_git_env(),
        )
        assert process.stdin is not None and process.stdout is not None
        try:
            for entry in entries:
                process.stdin.write(entry.sha.encode() + b"\n")
                process.stdin.flush()
                header = process.stdout.readline().split()
                if len(header) != 3:
                    raise RepoMindError(f"Git could not read blob {entry.sha} ({entry.path})")
                content = process.stdout.read(int(header[2]))
                process.stdout.read(1)  # the newline that terminates each record
                yield entry, content
        finally:
            process.stdin.close()
            process.stdout.close()
            process.wait()

    def read_file(self, commit: str, path: str) -> bytes | None:
        """File content at ``commit``, or None if the path does not exist there."""
        try:
            return self._run("cat-file", "blob", f"{commit}:{path}")
        except RepoMindError:
            return None
