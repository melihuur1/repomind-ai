import { useQuery } from "@tanstack/react-query";
import { ArrowRight, Database, FileCode2, GitCommitHorizontal, ShieldCheck } from "lucide-react";
import { useEffect } from "react";
import { Link } from "react-router-dom";
import { api, type Repository } from "../api/client";
import { EmptyNote, ErrorNote, Spinner, useHealth } from "../components/common";
import { TopBar } from "../components/TopBar";
import { repoUrl, shortCommit } from "../lib/links";
import { LanguageBar } from "../workspace/RepositoryOverview";

function RepositoryCard({ repository }: { repository: Repository }) {
  return (
    <Link to={repoUrl(repository.name)} className="repo-card" data-testid="repo-card">
      <div className="repo-card-head">
        <h3>{repository.name}</h3>
        <ArrowRight size={16} aria-hidden />
      </div>
      <p className="repo-card-subject">{repository.subject}</p>
      <LanguageBar languages={repository.languages} />
      <dl className="repo-card-stats">
        <div>
          <dt>
            <GitCommitHorizontal size={13} aria-hidden /> Commit
          </dt>
          <dd>
            <code>{shortCommit(repository.commit)}</code>
          </dd>
        </div>
        <div>
          <dt>
            <FileCode2 size={13} aria-hidden /> Files
          </dt>
          <dd>{repository.files_indexed}</dd>
        </div>
        <div>
          <dt>
            <Database size={13} aria-hidden /> Chunks
          </dt>
          <dd>{repository.chunks}</dd>
        </div>
      </dl>
    </Link>
  );
}

export function HomePage() {
  useEffect(() => {
    document.title = "RepoMind AI";
  }, []);
  const health = useHealth();
  const { data, isLoading, error } = useQuery({ queryKey: ["repositories"], queryFn: api.repositories });
  return (
    <div className="home">
      <TopBar />
      <main className="home-main">
        <section className="hero">
          <h1>Read a codebase by what it does.</h1>
          <p>
            Search any indexed repository by meaning or by exact name, and open every result at its real lines. Each source is checked against the Git commit it came from.
          </p>
          <ul className="hero-points">
            <li>
              <ShieldCheck size={15} aria-hidden /> Code is read from Git and never executed
            </li>
            <li>
              <ShieldCheck size={15} aria-hidden /> Secrets and credentials stay out of the index
            </li>
            <li>
              <ShieldCheck size={15} aria-hidden /> Runs on your machine, with no paid API
            </li>
          </ul>
        </section>

        <section aria-labelledby="repositories-heading">
          <h2 id="repositories-heading">Repositories</h2>
          {health.data && !health.data.database ? <ErrorNote error={new Error()}>The database is not running. Start it with: docker compose up -d db</ErrorNote> : null}
          {isLoading ? <Spinner /> : null}
          {error ? <ErrorNote error={error} /> : null}
          {data && data.length === 0 ? <EmptyNote title="Nothing is indexed yet." /> : null}
          <div className="repo-grid">{data?.map((repository) => <RepositoryCard key={repository.name} repository={repository} />)}</div>
        </section>
      </main>
    </div>
  );
}
