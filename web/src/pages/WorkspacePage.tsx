import { useQuery } from "@tanstack/react-query";
import { FolderTree, PanelRightClose, Search, Sparkles } from "lucide-react";
import { useCallback, useEffect, useRef, useState, type PointerEvent as ReactPointerEvent } from "react";
import { useLocation, useParams } from "react-router-dom";
import { api } from "../api/client";
import { ErrorNote, Spinner } from "../components/common";
import { TopBar } from "../components/TopBar";
import { parseBlobPath, parseLineHash, repoFromSlug } from "../lib/links";
import { readNumber, writeNumber } from "../lib/theme";
import { AiPanel, type Turn } from "../workspace/AiPanel";
import { CodeViewer } from "../workspace/CodeViewer";
import { FileTree } from "../workspace/FileTree";
import { RepositoryOverview } from "../workspace/RepositoryOverview";
import { SearchPanel } from "../workspace/SearchPanel";

type SideTab = "files" | "search";

function usePanelWidth(key: string, fallback: number, min: number, max: number, side: "left" | "right") {
  const [width, setWidth] = useState(() => readNumber(key, fallback));
  const start = useCallback(
    (event: ReactPointerEvent) => {
      event.preventDefault();
      const originX = event.clientX;
      const originWidth = width;
      let latest = originWidth;
      const move = (next: PointerEvent) => {
        const delta = next.clientX - originX;
        latest = Math.min(max, Math.max(min, originWidth + (side === "left" ? delta : -delta)));
        setWidth(latest);
      };
      const stop = () => {
        window.removeEventListener("pointermove", move);
        window.removeEventListener("pointerup", stop);
        writeNumber(key, latest);
      };
      window.addEventListener("pointermove", move);
      window.addEventListener("pointerup", stop);
    },
    [key, max, min, side, width],
  );
  return [width, start] as const;
}

export function WorkspacePage() {
  const params = useParams();
  const { hash } = useLocation();
  const repo = repoFromSlug(params.repo ?? "");
  const blob = parseBlobPath(params["*"]);
  const selection = parseLineHash(hash);

  const [tab, setTab] = useState<SideTab>("files");
  const [aiOpen, setAiOpen] = useState(true);
  const [conversations, setConversations] = useState<Record<string, Turn[]>>({});
  const [leftWidth, startLeft] = usePanelWidth("repomind:left", 300, 220, 560, "left");
  const [rightWidth, startRight] = usePanelWidth("repomind:right", 420, 320, 720, "right");
  const searchInput = useRef<HTMLInputElement>(null);

  const repositories = useQuery({ queryKey: ["repositories"], queryFn: api.repositories });
  const repository = repositories.data?.find((item) => item.name === repo);

  useEffect(() => {
    document.title = blob ? `${blob.path.split("/").pop()} · ${repo}` : `${repo} · RepoMind AI`;
  }, [blob, repo]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement;
      if (event.key === "/" && !["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName)) {
        event.preventDefault();
        setTab("search");
        window.setTimeout(() => searchInput.current?.focus(), 0);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const setTurns = useCallback(
    (update: (turns: Turn[]) => Turn[]) => setConversations((all) => ({ ...all, [repo]: update(all[repo] ?? []) })),
    [repo],
  );

  if (repositories.isLoading) {
    return (
      <div className="centered">
        <Spinner />
      </div>
    );
  }
  if (repositories.error || !repository) {
    return (
      <div className="home">
        <TopBar />
        <main className="home-main">
          <ErrorNote error={repositories.error ?? new Error()}>{repositories.error ? null : `"${repo}" is not indexed.`}</ErrorNote>
        </main>
      </div>
    );
  }

  return (
    <div className="workspace">
      <TopBar repository={repository} />
      <div
        className="workspace-grid"
        style={{ gridTemplateColumns: aiOpen ? `${leftWidth}px 5px minmax(0, 1fr) 5px ${rightWidth}px` : `${leftWidth}px 5px minmax(0, 1fr)` }}
      >
        <aside className="side" aria-label="Files and search">
          <div className="side-tabs" role="tablist">
            <button type="button" role="tab" aria-selected={tab === "files"} className={tab === "files" ? "active" : ""} onClick={() => setTab("files")}>
              <FolderTree size={14} aria-hidden /> Files
            </button>
            <button type="button" role="tab" aria-selected={tab === "search"} className={tab === "search" ? "active" : ""} onClick={() => setTab("search")} data-testid="search-tab">
              <Search size={14} aria-hidden /> Search <kbd>/</kbd>
            </button>
          </div>
          <div className={tab === "files" ? "side-body" : "side-body hidden"}>
            <FileTree repo={repo} commit={repository.commit} currentPath={blob?.path ?? null} />
          </div>
          <div className={tab === "search" ? "side-body" : "side-body hidden"}>
            <SearchPanel repo={repo} commit={repository.commit} inputRef={searchInput} />
          </div>
        </aside>
        <div className="resizer" onPointerDown={startLeft} aria-hidden />
        <main className="center">
          {blob ? <CodeViewer repo={repo} commit={blob.commit} currentCommit={repository.commit} path={blob.path} selection={selection} /> : <RepositoryOverview repository={repository} />}
          {!aiOpen ? (
            <button type="button" className="btn ai-reopen" onClick={() => setAiOpen(true)}>
              <Sparkles size={14} aria-hidden /> Experimental AI Analysis
            </button>
          ) : null}
        </main>
        {aiOpen ? (
          <>
            <div className="resizer" onPointerDown={startRight} aria-hidden />
            <aside className="ai" aria-label="Experimental AI Analysis">
              <AiPanel
                repo={repo}
                turns={conversations[repo] ?? []}
                setTurns={setTurns}
                closeButton={
                  <button type="button" className="btn btn-ghost btn-icon" title="Hide this panel" aria-label="Hide this panel" onClick={() => setAiOpen(false)}>
                    <PanelRightClose size={15} aria-hidden />
                  </button>
                }
              />
            </aside>
          </>
        ) : null}
      </div>
    </div>
  );
}
