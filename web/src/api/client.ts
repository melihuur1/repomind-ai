import type { components } from "./schema";

type Schemas = components["schemas"];
export type AiStatus = Schemas["AiStatus"];
export type AskResponse = Schemas["AskResponse"];
export type AskSource = Schemas["AskSource"];
export type FileContent = Schemas["FileContent"];
export type Health = Schemas["Health"];
export type IndexJob = Schemas["IndexJob"];
export type Repository = Schemas["Repository"];
export type SearchResponse = Schemas["SearchResponse"];
export type SearchResultItem = Schemas["SearchResultItem"];
export type Tree = Schemas["Tree"];
export type TreeEntry = Schemas["TreeEntry"];
export type SearchMode = "keyword" | "semantic" | "hybrid";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
  ) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, init);
  } catch {
    throw new ApiError(0, "network", "The RepoMind server cannot be reached. Is it still running?");
  }
  const text = await response.text();
  let body: unknown = null;
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    // Not JSON: fall through to the generic message below.
  }
  if (!response.ok) {
    const error = (body ?? {}) as { code?: string; message?: string };
    throw new ApiError(response.status, error.code ?? "error", error.message ?? `The request failed (${response.status}).`);
  }
  return body as T;
}

function query(path: string, params: Record<string, string | number | boolean | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined) search.set(key, String(value));
  }
  return `${path}?${search}`;
}

function sendJson(body: unknown): RequestInit {
  return { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
}

export const api = {
  health: () => request<Health>("/api/health"),
  repositories: () => request<Repository[]>("/api/repositories"),
  addRepository: (url: string) => request<IndexJob>("/api/repositories", sendJson({ url })),
  removeRepository: (repo: string) => request<null>(query("/api/repositories", { repo }), { method: "DELETE" }),
  jobs: () => request<IndexJob[]>("/api/jobs"),
  retryJob: (id: number) => request<IndexJob>(`/api/jobs/${id}/retry`, sendJson({})),
  dismissJob: (id: number) => request<null>(`/api/jobs/${id}`, { method: "DELETE" }),
  tree: (repo: string) => request<Tree>(query("/api/tree", { repo })),
  file: (repo: string, path: string, commit?: string) => request<FileContent>(query("/api/file", { repo, path, commit })),
  search: (repo: string, q: string, mode: SearchMode, includeTests: boolean) =>
    request<SearchResponse>(query("/api/search", { repo, q, mode, limit: 30, include_tests: includeTests ? true : undefined })),
  ask: (repo: string, question: string) => request<AskResponse>("/api/ask", sendJson({ repo, question })),
};
