import { describe, expect, it } from "vitest";
import type { TreeEntry } from "../api/client";
import { ancestors, buildTree } from "./tree";

const entry = (path: string): TreeEntry => ({ path, language: "java", line_count: 1, has_syntax_errors: false, is_test: false });

describe("buildTree", () => {
  it("lists folders before files, and joins folders that hold only one folder", () => {
    const tree = buildTree([entry("compose.yaml"), entry("src/main/java/A.java"), entry("src/main/java/B.java")]);
    expect(tree.map((node) => node.name)).toEqual(["src/main/java", "compose.yaml"]);
    expect(tree[0].path).toBe("src/main/java");
    expect(tree[0].children.map((node) => node.name)).toEqual(["A.java", "B.java"]);
  });

  it("does not join a folder with a file beside it", () => {
    const tree = buildTree([entry("src/a.ts"), entry("src/lib/b.ts")]);
    expect(tree[0].name).toBe("src");
    expect(tree[0].children.map((node) => node.name)).toEqual(["lib", "a.ts"]);
  });

  it("finds every folder a file sits in", () => {
    expect(ancestors("a/b/c.ts")).toEqual(["a", "a/b"]);
  });
});
