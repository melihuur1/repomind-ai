/** Links are pinned to a commit, so a line reference keeps meaning the same line. */

export interface LineRange {
  start: number;
  end: number;
}

// GitHub names cannot contain "~", so "owner/name" fits in one URL segment.
export const repoSlug = (name: string): string => name.replace("/", "~");
export const repoFromSlug = (slug: string): string => slug.replace("~", "/");

export const repoUrl = (name: string): string => `/r/${encodeURIComponent(repoSlug(name))}`;

export function blobUrl(repo: string, commit: string, path: string, lines?: LineRange | null): string {
  const encoded = path.split("/").map(encodeURIComponent).join("/");
  return `${repoUrl(repo)}/blob/${commit}/${encoded}${lines ? lineHash(lines) : ""}`;
}

export function lineHash({ start, end }: LineRange): string {
  return start === end ? `#L${start}` : `#L${start}-L${end}`;
}

export function parseLineHash(hash: string): LineRange | null {
  const match = /^#L(\d+)(?:-L?(\d+))?$/.exec(hash);
  if (!match) return null;
  const first = Number(match[1]);
  const second = match[2] ? Number(match[2]) : first;
  if (first < 1 || second < 1) return null;
  return { start: Math.min(first, second), end: Math.max(first, second) };
}

/** The file and commit in a workspace URL's tail: "blob/<commit>/<path>". */
export function parseBlobPath(tail: string | undefined): { commit: string; path: string } | null {
  const match = /^blob\/([0-9a-fA-F]{7,40})\/(.+)$/.exec(tail ?? "");
  return match ? { commit: match[1], path: match[2] } : null;
}

export const shortCommit = (commit: string): string => commit.slice(0, 7);

export function githubBlobUrl(origin: string, commit: string, path: string, lines?: LineRange | null): string {
  const encoded = path.split("/").map(encodeURIComponent).join("/");
  const anchor = lines ? (lines.start === lines.end ? `#L${lines.start}` : `#L${lines.start}-L${lines.end}`) : "";
  return `${origin}/blob/${commit}/${encoded}${anchor}`;
}
