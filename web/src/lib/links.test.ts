import { describe, expect, it } from "vitest";
import { blobUrl, lineHash, parseBlobPath, parseLineHash, repoFromSlug, repoSlug } from "./links";

describe("links", () => {
  it("keeps an owner/name repository in one URL segment", () => {
    expect(repoSlug("octo/hello")).toBe("octo~hello");
    expect(repoFromSlug("octo~hello")).toBe("octo/hello");
    expect(repoFromSlug(repoSlug("stockguard"))).toBe("stockguard");
  });

  it("pins a link to its commit and lines", () => {
    expect(blobUrl("stockguard", "abc1234", "src/a b.ts", { start: 3, end: 9 })).toBe("/r/stockguard/blob/abc1234/src/a%20b.ts#L3-L9");
  });

  it("reads line references, and ignores anything else", () => {
    expect(parseLineHash("#L12")).toEqual({ start: 12, end: 12 });
    expect(parseLineHash("#L12-L20")).toEqual({ start: 12, end: 20 });
    expect(parseLineHash("#L20-L12")).toEqual({ start: 12, end: 20 });
    expect(parseLineHash("#L0")).toBeNull();
    expect(parseLineHash("#<img src=x>")).toBeNull();
    expect(lineHash({ start: 5, end: 5 })).toBe("#L5");
  });

  it("reads a commit and path from a workspace URL", () => {
    expect(parseBlobPath("blob/abc1234/src/a.ts")).toEqual({ commit: "abc1234", path: "src/a.ts" });
    expect(parseBlobPath("blob/not-a-commit/src/a.ts")).toBeNull();
    expect(parseBlobPath(undefined)).toBeNull();
  });
});
