import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";
import type { AskResponse, AskSource } from "../api/client";
import { AnswerCard } from "./AnswerCard";

const CHECKS = { answer: "passed", declaration: "passed", linked_use: "passed", review_candidate: "failed", closest: "not_made" } as const;

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

function render(sources: AskSource[], changes: Partial<AskResponse> = {}): string {
  const response: AskResponse = {
    experimental: true,
    verdict: "lines_chosen",
    message: "The model chose these lines.",
    ai_used: true,
    ai: { state: "ready", model: "fake", detail: "ready", remote: false },
    model_reading: null,
    model_reading_removed_because: null,
    sources,
    translated_query: null,
    warnings: [],
    discarded: [],
    model: "fake",
    input_tokens: 10,
    output_tokens: 5,
    cost_usd: 0,
    tokens_are_estimated: false,
    cost_is_cautious: false,
    seconds: 1,
    ...changes,
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

  it("shows the declaration of a constant apart, between the model's choice and anything else", () => {
    const html = render([source("answer", "src/Client.java", 30), source("declaration", "src/Limits.java", 2), source("closest", "src/Other.java", 4)]);
    const value = "Value of a constant the chosen lines use";
    const order = [CHOSEN, "src/Client.java", value, "src/Limits.java"].map((text) => html.indexOf(text));
    expect(order.every((position) => position >= 0)).toBe(true);
    expect([...order].sort((a, b) => a - b)).toEqual(order);
    expect(html.match(/data-testid="source-declaration"/g)).toHaveLength(1);
    expect(html.match(/data-testid="source-answer"/g)).toHaveLength(1);
    expect(html).toContain("These lines declare a constant that the lines above use");
    expect(html).not.toContain("source-linked_use");
  });

  it("does not mention added lines when there are none", () => {
    const html = render([source("answer", "src/Client.java", 30), source("closest", "src/Other.java", 4)]);
    expect(html).toContain(CHOSEN);
    expect(html).not.toContain(ADDED);
    expect(html).not.toContain("source-linked_use");
  });

  it("gives a free model's tokens and no cost", () => {
    const html = render([source("answer", "src/Client.java", 30)], { input_tokens: 1234, output_tokens: 56 });
    expect(html).toContain('data-testid="answer-usage"');
    expect(html).toContain("1,234 in, 56 out tokens");
    expect(html).not.toContain("by the prices set ·");
    expect(html).not.toContain("$");
  });

  it("gives a paid answer's cost as counted from the prices set, and says when the tokens are estimated", () => {
    const html = render([source("answer", "src/Client.java", 30)], { input_tokens: 1500, output_tokens: 40, cost_usd: 0.000664, tokens_are_estimated: true });
    expect(html).toContain("1,500 in, 40 out tokens · $0.0007 by the prices set (estimated)");
  });

  it("says a cost is at most this when the server did not say how its cache was used", () => {
    const html = render([source("answer", "src/Client.java", 30)], { input_tokens: 1050, output_tokens: 45, cost_usd: 0.000154, cost_is_cautious: true });
    expect(html).toContain("1,050 in, 45 out tokens · $0.0002 by the prices set (at most)");
  });

  it("gives an exact cost without a qualifier", () => {
    const html = render([source("answer", "src/Client.java", 30)], { input_tokens: 1050, output_tokens: 45, cost_usd: 0.000154 });
    expect(html).toContain("1,050 in, 45 out tokens · $0.0002 by the prices set");
    expect(html).not.toContain("(at most)");
  });

  it("labels lines found without the model, gives the reason, and keeps Git's check", () => {
    const html = render([source("closest", "src/Other.java", 4)], {
      verdict: "ai_unavailable",
      ai_used: false,
      model: null,
      input_tokens: 0,
      output_tokens: 0,
      ai: { state: "auth_failed", model: "gpt-x", detail: "This question was answered without the model. OpenAI did not accept the API key (HTTP 401).", provider: "openai", remote: true, endpoint: "api.openai.com" },
    });
    expect(html).toContain("The API key was refused.");
    expect(html).toContain("did not accept the API key (HTTP 401)");
    expect(html).toContain("Closest lines found by search, without the model: not an");
    expect(html).toContain("In Git at");
    expect(html).not.toContain('data-testid="answer-usage"');
    expect(html).toContain("model not asked");
  });

  it("does not call lines found with the model's help lines found without it", () => {
    const html = render([source("closest", "src/Other.java", 4)], { verdict: "not_enough_evidence" });
    expect(html).toContain("Closest lines found: not an");
    expect(html).not.toContain("without the model");
  });
});
