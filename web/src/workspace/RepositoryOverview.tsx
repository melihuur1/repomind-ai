import { FileCode2, GitCommitHorizontal, MousePointerClick } from "lucide-react";
import type { Repository } from "../api/client";
import { shortCommit } from "../lib/links";

const LANGUAGE_NAMES: Record<string, string> = {
  java: "Java",
  typescript: "TypeScript",
  tsx: "TSX",
  javascript: "JavaScript",
  sql: "SQL",
  yaml: "YAML",
};

const SKIP_NAMES: Record<string, string> = {
  hidden: "hidden",
  excluded_directory: "dependencies and build output",
  sensitive_name: "sensitive by name",
  unsupported_language: "other file types",
  generated: "generated",
  data_dump: "data dumps",
  too_large: "too large",
  binary: "binary",
  not_utf8: "not text",
  minified: "minified",
  contains_secret: "holding a credential",
};

export function LanguageBar({ languages }: { languages: Record<string, number> }) {
  const entries = Object.entries(languages).sort((a, b) => b[1] - a[1]);
  const total = entries.reduce((sum, [, count]) => sum + count, 0) || 1;
  return (
    <div className="language-bar">
      <div className="language-track" aria-hidden>
        {entries.map(([language, count]) => (
          <span key={language} className={`language-${language}`} style={{ width: `${(count / total) * 100}%` }} />
        ))}
      </div>
      <ul className="language-legend">
        {entries.map(([language, count]) => (
          <li key={language}>
            <span className={`language-dot language-${language}`} aria-hidden />
            {LANGUAGE_NAMES[language] ?? language} <span className="muted">{count}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

export function RepositoryOverview({ repository }: { repository: Repository }) {
  const skipped = Object.entries(repository.skipped).filter(([, count]) => count > 0);
  return (
    <div className="overview">
      <h1>{repository.name}</h1>
      <p className="overview-commit">
        <GitCommitHorizontal size={15} aria-hidden />
        <code>{shortCommit(repository.commit)}</code>
        <span>{repository.subject}</span>
        <span className="muted">{new Date(repository.committed_at).toLocaleDateString("en-GB", { dateStyle: "medium" })}</span>
      </p>
      <div className="stat-grid">
        <div className="stat">
          <span className="stat-value">{repository.files_indexed}</span>
          <span className="stat-label">files indexed, of {repository.files_in_commit}</span>
        </div>
        <div className="stat">
          <span className="stat-value">{repository.chunks.toLocaleString("en-GB")}</span>
          <span className="stat-label">code chunks</span>
        </div>
        <div className="stat">
          <span className="stat-value">{repository.symbols.toLocaleString("en-GB")}</span>
          <span className="stat-label">symbols</span>
        </div>
        <div className="stat">
          <span className="stat-value">{repository.embedded_chunks.toLocaleString("en-GB")}</span>
          <span className="stat-label">chunks with embeddings</span>
        </div>
      </div>
      <h2>Languages</h2>
      <LanguageBar languages={repository.languages} />
      {skipped.length ? (
        <>
          <h2>Not indexed</h2>
          <p className="muted overview-skipped">
            {skipped.map(([reason, count]) => `${count} ${SKIP_NAMES[reason] ?? reason}`).join(" · ")}
          </p>
        </>
      ) : null}
      <p className="overview-hint">
        <MousePointerClick size={15} aria-hidden /> Pick a file on the left, or search the code.
        <FileCode2 size={15} aria-hidden /> Line numbers link to this exact commit.
      </p>
    </div>
  );
}
