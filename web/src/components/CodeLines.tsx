import { useMemo } from "react";
import { highlightLines, type Token } from "../lib/highlight";

function TokenRun({ tokens }: { tokens: Token[] }) {
  return (
    <>
      {tokens.map((token, index) =>
        token.className ? (
          <span key={index} className={token.className}>
            {token.text}
          </span>
        ) : (
          token.text
        ),
      )}
    </>
  );
}

/** A short piece of code inside a search result or an answer. */
export function CodeSnippet({ text, language, startLine, maxLines }: { text: string; language: string; startLine: number; maxLines?: number }) {
  const all = useMemo(() => text.split("\n"), [text]);
  const lines = useMemo(() => (maxLines ? all.slice(0, maxLines) : all), [all, maxLines]);
  const tokens = useMemo(() => highlightLines(lines, language), [lines, language]);
  return (
    <div className="snippet" translate="no">
      {lines.map((_, index) => (
        <div className="snippet-line" key={index}>
          <span className="snippet-number">{startLine + index}</span>
          <code>
            <TokenRun tokens={tokens[index] ?? []} />
          </code>
        </div>
      ))}
      {maxLines && all.length > maxLines ? <div className="snippet-more">{all.length - maxLines} more lines</div> : null}
    </div>
  );
}

export { TokenRun };
