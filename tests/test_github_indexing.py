"""Adding a repository by URL: hostile addresses, what a clone may do, limits, and the job's life."""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from repomind.api.app import create_app
from repomind.api.jobs import INTERRUPTED, IndexingWorker
from repomind.ingest.git_source import GitRepository
from repomind.ingest.github import (
    GitHubRepo,
    IndexFailure,
    IndexLimits,
    RepoFacts,
    _SameHostRedirects,
    clone_command,
    facts_from_api,
    git_environment,
    parse_github_url,
    remove_tree,
    run_clone,
)
from tests.conftest import commit_all, git, write
from tests.test_api import _services

URL = "https://github.com/octo/shop"


class TestAddresses:
    @pytest.mark.parametrize(
        ("url", "owner", "name"),
        [
            ("https://github.com/octo/shop", "octo", "shop"),
            ("https://github.com/octo/shop/", "octo", "shop"),
            ("https://github.com/octo/shop.git", "octo", "shop"),
            ("github.com/octo/shop", "octo", "shop"),
            ("  https://github.com/octo/my.repo-1_x  ", "octo", "my.repo-1_x"),
        ],
    )
    def test_a_repository_address_is_accepted(self, url, owner, name):
        repo = parse_github_url(url)
        assert (repo.owner, repo.name) == (owner, name)
        assert repo.clone_url == f"https://github.com/{owner}/{name}.git"

    @pytest.mark.parametrize(
        "url",
        [
            "",
            "http://github.com/octo/shop",
            "https://github.com/octo",
            "https://github.com/octo/shop/tree/main",
            "https://github.com/octo/shop?tab=readme",
            "https://github.com/octo/shop#readme",
            "https://github.com:443/octo/shop",
            "https://user:pass@github.com/octo/shop",
            "https://github.com@evil.example/octo/shop",
            "https://github.com.evil.example/octo/shop",
            "https://evil.example/github.com/octo/shop",
            "https://raw.githubusercontent.com/octo/shop",
            "https://GITHUB.com/octo/shop",
            "https://127.0.0.1/octo/shop",
            "https://169.254.169.254/latest/meta-data",
            "http://localhost:55432/octo/shop",
            "file:///C:/Windows/System32",
            "file://github.com/octo/shop",
            "ssh://git@github.com/octo/shop",
            "git@github.com:octo/shop.git",
            "git://github.com/octo/shop",
            "ext::sh -c touch%20/tmp/pwned",
            "https://github.com/../../etc/passwd",
            "https://github.com/octo/..",
            "https://github.com/octo/.",
            "https://github.com/octo/%2e%2e",
            "https://github.com/-octo/shop",
            "https://github.com/octo/-shop",
            "https://github.com/octo/--upload-pack=touch",
            "https://github.com/octo/shop\nhttps://evil.example/x/y",
            "https://github.com/octo/shop evil",
            "https://github.com/octo/.git",
            "https://gіthub.com/octo/shop",
            "https://github.com/" + "a" * 300 + "/shop",
        ],
    )
    def test_anything_else_is_refused_before_any_request(self, url):
        with pytest.raises(IndexFailure) as caught:
            parse_github_url(url)
        assert caught.value.code == "invalid_url"


