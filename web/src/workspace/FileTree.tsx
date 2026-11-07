import { useQuery } from "@tanstack/react-query";
import { ChevronDown, ChevronRight, FileCode2, FileWarning, Folder, FolderOpen, Search as SearchIcon } from "lucide-react";
import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import { EmptyNote, ErrorNote, Spinner } from "../components/common";
import { blobUrl } from "../lib/links";
import { buildTree, isOpen, type TreeNode } from "../lib/tree";

interface RowProps {
  node: TreeNode;
  depth: number;
  repo: string;
  commit: string;
  currentPath: string | null;
  closed: Set<string>;
  opened: Set<string>;
  toggle: (node: TreeNode, open: boolean) => void;
}

function Row({ node, depth, repo, commit, currentPath, closed, opened, toggle }: RowProps) {
  const indent = { paddingLeft: `${8 + depth * 14}px` };
  if (node.file) {
    const active = node.path === currentPath;
    return (
      <li>
        <Link
          to={blobUrl(repo, commit, node.path)}
          className={`tree-row tree-file${active ? " active" : ""}${node.file.is_test ? " is-test" : ""}`}
          style={indent}
          aria-current={active ? "page" : undefined}
          data-testid="tree-file"
          title={node.path}
        >
          {node.file.has_syntax_errors ? <FileWarning size={14} aria-label="Has syntax errors" /> : <FileCode2 size={14} className={`file-icon language-text-${node.file.language}`} aria-hidden />}
          <span className="tree-name">{node.name}</span>
        </Link>
      </li>
    );
  }
  const open = isOpen(node, depth, closed, opened, currentPath);
  return (
    <li>
      <button type="button" className="tree-row tree-folder" style={indent} aria-expanded={open} onClick={() => toggle(node, !open)} title={node.path}>
        {open ? <ChevronDown size={13} aria-hidden /> : <ChevronRight size={13} aria-hidden />}
        {open ? <FolderOpen size={14} aria-hidden /> : <Folder size={14} aria-hidden />}
        <span className="tree-name">{node.name}</span>
      </button>
      {open ? (
        <ul>
          {node.children.map((child) => (
            <Row key={child.path} node={child} depth={depth + 1} repo={repo} commit={commit} currentPath={currentPath} closed={closed} opened={opened} toggle={toggle} />
          ))}
        </ul>
      ) : null}
    </li>
  );
}

export function FileTree({ repo, commit, currentPath }: { repo: string; commit: string; currentPath: string | null }) {
  const { data, isLoading, error } = useQuery({ queryKey: ["tree", repo, commit], queryFn: () => api.tree(repo) });
  const [filter, setFilter] = useState("");
  const [closed, setClosed] = useState<Set<string>>(new Set());
  const [opened, setOpened] = useState<Set<string>>(new Set());

  const nodes = useMemo(() => (data ? buildTree(data.files) : []), [data]);
  const needle = filter.trim().toLowerCase();
  const matches = useMemo(() => (needle && data ? data.files.filter((file) => file.path.toLowerCase().includes(needle)) : []), [data, needle]);

  const toggle = (node: TreeNode, open: boolean) => {
    const change = (set: Set<string>, add: boolean) => {
      const next = new Set(set);
      if (add) next.add(node.path);
      else next.delete(node.path);
      return next;
    };
    setClosed((set) => change(set, !open));
    setOpened((set) => change(set, open));
  };

  if (isLoading) return <div className="side-pad"><Spinner /></div>;
  if (error) return <div className="side-pad"><ErrorNote error={error} /></div>;
  if (!data) return null;

  return (
    <div className="tree">
      <div className="tree-filter">
        <SearchIcon size={13} aria-hidden />
        <input value={filter} onChange={(event) => setFilter(event.target.value)} placeholder="Filter files by name" aria-label="Filter files by name" />
      </div>
      <nav className="tree-scroll" aria-label="Files">
        {needle ? (
          matches.length ? (
            <ul>
              {matches.map((file) => (
                <li key={file.path}>
                  <Link to={blobUrl(repo, commit, file.path)} className={`tree-row tree-file tree-flat${file.path === currentPath ? " active" : ""}`} title={file.path} data-testid="tree-file">
                    <FileCode2 size={14} className={`file-icon language-text-${file.language}`} aria-hidden />
                    <span className="tree-name">{file.path}</span>
                  </Link>
                </li>
              ))}
            </ul>
          ) : (
            <EmptyNote title="No file matches." />
          )
        ) : (
          <ul>
            {nodes.map((node) => (
              <Row key={node.path} node={node} depth={0} repo={repo} commit={commit} currentPath={currentPath} closed={closed} opened={opened} toggle={toggle} />
            ))}
          </ul>
        )}
      </nav>
      <div className="tree-foot muted">
        {data.files.length} indexed of {data.files_in_commit} files in this commit
      </div>
    </div>
  );
}
