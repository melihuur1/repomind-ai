import type { TreeEntry } from "../api/client";

export interface TreeNode {
  name: string;
  path: string;
  children: TreeNode[];
  file?: TreeEntry;
}

function sortNodes(nodes: TreeNode[]): void {
  nodes.sort((a, b) => Number(Boolean(a.file)) - Number(Boolean(b.file)) || a.name.localeCompare(b.name));
  for (const node of nodes) sortNodes(node.children);
}

/** A folder holding only one folder is shown as one row, as src/main/java/com/example. */
function compress(node: TreeNode): TreeNode {
  let current = node;
  while (!current.file && current.children.length === 1 && !current.children[0].file) {
    const only = current.children[0];
    current = { ...only, name: `${current.name}/${only.name}` };
  }
  return { ...current, children: current.children.map(compress) };
}

export function buildTree(files: TreeEntry[]): TreeNode[] {
  const root: TreeNode = { name: "", path: "", children: [] };
  for (const file of files) {
    let current = root;
    const parts = file.path.split("/");
    parts.forEach((part, index) => {
      const path = parts.slice(0, index + 1).join("/");
      let next = current.children.find((child) => child.name === part);
      if (!next) {
        next = { name: part, path, children: [] };
        current.children.push(next);
      }
      if (index === parts.length - 1) next.file = file;
      current = next;
    });
  }
  sortNodes(root.children);
  return root.children.map(compress);
}

/** Every folder a file sits in, so that opening a file can reveal it. */
export function ancestors(path: string): string[] {
  const parts = path.split("/");
  return parts.slice(0, -1).map((_, index) => parts.slice(0, index + 1).join("/"));
}

export function isOpen(node: TreeNode, closed: Set<string>, opened: Set<string>, current: string | null): boolean {
  if (closed.has(node.path)) return false;
  if (opened.has(node.path)) return true;
  return current !== null && (current === node.path || current.startsWith(`${node.path}/`));
}