class TestWhatGitHubSays:
    LIMITS = IndexLimits()
    ASKED = GitHubRepo("octo", "shop")

    def test_a_private_repository_is_refused(self):
        with pytest.raises(IndexFailure) as caught:
            facts_from_api({"private": True, "default_branch": "main", "size": 1}, self.ASKED, self.LIMITS)
        assert caught.value.code == "not_public"

    def test_a_missing_private_field_is_not_taken_as_public(self):
        with pytest.raises(IndexFailure):
            facts_from_api({"default_branch": "main", "size": 1}, self.ASKED, self.LIMITS)

    def test_too_large_a_repository_is_refused_before_cloning(self):
        with pytest.raises(IndexFailure) as caught:
            facts_from_api({"private": False, "default_branch": "main", "size": 400 * 1024}, self.ASKED, self.LIMITS)
        assert caught.value.code == "too_large"

    def test_a_renamed_repository_uses_its_real_name(self):
        facts = facts_from_api({"private": False, "default_branch": "main", "size": 1, "full_name": "new-owner/new-name"}, self.ASKED, self.LIMITS)
        assert facts.repo.full_name == "new-owner/new-name"

    @pytest.mark.parametrize("branch", ["", "--upload-pack=x", "-x", "a..b", "a b"])
    def test_a_strange_default_branch_is_refused(self, branch):
        with pytest.raises(IndexFailure):
            facts_from_api({"private": False, "default_branch": branch, "size": 1}, self.ASKED, self.LIMITS)

    @pytest.mark.parametrize("target", ["https://evil.example/x", "http://api.github.com/x", "https://169.254.169.254/x", "file:///etc/passwd"])
    def test_a_redirect_cannot_leave_the_github_api(self, target):
        with pytest.raises(IndexFailure):
            _SameHostRedirects().redirect_request(None, None, 301, "Moved", {}, target)


def _local_url(path: Path) -> str:
    return path.resolve().as_uri()


def _file_env() -> dict[str, str]:
    return {**git_environment(), "GIT_ALLOW_PROTOCOL": "file"}


class TestCloning:
    def test_only_https_is_allowed_so_a_local_path_cannot_be_cloned(self, sample_repo, tmp_path):
        command = clone_command(_local_url(sample_repo), "main", tmp_path / "clone")
        with pytest.raises(IndexFailure) as caught:
            run_clone(command, tmp_path / "clone", IndexLimits())
        assert caught.value.code == "clone_failed"

    def test_the_command_runs_nothing_from_the_repository(self):
        command = clone_command("https://github.com/o/r.git", "main", Path("x"))
        for flag in ("--no-checkout", "--depth=1", "--single-branch", "--no-tags", "--no-recurse-submodules", "--template="):
            assert flag in command
        joined = " ".join(command)
        assert "protocol.allow=never" in joined and "protocol.https.allow=always" in joined
        assert "core.hooksPath=" in joined and "http.followRedirects=false" in joined
        assert command.index("--") < command.index("https://github.com/o/r.git")

    def test_stored_credentials_and_local_git_settings_take_no_part(self):
        env = git_environment()
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        assert env["GIT_CONFIG_NOSYSTEM"] == "1"
        assert env["GIT_ALLOW_PROTOCOL"] == "https"

    def test_a_hostile_repository_leaves_no_files_hooks_or_submodules(self, sample_repo, tmp_path):
        marker = tmp_path / "ran.txt"
        write(sample_repo, ".gitmodules", '[submodule "evil"]\n\tpath = evil\n\turl = https://evil.example/x.git\n')
        write(sample_repo, ".gitattributes", "* filter=evil\n")
        hook = sample_repo / ".git" / "hooks" / "post-checkout"
        hook.write_text(f"#!/bin/sh\ntouch '{marker.as_posix()}'\n")
        git(sample_repo, "config", "filter.evil.smudge", f"sh -c 'touch {marker.as_posix()}'")
        commit_all(sample_repo, "hostile")
        target = tmp_path / "clone"
        run_clone(clone_command(_local_url(sample_repo), "main", target, allowed_protocol="file"), target, IndexLimits(), env=_file_env())
        assert [item.name for item in target.iterdir()] == [".git"], "no working files"
        assert not marker.exists(), "nothing ran"
        assert not (target / ".git" / "modules").exists()
        hooks = target / ".git" / "hooks"
        assert not hooks.exists() or not any(hooks.iterdir())
        assert GitRepository(target).resolve_commit("HEAD") == git(sample_repo, "rev-parse", "HEAD")

    def test_symlinks_and_submodules_in_a_commit_are_never_read(self, sample_repo, tmp_path):
        blob = subprocess.run(["git", "-C", str(sample_repo), "hash-object", "-w", "--stdin"], input=b"/etc/passwd", capture_output=True, check=True).stdout.decode().strip()
        git(sample_repo, "update-index", "--add", "--cacheinfo", f"120000,{blob},src/link.ts")
        git(sample_repo, "update-index", "--add", "--cacheinfo", f"160000,{git(sample_repo, 'rev-parse', 'HEAD')},vendored")
        git(sample_repo, "commit", "-m", "links")
        paths = {entry.path for entry in GitRepository(sample_repo).list_files("HEAD")}
        assert "src/link.ts" not in paths
        assert "vendored" not in paths

    def test_a_slow_clone_is_stopped(self, tmp_path):
        slow = [sys.executable, "-c", "import time; time.sleep(30)"]
        started = time.monotonic()
        with pytest.raises(IndexFailure) as caught:
            run_clone(slow, tmp_path / "x", IndexLimits(clone_seconds=1))
        assert caught.value.code == "timeout"
        assert time.monotonic() - started < 10

    def test_a_clone_that_grows_too_large_is_stopped(self, tmp_path):
        target = tmp_path / "grow"
        script = f"import os,time; os.makedirs(r'{target}'); open(r'{target}/big','wb').write(b'x'*(3*1024*1024)); time.sleep(30)"
        with pytest.raises(IndexFailure) as caught:
            run_clone([sys.executable, "-c", script], target, IndexLimits(max_clone_mb=1, clone_seconds=20))
        assert caught.value.code == "too_large"

    def test_read_only_git_objects_can_be_removed(self, sample_repo, tmp_path):
        target = tmp_path / "clone"
        run_clone(clone_command(_local_url(sample_repo), "main", target, allowed_protocol="file"), target, IndexLimits(), env=_file_env())
        remove_tree(target)
        assert not target.exists()


