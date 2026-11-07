import { useQuery } from "@tanstack/react-query";
import { GitCommitHorizontal } from "lucide-react";
import { Link, useNavigate } from "react-router-dom";
import { api, type Repository } from "../api/client";
import { repoUrl, shortCommit } from "../lib/links";
import { AiStatusPill, CopyButton, ThemeToggle } from "./common";

export function Brand() {
  return (
    <Link to="/" className="brand" aria-label="RepoMind AI home">
      <img src="/favicon.svg" alt="" width={22} height={22} />
      <span>
        RepoMind <em>AI</em>
      </span>
    </Link>
  );
}

export function TopBar({ repository }: { repository?: Repository }) {
  const navigate = useNavigate();
  const { data: repositories } = useQuery({ queryKey: ["repositories"], queryFn: api.repositories });
  return (
    <header className="topbar">
      <Brand />
      {repository ? (
        <>
          <span className="topbar-divider" aria-hidden />
          <label className="repo-select">
            <span className="visually-hidden">Repository</span>
            <select
              value={repository.name}
              data-testid="repo-select"
              onChange={(event) => {
                if (event.target.value) navigate(repoUrl(event.target.value));
              }}
            >
              {(repositories ?? [repository]).map((item) => (
                <option key={item.name} value={item.name}>
                  {item.name}
                </option>
              ))}
            </select>
          </label>
          <span className="commit-chip" title={`${repository.commit}\n${repository.subject}`} data-testid="commit-chip">
            <GitCommitHorizontal size={14} aria-hidden />
            <code>{shortCommit(repository.commit)}</code>
            <span className="commit-subject">{repository.subject}</span>
            <CopyButton text={repository.commit} title="Copy the full commit hash" />
          </span>
        </>
      ) : null}
      <span className="topbar-spacer" />
      <AiStatusPill />
      <ThemeToggle />
    </header>
  );
}
