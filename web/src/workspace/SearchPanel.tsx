import { useQuery } from "@tanstack/react-query";
import { Languages, Search as SearchIcon } from "lucide-react";
import { useState, type FormEvent, type RefObject } from "react";
import { Link } from "react-router-dom";
import { api, type SearchMode, type SearchResultItem } from "../api/client";
import { CodeSnippet } from "../components/CodeLines";
import { EmptyNote, ErrorNote, Spinner } from "../components/common";
import { blobUrl } from "../lib/links";

const MODES: { value: SearchMode; label: string; title: string }[] = [
  { value: "hybrid", label: "Hybrid", title: "Both, combined by rank" },
  { value: "semantic", label: "Semantic", title: "By meaning, with local embeddings" },
  { value: "keyword", label: "Keyword", title: "By exact words and names, with BM25" },
];

function Reasons({ item }: { item: SearchResultItem }) {
  const reasons = item.reasons as { ranks?: Record<string, number>; matched_terms?: Record<string, number>; cosine_similarity?: number };
  const parts: string[] = [];
  if (reasons.ranks) parts.push(Object.entries(reasons.ranks).map(([list, rank]) => `${list.replaceAll("_", " ")} #${rank}`).join(", "));
  if (reasons.matched_terms) parts.push(`terms: ${Object.keys(reasons.matched_terms).slice(0, 8).join(", ")}`);
  if (reasons.cosine_similarity !== undefined) parts.push(`cosine ${reasons.cosine_similarity}`);
  if (!parts.length) return null;
  return (
    <details className="reasons">
      <summary>Why this result</summary>
      <p>{parts.join(" · ")}</p>
    </details>
  );
}

function Result({ item, repo, commit }: { item: SearchResultItem; repo: string; commit: string }) {
  const name = item.path.split("/").pop();
  const folder = item.path.slice(0, item.path.length - (name?.length ?? 0));
  return (
    <li className="result" data-testid="search-result">
      <Link className="result-head" to={blobUrl(repo, commit, item.path, { start: item.start_line, end: item.end_line })}>
        <span className="result-rank">{item.rank}</span>
        <span className="result-path" title={item.path}>
          <span className="muted">{folder}</span>
          <strong>{name}</strong>
        </span>
        <span className="result-lines">
          {item.start_line}–{item.end_line}
        </span>
      </Link>
      {item.symbol ? <div className="result-symbol">{item.symbol}</div> : null}
      <CodeSnippet text={item.preview} language={item.language} startLine={item.start_line} maxLines={5} />
      <Reasons item={item} />
    </li>
  );
}

export function SearchPanel({ repo, commit, inputRef }: { repo: string; commit: string; inputRef: RefObject<HTMLInputElement | null> }) {
  const [draft, setDraft] = useState("");
  const [mode, setMode] = useState<SearchMode>("hybrid");
  const [includeTests, setIncludeTests] = useState(false);
  const [query, setQuery] = useState("");

  const { data, isFetching, error } = useQuery({
    queryKey: ["search", repo, commit, query, mode, includeTests],
    queryFn: () => api.search(repo, query, mode, includeTests),
    enabled: query.length > 0,
    staleTime: 60_000,
  });

  const submit = (event: FormEvent) => {
    event.preventDefault();
    setQuery(draft.trim());
  };

  return (
    <div className="search">
      <form className="search-form" onSubmit={submit} role="search">
        <div className="search-box">
          <SearchIcon size={14} aria-hidden />
          <input
            ref={inputRef}
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            placeholder="Search code, in Turkish or English"
            aria-label="Search code"
            maxLength={500}
            data-testid="search-input"
          />
        </div>
        <div className="search-options">
          <div className="segmented" role="radiogroup" aria-label="Search mode">
            {MODES.map((option) => (
              <button
                key={option.value}
                type="button"
                role="radio"
                aria-checked={mode === option.value}
                className={mode === option.value ? "active" : ""}
                title={option.title}
                data-testid={`mode-${option.value}`}
                onClick={() => setMode(option.value)}
              >
                {option.label}
              </button>
            ))}
          </div>
          <label className="check" title="Tests are left out unless a query mentions them">
            <input type="checkbox" checked={includeTests} onChange={(event) => setIncludeTests(event.target.checked)} />
            Tests
          </label>
        </div>
      </form>
      <div className="search-results">
        {isFetching ? <div className="side-pad"><Spinner label="Searching…" /></div> : null}
        {error ? <div className="side-pad"><ErrorNote error={error} /></div> : null}
        {data && !isFetching ? (
          <>
            <div className="search-summary muted">
              {data.results.length} {data.results.length === 1 ? "result" : "results"} · {data.seconds.toFixed(2)} s
            </div>
            {data.translated_query ? (
              <div className="search-translated" title="A local translation, also searched. It can be wrong.">
                <Languages size={13} aria-hidden /> also searched as “{data.translated_query}”
              </div>
            ) : null}
            {data.warnings.map((warning) => (
              <div className="note" key={warning}>{warning}</div>
            ))}
            {data.results.length === 0 ? <EmptyNote title="No code matches this query." /> : null}
            <ol className="result-list">
              {data.results.map((item) => (
                <Result key={`${item.path}:${item.start_line}`} item={item} repo={repo} commit={commit} />
              ))}
            </ol>
          </>
        ) : null}
        {!query && !isFetching ? (
          <EmptyNote title="Search this repository">Try a phrase like “how is overselling prevented”, or an exact name such as a config key.</EmptyNote>
        ) : null}
      </div>
    </div>
  );
}
