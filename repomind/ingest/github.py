"""Fetches public GitHub repositories, treating both the URL and the repository as hostile.

The address a user types is never opened. It is reduced to an owner and a name, and every
request is rebuilt from those, so it cannot reach another host or a local service. A clone
writes only Git's own objects: no working files, no hooks, no submodules and no filters, so
nothing a repository contains gets to run.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from repomind.config import get_setting
from repomind.errors import RepoMindError

_MAX_URL_CHARACTERS = 200
_OWNER = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})"
_NAME = r"[A-Za-z0-9_](?:[A-Za-z0-9._-]{0,98}[A-Za-z0-9_-])?"
_URL = re.compile(rf"(?:https://)?github\.com/({_OWNER})/({_NAME})/?", re.ASCII)
_SHA = re.compile(r"^[0-9a-f]{40}$")
_BRANCH = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._/-]{0,200}$", re.ASCII)
_API_HOST = "api.github.com"
_API_TIMEOUT_SECONDS = 15
_POLL_SECONDS = 0.5


class IndexFailure(RepoMindError):
    """Why a repository could not be indexed, in words a user can act on. ``code`` is stable."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class GitHubRepo:
    owner: str
    name: str

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"

    @property
    def local_name(self) -> str:
        return self.full_name.lower()

    @property
    def web_url(self) -> str:
        return f"https://github.com/{self.full_name}"

    @property
    def clone_url(self) -> str:
        return f"{self.web_url}.git"


def parse_github_url(url: str) -> GitHubRepo:
    text = url.strip()
    found = _URL.fullmatch(text) if len(text) <= _MAX_URL_CHARACTERS else None
    name = found.group(2).removesuffix(".git") if found else ""
    if not name:
        raise IndexFailure(
            "invalid_url",
            "Enter the address of a public GitHub repository, such as https://github.com/owner/name.",
        )
    return GitHubRepo(found.group(1), name)


@dataclass(frozen=True)
class RepoFacts:
    repo: GitHubRepo
    default_branch: str
    size_kb: int


@dataclass(frozen=True)
class IndexLimits:
    max_repo_mb: int = 300
    max_clone_mb: int = 600
    max_files: int = 20_000
    max_indexable_files: int = 5_000
    max_source_mb: int = 100
    clone_seconds: int = 300
    total_seconds: int = 1_800

    @classmethod
    def load(cls) -> "IndexLimits":
        def value(name: str, default: int) -> int:
            raw = get_setting(f"REPOMIND_INDEX_{name}")
            return int(raw) if raw and raw.isdigit() else default

        return cls(
            value("MAX_REPO_MB", cls.max_repo_mb),
            value("MAX_CLONE_MB", cls.max_clone_mb),
            value("MAX_FILES", cls.max_files),
            value("MAX_INDEXABLE_FILES", cls.max_indexable_files),
            value("MAX_SOURCE_MB", cls.max_source_mb),
            value("CLONE_SECONDS", cls.clone_seconds),
            value("TOTAL_SECONDS", cls.total_seconds),
        )


class RepositorySource(Protocol):
    def lookup(self, repo: GitHubRepo, limits: IndexLimits) -> RepoFacts: ...

    def head_commit(self, facts: RepoFacts) -> str: ...

    def clone(self, facts: RepoFacts, target: Path, limits: IndexLimits) -> None: ...


class _SameHostRedirects(urllib.request.HTTPRedirectHandler):
    """GitHub redirects a renamed repository. That is followed only within its own API."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urllib.parse.urlparse(newurl)
        if parsed.scheme != "https" or parsed.hostname != _API_HOST:
            raise IndexFailure("network", "GitHub redirected the request somewhere unexpected, so it was stopped.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _lookup_url(repo: GitHubRepo) -> str:
    return f"https://{_API_HOST}/repos/{repo.owner}/{repo.name}"


def facts_from_api(payload: dict, asked: GitHubRepo, limits: IndexLimits) -> RepoFacts:
    if payload.get("private") is not False:
        raise IndexFailure("not_public", "Only public repositories can be indexed.")
    full_name = str(payload.get("full_name") or "")
    canonical = parse_github_url(f"https://github.com/{full_name}") if full_name else asked
    branch = str(payload.get("default_branch") or "")
    if not _BRANCH.match(branch) or ".." in branch:
        raise IndexFailure("invalid_repository", "This repository has no usable default branch.")
    size_kb = int(payload.get("size") or 0)
    if size_kb > limits.max_repo_mb * 1024:
        raise IndexFailure("too_large", f"This repository is about {size_kb // 1024} MB, and the limit is {limits.max_repo_mb} MB.")
    return RepoFacts(canonical, branch, size_kb)


def git_environment() -> dict[str, str]:
    """Neither this machine's Git settings nor stored credentials take part in a clone."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_TERMINAL_PROMPT="0",
        GCM_INTERACTIVE="never",
        GIT_LFS_SKIP_SMUDGE="1",
        GIT_ALLOW_PROTOCOL="https",
    )
    return env