class Source:
    """GitHub stood in for by a local repository, cloned with the real command."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.clones = 0
        self.lookup_error: IndexFailure | None = None
        self.clone_error: IndexFailure | None = None
        self.hold: threading.Event | None = None
        self.holding = threading.Event()

    def lookup(self, repo, limits):
        if self.lookup_error:
            raise self.lookup_error
        return RepoFacts(repo, "main", 1)

    def head_commit(self, facts):
        return git(self.path, "rev-parse", "HEAD")

    def clone(self, facts, target, limits):
        self.clones += 1
        if self.hold:
            self.holding.set()
            self.hold.wait(20)
        if self.clone_error:
            raise self.clone_error
        run_clone(clone_command(_local_url(self.path), "main", target, allowed_protocol="file"), target, limits, env=_file_env())


@pytest.fixture
def source(sample_repo) -> Source:
    write(sample_repo, ".env", "TOKEN=do-not-index\n")
    write(sample_repo, "config/secrets.yml", "password: hunter2\n")
    write(sample_repo, "src/leak.ts", 'export const key = "AKIAIOSFODNN7EXAMPLE";\n')
    commit_all(sample_repo, "secrets")
    return Source(sample_repo)


@pytest.fixture
def app_with_worker(store, source, fake_embedder, tmp_path):
    def build(limits=IndexLimits(), **options):
        services = _services(fake_embedder, **options)
        worker = IndexingWorker(services, source, tmp_path / "data", limits)
        return services, worker, TestClient(create_app(services, worker))

    return build


def _finish(client: TestClient, job_id: int, seconds: float = 30) -> dict:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("done", "failed"):
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish: {job}")


def _add(client: TestClient, url: str = URL) -> dict:
    response = client.post("/api/repositories", json={"url": url})
    assert response.status_code == 202, response.text
    return response.json()


class TestJobs:
    def test_a_repository_becomes_browsable_with_its_secrets_left_out(self, app_with_worker, source):
        _, _, app = app_with_worker()
        with app as client:
            job = _finish(client, _add(client)["id"])
            assert job["status"] == "done"
            assert job["outcome"] == "indexed"
            assert job["commit_sha"] == git(source.path, "rev-parse", "HEAD")
            repositories = {item["name"]: item for item in client.get("/api/repositories").json()}
            assert repositories["octo/shop"]["origin_url"] == URL
            files = {entry["path"] for entry in client.get("/api/tree", params={"repo": "octo/shop"}).json()["files"]}
            assert "src/payment.ts" in files
            assert {".env", "config/secrets.yml", "src/leak.ts"}.isdisjoint(files)
            shown = client.get("/api/file", params={"repo": "octo/shop", "path": "src/payment.ts"}).json()
            assert shown["origin_url"] == URL
            assert client.get("/api/search", params={"repo": "octo/shop", "q": "refund"}).json()["results"]

    def test_the_same_commit_is_not_cloned_or_indexed_again(self, app_with_worker, source, fake_embedder):
        _, _, app = app_with_worker()
        with app as client:
            _finish(client, _add(client)["id"])
            embedded = len(fake_embedder.embedded)
            second = _finish(client, _add(client)["id"])
        assert second["outcome"] == "unchanged"
        assert source.clones == 1
        assert len(fake_embedder.embedded) == embedded

    def test_a_new_commit_is_indexed_and_the_old_clone_goes(self, app_with_worker, source, tmp_path):
        _, _, app = app_with_worker()
        with app as client:
            first = _finish(client, _add(client)["id"])
            write(source.path, "src/added.ts", "export const added = 1;\n")
            head = commit_all(source.path, "more")
            second = _finish(client, _add(client)["id"])
            files = {entry["path"] for entry in client.get("/api/tree", params={"repo": "octo/shop"}).json()["files"]}
        assert second["outcome"] == "indexed"
        assert second["commit_sha"] == head != first["commit_sha"]
        assert "src/added.ts" in files
        clones = [child.name for child in (tmp_path / "data" / "repos" / "octo__shop").iterdir()]
        assert clones == [head]

    def test_a_failure_is_explained_and_a_retry_can_succeed(self, app_with_worker, source):
        source.clone_error = IndexFailure("clone_failed", "Git could not clone this repository.")
        _, _, app = app_with_worker()
        with app as client:
            failed = _finish(client, _add(client)["id"])
            assert failed["status"] == "failed"
            assert failed["error_code"] == "clone_failed"
            assert "could not clone" in failed["message"]
            assert client.get("/api/repositories").json() == []
            source.clone_error = None
            retried = client.post(f"/api/jobs/{failed['id']}/retry", json={})
            assert retried.status_code == 202
            assert _finish(client, retried.json()["id"])["status"] == "done"

    def test_only_a_failed_job_can_be_retried(self, app_with_worker):
        _, _, app = app_with_worker()
        with app as client:
            done = _finish(client, _add(client)["id"])
            assert client.post(f"/api/jobs/{done['id']}/retry", json={}).status_code == 409
            assert client.post("/api/jobs/999/retry", json={}).status_code == 404

    @pytest.mark.parametrize(
        ("code", "message"),
        [("not_found", "This repository does not exist, or it is not public."), ("rate_limited", "GitHub is limiting requests from this machine.")],
    )
    def test_what_github_refuses_reaches_the_user(self, app_with_worker, source, code, message):
        source.lookup_error = IndexFailure(code, message)
        _, _, app = app_with_worker()
        with app as client:
            job = _finish(client, _add(client)["id"])
        assert (job["status"], job["error_code"], job["message"]) == ("failed", code, message)
        assert source.clones == 0

    def test_a_bad_address_is_refused_at_once_with_no_job(self, app_with_worker):
        _, _, app = app_with_worker()
        with app as client:
            response = client.post("/api/repositories", json={"url": "file:///etc/passwd"})
            assert response.status_code == 422
            assert response.json()["code"] == "invalid_url"
            assert client.get("/api/jobs").json() == []

    def test_a_repository_with_nothing_to_read_says_so(self, app_with_worker, store, tmp_path, source):
        for path in source.path.rglob("*"):
            if path.is_file() and ".git" not in path.parts:
                path.unlink()
        write(source.path, "README.md", "# docs only\n")
        commit_all(source.path, "docs only")
        _, _, app = app_with_worker()
        with app as client:
            job = _finish(client, _add(client)["id"])
        assert job["error_code"] == "no_source"
        assert "Java, TypeScript" in job["message"]

    def test_file_limits_are_enforced_after_cloning(self, app_with_worker):
        _, _, app = app_with_worker(limits=IndexLimits(max_files=2))
        with app as client:
            job = _finish(client, _add(client)["id"])
            assert job["error_code"] == "too_many_files"
            assert client.get("/api/repositories").json() == []

    def test_source_size_limit_is_enforced_after_cloning(self, app_with_worker):
        _, _, app = app_with_worker(limits=IndexLimits(max_source_mb=0))
        with app as client:
            assert _finish(client, _add(client)["id"])["error_code"] == "too_large"

    def test_indexing_does_not_start_on_low_memory(self, app_with_worker, source):
        _, _, app = app_with_worker(free_memory=lambda: 1.0)
        with app as client:
            assert _finish(client, _add(client)["id"])["error_code"] == "low_memory"
        assert source.clones == 0

    def test_too_long_an_indexing_stops_and_a_retry_continues(self, app_with_worker, source, fake_embedder):
        _, _, app = app_with_worker(limits=IndexLimits(total_seconds=-1))
        with app as client:
            failed = _finish(client, _add(client)["id"])
        assert failed["error_code"] == "timeout"
        assert "continues" in failed["message"]
        _, _, app = app_with_worker()
        with app as client:
            retried = _finish(client, client.post(f"/api/jobs/{failed['id']}/retry", json={}).json()["id"])
        assert retried["status"] == "done"
        assert source.clones == 1, "the clone from the first attempt was reused"

    def test_the_page_keeps_working_while_a_repository_is_indexing(self, app_with_worker, source):
        source.hold = threading.Event()
        _, _, app = app_with_worker()
        with app as client:
            job = _add(client)
            assert source.holding.wait(10)
            assert client.get("/api/health").status_code == 200
            assert client.get("/api/repositories").status_code == 200
            shown = client.get(f"/api/jobs/{job['id']}").json()
            assert shown["status"] == "running"
            assert shown["phase"] == "cloning"
            again = _add(client)
            assert again["id"] == job["id"], "the same repository is not queued twice"
            source.hold.set()
            assert _finish(client, job["id"])["status"] == "done"

    def test_a_job_a_restart_interrupted_is_marked_and_can_be_retried(self, app_with_worker, store):
        job, _ = store.create_job("octo/shop", URL)
        store.update_job(job["id"], status="running", phase="embedding")
        _, _, app = app_with_worker()
        with app as client:
            shown = client.get(f"/api/jobs/{job['id']}").json()
            assert (shown["status"], shown["error_code"], shown["message"]) == ("failed", "interrupted", INTERRUPTED)
            assert _finish(client, client.post(f"/api/jobs/{job['id']}/retry", json={}).json()["id"])["status"] == "done"

    def test_removing_deletes_the_index_and_the_clone(self, app_with_worker, tmp_path):
        _, _, app = app_with_worker()
        with app as client:
            _finish(client, _add(client)["id"])
            assert (tmp_path / "data" / "repos" / "octo__shop").is_dir()
            assert client.delete("/api/repositories", params={"repo": "octo/shop"}).status_code == 204
            assert client.get("/api/repositories").json() == []
        assert not (tmp_path / "data" / "repos" / "octo__shop").exists()

    def test_a_repository_from_a_local_folder_cannot_be_removed_and_its_files_stay(self, app_with_worker, store, sample_repo, fake_embedder):
        from repomind.indexing import index_repository

        index_repository(store, str(sample_repo), fake_embedder, name="mine")
        _, _, app = app_with_worker()
        with app as client:
            response = client.delete("/api/repositories", params={"repo": "mine"})
            assert response.status_code == 403
            assert response.json()["code"] == "local_repository"
            assert [item["name"] for item in client.get("/api/repositories").json()] == ["mine"]
        assert (sample_repo / "src" / "payment.ts").exists()

    def test_removing_needs_its_own_origin(self, app_with_worker):
        _, _, app = app_with_worker()
        with app as client:
            _finish(client, _add(client)["id"])
            response = client.delete("/api/repositories", params={"repo": "octo/shop"}, headers={"Origin": "https://evil.example", "Host": "127.0.0.1:7321"})
            assert response.status_code == 403
            assert client.get("/api/repositories").json()
