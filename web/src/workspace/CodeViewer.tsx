import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, EyeOff, ExternalLink, GitCommitHorizontal, Link2 } from "lucide-react";
import { Fragment, useEffect, useMemo, useRef } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { api, ApiError } from "../api/client";
import { CopyButton, ErrorNote, Spinner } from "../components/common";
import { TokenRun } from "../components/CodeLines";
import { highlightLines } from "../lib/highlight";
import { blobUrl, githubBlobUrl, lineHash, shortCommit, type LineRange } from "../lib/links";

interface Props {
  repo: string;
  commit: string;
  currentCommit: string;
  path: string;
  selection: LineRange | null;
}

export function CodeViewer({ repo, commit, currentCommit, path, selection }: Props) {
  const navigate = useNavigate();
  const location = useLocation();
  const scroller = useRef<HTMLDivElement>(null);
  const { data, isLoading, error } = useQuery({
    queryKey: ["file", repo, commit, path],
    queryFn: () => api.file(repo, path, commit),
  });

  const tokens = useMemo(() => (data ? highlightLines(data.lines, data.language) : []), [data]);
  const hidden = useMemo(() => new Set(data?.hidden_lines ?? []), [data]);

  useEffect(() => {
    if (!data) return;
    if (!selection) {
      scroller.current?.scrollTo({ top: 0 });
      return;
    }
    document.getElementById(`L${selection.start}`)?.scrollIntoView({ block: "center" });
  }, [data, selection?.start, selection?.end]); // eslint-disable-line react-hooks/exhaustive-deps

  const selectLine = (event: React.MouseEvent, line: number) => {
    event.preventDefault();
    const next = event.shiftKey && selection ? { start: Math.min(selection.start, line), end: Math.max(selection.start, line) } : { start: line, end: line };
    navigate({ pathname: location.pathname, hash: lineHash(next) }, { replace: true });
  };

  const crumbs = path.split("/");
  const permalink = `${window.location.origin}${blobUrl(repo, data?.commit ?? commit, path, selection)}`;

  return (
    <div className="viewer">
      <div className="viewer-bar">
        <nav className="crumbs" aria-label="File path">
          {crumbs.map((part, index) => (
            <Fragment key={index}>
              {index > 0 ? <span className="crumb-sep">/</span> : null}
              <span className={index === crumbs.length - 1 ? "crumb-current" : "crumb"}>{part}</span>
            </Fragment>
          ))}
        </nav>
        <div className="viewer-actions">
          {data ? (
            <>
              <span className="badge" title={data.commit} data-testid="file-commit">
                <GitCommitHorizontal size={12} aria-hidden />
                {shortCommit(data.commit)}
              </span>
              <span className="badge">{data.lines.length} lines</span>
              <span className="badge">{data.language}</span>
            </>
          ) : null}
          <CopyButton text={permalink} title="Copy a link to this commit and these lines" label="Link" />
          {data?.origin_url ? (
            <a className="btn btn-ghost" href={githubBlobUrl(data.origin_url, data.commit, data.path, selection)} target="_blank" rel="noreferrer noopener">
              <ExternalLink size={14} aria-hidden /> GitHub
            </a>
          ) : null}
        </div>
      </div>
      {data?.has_syntax_errors ? (
        <div className="viewer-note">
          <AlertTriangle size={14} aria-hidden /> The parser found syntax errors in this file. It is shown as committed.
        </div>
      ) : null}
      {data?.hidden_lines.length ? (
        <div className="viewer-note">
          <EyeOff size={14} aria-hidden /> {data.hidden_lines.length} {data.hidden_lines.length === 1 ? "line is hidden because it may hold" : "lines are hidden because they may hold"} a credential.
        </div>
      ) : null}
      <div className="code-scroll" ref={scroller} data-testid="code-view" translate="no">
        {isLoading ? <div className="side-pad"><Spinner label="Reading from Git…" /></div> : null}
        {error ? (
          <div className="side-pad">
            <ErrorNote error={error}>
              {error instanceof ApiError && error.code === "commit_not_indexed" ? (
                <>
                  {" "}
                  <Link to={blobUrl(repo, currentCommit, path, selection)}>Open this file at {shortCommit(currentCommit)}</Link>
                </>
              ) : null}
            </ErrorNote>
          </div>
        ) : null}
        {data ? (
          <div className="code-table">
            {data.lines.map((_, index) => {
              const number = index + 1;
              const selected = selection !== null && number >= selection.start && number <= selection.end;
              return (
                <div key={number} id={`L${number}`} className={`code-line${selected ? " selected" : ""}`} data-line={number}>
                  <a className="line-number" href={`#L${number}`} onClick={(event) => selectLine(event, number)} aria-label={`Line ${number}`}>
                    {number}
                  </a>
                  {hidden.has(number) ? (
                    <code className="line-hidden">
                      <Link2 size={12} aria-hidden /> hidden: this line may hold a credential
                    </code>
                  ) : (
                    <code className="line-code">
                      <TokenRun tokens={tokens[index] ?? []} />
                    </code>
                  )}
                </div>
              );
            })}
          </div>
        ) : null}
      </div>
    </div>
  );
}
