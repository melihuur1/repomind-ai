import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, CheckCircle2, GitBranchPlus, Loader2, RotateCcw } from "lucide-react";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { api, ApiError, type IndexJob } from "../api/client";
import { repoUrl } from "../lib/links";
import { ErrorNote } from "./common";

const PHASES: Record<string, string> = {
  queued: "Waiting to start",
  checking: "Checking the repository on GitHub",
  cloning: "Cloning, with nothing from it being run",
  indexing: "Parsing the code",
  embedding: "Creating embeddings",
};

function JobRow({ job }: { job: IndexJob }) {
  const client = useQueryClient();
  const retry = useMutation({
    mutationFn: () => api.retryJob(job.id),
    onSuccess: () => client.invalidateQueries({ queryKey: ["jobs"] }),
  });
  const active = job.status === "queued" || job.status === "running";
  const percent = job.total > 0 ? Math.round((job.done / job.total) * 100) : null;
  return (
    <li className={`job job-${job.status}`} data-testid="job" data-status={job.status} data-error={job.error_code ?? undefined}>
      <div className="job-head">
        {active ? <Loader2 size={14} className="spin" aria-hidden /> : null}
        {job.status === "done" ? <CheckCircle2 size={14} aria-hidden /> : null}
        {job.status === "failed" ? <AlertTriangle size={14} aria-hidden /> : null}
        <strong>{job.repo}</strong>
        <span className="job-actions">
          {job.status === "done" ? (
            <Link className="btn btn-ghost" to={repoUrl(job.repo)} data-testid="job-open">
              Open
            </Link>
          ) : null}
          {job.status === "failed" ? (
            <button type="button" className="btn btn-ghost" onClick={() => retry.mutate()} disabled={retry.isPending} data-testid="job-retry">
              <RotateCcw size={13} aria-hidden /> Retry
            </button>
          ) : null}
        </span>
      </div>
      {active ? (
        <>
          <p className="job-message muted">
            {PHASES[job.phase] ?? job.phase}
            {percent !== null ? ` · ${job.done.toLocaleString("en-GB")} of ${job.total.toLocaleString("en-GB")}` : ""}
          </p>
          <div className="progress" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={percent ?? undefined}>
            <span className={percent === null ? "progress-unknown" : ""} style={percent === null ? undefined : { width: `${percent}%` }} />
          </div>
        </>
      ) : (
        <p className="job-message" data-testid="job-message">
          {job.message}
        </p>
      )}
      {retry.error ? <ErrorNote error={retry.error} /> : null}
    </li>
  );
}

export function AddRepository() {
  const client = useQueryClient();
  const [url, setUrl] = useState("");
  const finished = useRef(new Set<number>());

  const jobs = useQuery({
    queryKey: ["jobs"],
    queryFn: api.jobs,
    refetchInterval: (query) => ((query.state.data ?? []).some((job) => job.status === "queued" || job.status === "running") ? 1000 : false),
  });

  const add = useMutation({
    mutationFn: (value: string) => api.addRepository(value),
    onSuccess: () => {
      setUrl("");
      void client.invalidateQueries({ queryKey: ["jobs"] });
    },
  });

  // When a job finishes, the list of repositories has changed.
  useEffect(() => {
    let changed = false;
    for (const job of jobs.data ?? []) {
      if (job.status === "done" && !finished.current.has(job.id)) {
        finished.current.add(job.id);
        changed = true;
      }
    }
    if (changed) void client.invalidateQueries({ queryKey: ["repositories"] });
  }, [jobs.data, client]);

  const submit = (event: FormEvent) => {
    event.preventDefault();
    const value = url.trim();
    if (value) add.mutate(value);
  };

  const recent = (jobs.data ?? []).slice(0, 5);
  return (
    <section className="add-repo" aria-labelledby="add-heading">
      <h2 id="add-heading">Analyze a public GitHub repository</h2>
      <form className="add-form" onSubmit={submit}>
        <div className="add-input">
          <GitBranchPlus size={15} aria-hidden />
          <input
            value={url}
            onChange={(event) => setUrl(event.target.value)}
            placeholder="https://github.com/owner/repository"
            aria-label="GitHub repository address"
            maxLength={200}
            spellCheck={false}
            data-testid="repo-url"
          />
        </div>
        <button type="submit" className="btn btn-primary btn-tall" disabled={add.isPending || !url.trim()} data-testid="repo-add">
          Analyze
        </button>
      </form>
      <p className="muted add-hint">Public repositories only. Its code is read from Git and never run, and secrets and build output stay out of the index.</p>
      {add.error ? (
        <div data-testid="repo-url-error">
          <ErrorNote error={add.error instanceof ApiError ? add.error : new ApiError(0, "unknown", "This could not be added.")} />
        </div>
      ) : null}
      {recent.length ? (
        <ul className="job-list">
          {recent.map((job) => (
            <JobRow key={job.id} job={job} />
          ))}
        </ul>
      ) : null}
    </section>
  );
}
