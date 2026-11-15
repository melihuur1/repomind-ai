import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";
import type { AskResponse, AskSource } from "../api/client";
import { AnswerCard } from "./AnswerCard";

const CHECKS = { answer: "passed", linked_use: "passed", review_candidate: "failed", closest: "not_made" } as const;

function source(role: AskSource["role"], path: string, line: number): AskSource {
  return {
    role,
    repo: "shop",
    commit: "abc1234abc1234abc1234abc1234abc1234abc12",
    path,
    language: "java",
    symbol: null,
    start_line: line,
    end_line: line,
    text: "return 1;",
    exists_in_git: true,
    answer_check: CHECKS[role],
  };
}

function render(sources: AskSource[]): string {
  const response: AskResponse = {
    experimental: true,
    verdict: "lines_chosen",
    message: "The model chose these lines.",
    ai_used: true,
    ai: { state: "ready", model: "fake", detail: "ready" },
    model_reading: null,
    model_reading_removed_because: null,
    sources,
    translated_query: null,
    warnings: [],
    discarded: [],
    model: "fake",
    input_tokens: 10,
    output_tokens: 5,
    seconds: 1,
  };
  return renderToStaticMarkup(
    <MemoryRouter>
      <AnswerCard response={response} />
    </MemoryRouter>,
  );
}

const CHOSEN = "Lines the model chose";
const ADDED = "Added by RepoMind, not chosen by the model";

describe("AnswerCard", () => {
  it("keeps a line RepoMind added apart from the lines the model chose", () => {
    const html = render([source("answer", "src/Limits.java", 2), source("linked_use", "src/Client.java", 30)]);
    const order = [CHOSEN, "src/Limits.java", ADDED, "src/Client.java"].map((text) => html.indexOf(text));
    expect(order.every((position) => position >= 0)).toBe(true);
    expect([...order].sort((a, b) => a - b)).toEqual(order);
    expect(html.match(/data-testid="source-answer"/g)).toHaveLength(1);
    expect(html.match(/data-testid="source-linked_use"/g)).toHaveLength(1);
    expect(html).toContain("The model did not pick them");
  });

  it("does not mention added lines when there are none", () => {
    const html = render([source("answer", "src/Client.java", 30), source("closest", "src/Other.java", 4)]);
    expect(html).toContain(CHOSEN);
    expect(html).not.toContain(ADDED);
    expect(html).not.toContain("source-linked_use");
  });
});
