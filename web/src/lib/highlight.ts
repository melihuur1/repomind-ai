import java from "highlight.js/lib/languages/java";
import javascript from "highlight.js/lib/languages/javascript";
import sql from "highlight.js/lib/languages/sql";
import typescript from "highlight.js/lib/languages/typescript";
import yaml from "highlight.js/lib/languages/yaml";
import { createLowlight } from "lowlight";

/**
 * Syntax colours as data, never as HTML. Repository content is untrusted, so each token ends up
 * as React text, which cannot become markup whatever a file contains.
 */

export interface Token {
  text: string;
  className?: string;
}

const lowlight = createLowlight({ java, javascript, sql, typescript, yaml });
const GRAMMAR: Record<string, string> = { java: "java", javascript: "javascript", sql: "sql", typescript: "typescript", tsx: "typescript", yaml: "yaml" };
const MAX_LINES = 5000;
const MAX_CHARACTERS = 400_000;

interface TreeNode {
  type: string;
  value?: string;
  properties?: { className?: unknown };
  children?: TreeNode[];
}

function walk(node: TreeNode, classes: string[], lines: Token[][]): void {
  if (node.type === "text") {
    const parts = (node.value ?? "").split("\n");
    parts.forEach((part, index) => {
      if (index > 0) lines.push([]);
      if (part) lines[lines.length - 1].push({ text: part, className: classes.join(" ") || undefined });
    });
    return;
  }
  const own = node.type === "element" && Array.isArray(node.properties?.className) ? (node.properties!.className as string[]) : [];
  for (const child of node.children ?? []) walk(child, [...classes, ...own], lines);
}

export function highlightLines(lines: string[], language: string): Token[][] {
  const plain = (): Token[][] => lines.map((line) => (line ? [{ text: line }] : []));
  const grammar = GRAMMAR[language];
  if (!grammar || lines.length > MAX_LINES) return plain();
  const source = lines.join("\n");
  if (source.length > MAX_CHARACTERS) return plain();
  try {
    const result: Token[][] = [[]];
    walk(lowlight.highlight(grammar, source) as unknown as TreeNode, [], result);
    while (result.length < lines.length) result.push([]);
    return result;
  } catch {
    return plain();
  }
}
