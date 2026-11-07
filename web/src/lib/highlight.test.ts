import { describe, expect, it } from "vitest";
import { highlightLines } from "./highlight";

const text = (tokens: { text: string }[]) => tokens.map((token) => token.text).join("");

describe("highlightLines", () => {
  it("keeps every line and its exact text", () => {
    const lines = ["public class A {", "", "  /* a comment", "     across lines */", "  int b = 1;", "}"];
    const tokens = highlightLines(lines, "java");
    expect(tokens).toHaveLength(lines.length);
    expect(tokens.map(text)).toEqual(lines);
  });

  it("colours keywords", () => {
    const [first] = highlightLines(["const a = 1;"], "typescript");
    expect(first.find((token) => token.text === "const")?.className).toContain("hljs-keyword");
  });

  it("returns markup in a file as plain text tokens", () => {
    const line = `const a = "<img src=x onerror=alert(1)>";`;
    const [tokens] = highlightLines([line], "typescript");
    expect(text(tokens)).toBe(line);
  });

  it("leaves an unknown language plain", () => {
    expect(highlightLines(["hello"], "cobol")).toEqual([[{ text: "hello" }]]);
  });
});