def _git_options(allowed_protocol: str = "https") -> list[str]:
    options = [
        "-c", "protocol.allow=never",
        "-c", f"protocol.{allowed_protocol}.allow=always",
        "-c", f"core.hooksPath={os.devnull}",
        "-c", "core.symlinks=false",
        "-c", "core.fsmonitor=false",
        "-c", "http.followRedirects=false",
        "-c", "credential.helper=",
        "-c", "submodule.recurse=false",
    ]
    if sys.platform == "win32":
        # Git for Windows keeps its certificate setting in the system file, which is ignored here.
        options += ["-c", "http.sslBackend=schannel"]
    return options


def clone_command(url: str, branch: str, target: Path, allowed_protocol: str = "https") -> list[str]:
    return [
        "git",
        *_git_options(allowed_protocol),
        "clone",
        "--no-checkout",
        "--depth=1",
        "--single-branch",
        "--no-tags",
        "--no-recurse-submodules",
        "--template=",
        f"--branch={branch}",
        "--",
        url,
        str(target),
    ]


def folder_size_mb(path: Path) -> float:
    total = 0
    for folder, _, names in os.walk(path):
        for name in names:
            try:
                total += os.path.getsize(os.path.join(folder, name))
            except OSError:
                continue
    return total / (1024 * 1024)


def run_clone(command: list[str], target: Path, limits: IndexLimits, env: dict[str, str] | None = None) -> None:
    """Watches the clone, and stops it if it takes too long or grows too large."""
    process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, env=env or git_environment())
    deadline = time.monotonic() + limits.clone_seconds
    failure: IndexFailure | None = None
    while process.poll() is None:
        if time.monotonic() > deadline:
            failure = IndexFailure("timeout", f"Cloning took longer than {limits.clone_seconds} seconds, so it was stopped.")
        elif target.exists() and folder_size_mb(target) > limits.max_clone_mb:
            failure = IndexFailure("too_large", f"This repository grew beyond the {limits.max_clone_mb} MB limit while cloning.")
        if failure:
            process.kill()
            process.wait()
            raise failure
        time.sleep(_POLL_SECONDS)
    if process.returncode != 0:
        raise IndexFailure("clone_failed", "Git could not clone this repository. It may be empty, or GitHub may be unreachable.")


class GitHubSource:
    def lookup(self, repo: GitHubRepo, limits: IndexLimits) -> RepoFacts:
        request = urllib.request.Request(
            _lookup_url(repo),
            headers={"Accept": "application/vnd.github+json", "User-Agent": "RepoMind-Indexer", "X-GitHub-Api-Version": "2022-11-28"},
        )
        opener = urllib.request.build_opener(_SameHostRedirects)
        try:
            with opener.open(request, timeout=_API_TIMEOUT_SECONDS) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise IndexFailure("not_found", "This repository does not exist, or it is not public.") from exc
            if exc.code in (403, 429):
                raise IndexFailure("rate_limited", "GitHub is limiting requests from this machine. Try again in a while.") from exc
            raise IndexFailure("network", f"GitHub answered with an error ({exc.code}). Try again in a while.") from exc
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            raise IndexFailure("network", "GitHub cannot be reached from this machine.") from exc
        return facts_from_api(payload, repo, limits)

    def head_commit(self, facts: RepoFacts) -> str:
        try:
            result = subprocess.run(
                ["git", *_git_options(), "ls-remote", "--heads", "--", facts.repo.clone_url, f"refs/heads/{facts.default_branch}"],
                capture_output=True,
                stdin=subprocess.DEVNULL,
                env=git_environment(),
                timeout=60,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise IndexFailure("network", "GitHub did not answer in time.") from exc
        first = result.stdout.decode("ascii", "replace").split()[:1]
        if result.returncode != 0 or not first or not _SHA.match(first[0]):
            raise IndexFailure("clone_failed", "Could not read this repository's latest commit. It may be empty.")
        return first[0]

    def clone(self, facts: RepoFacts, target: Path, limits: IndexLimits) -> None:
        run_clone(clone_command(facts.repo.clone_url, facts.default_branch, target), target, limits)


def remove_tree(path: Path) -> None:
    """Git marks its object files read-only, which stops a plain removal on Windows."""

    def make_writable_and_retry(function, target, _error) -> None:
        os.chmod(target, stat.S_IWRITE)
        function(target)

    if not path.exists():
        return
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=make_writable_and_retry)
    else:
        shutil.rmtree(path, onerror=make_writable_and_retry)
