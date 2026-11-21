import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import type { AiStatus } from "../api/client";
import { AiNotes, accuracyText, providerText, STATE_TITLES, usd } from "./AiNotes";

const LOCAL: AiStatus = { state: "ready", model: "qwen2.5:7b", detail: "The local model is available.", provider: "ollama", remote: false, endpoint: null };
const MEASURED = { model: "qwen2.5:7b", fully_right: 13, wrong: 6, questions: 32, measured_on: "2026-11-15", record: "eval/V2_EVALUATION.md" };
const OPENAI: AiStatus = {
  state: "ready",
  model: "gpt-x-mini",
  detail: "The OpenAI API is in use. Each question, and the lines of code chosen for it, are sent to api.openai.com.",
  provider: "openai",
  remote: true,
  endpoint: "api.openai.com",
  spending: { budget_usd: 1, recorded_usd: 0.0123, left_usd: 0.9877, worst_case_usd: 0, estimated_usd: 0, legacy_usd: 0, input_usd_per_mtok: 0.4, output_usd_per_mtok: 1.6 },
};

const render = (ai: AiStatus) => renderToStaticMarkup(<AiNotes ai={ai} />);
// The markup writes an apostrophe as an entity.
const asMarkup = (text: string) => text.replace(/'/g, "&#x27;");

describe("the model's accuracy", () => {
  it("is given for the model that was measured, with when and that later changes are not", () => {
    const text = accuracyText({ ...LOCAL, accuracy: MEASURED });
    expect(text).toContain("Measured on 2026-11-15 with qwen2.5:7b");
    expect(text).toContain("13 of 32 questions, and a wrong one for 6");
    expect(text).toContain("Changes made since are not measured");
  });

  it("is said to be unknown for a model that was not measured, with no figure made up", () => {
    const text = accuracyText(OPENAI);
    expect(text).toContain("gpt-x-mini has not been evaluated in RepoMind yet");
    expect(text).not.toMatch(/\d+ of \d+/);
    expect(accuracyText({ ...LOCAL, model: "qwen2.5-coder:7b" })).toContain("has not been evaluated");
  });

  it("names no model when there is none", () => {
    expect(accuracyText({ state: "not_configured", detail: "No model is configured.", remote: false })).toBe("Experimental. Read the code it points at.");
  });
});

describe("the provider", () => {
  it("is a local model, a model on another machine, or the OpenAI API in the cloud", () => {
    expect(providerText(LOCAL)).toBe("qwen2.5:7b, a local model on this machine (Ollama).");
    expect(providerText({ ...LOCAL, remote: true, endpoint: "models.example.org" })).toBe("qwen2.5:7b, on another machine: models.example.org.");
    expect(providerText(OPENAI)).toBe("gpt-x-mini through the OpenAI API, in the cloud.");
    expect(providerText({ state: "misconfigured", detail: "bad", remote: false })).toBeNull();
  });
});

describe("the privacy note", () => {
  it("says what goes to OpenAI and does not promise that nothing is kept", () => {
    const html = render(OPENAI);
    expect(html).toContain('data-testid="ai-privacy"');
    expect(html).toContain("the lines of code chosen for it, are sent to OpenAI (api.openai.com)");
    expect(html).toContain("store: false");
    expect(html).toContain("That is not a promise that nothing is kept");
    expect(html).not.toMatch(/zero (data )?retention|nothing is stored|never stored/i);
  });

  it("is shown for OpenAI whatever its state, and never for a local model", () => {
    expect(render({ ...OPENAI, state: "auth_failed", detail: "OpenAI did not accept the API key (HTTP 401)." })).toContain("ai-privacy");
    expect(render(LOCAL)).not.toContain("ai-privacy");
    expect(render({ ...LOCAL, state: "offline", detail: "not running" })).not.toContain("ai-privacy");
  });

  it("leaves a remote server of one's own to its own note", () => {
    const html = render({ ...LOCAL, remote: true, endpoint: "models.example.org", detail: "A remote model is in use." });
    expect(html).toContain('data-testid="ai-remote"');
    expect(html).not.toContain("ai-privacy");
  });
});

describe("each state", () => {
  const states = Object.keys(STATE_TITLES) as (keyof typeof STATE_TITLES)[];

  it("has its own sentence", () => {
    expect(states.sort()).toEqual(
      ["auth_failed", "budget_reached", "limited", "low_memory", "misconfigured", "model_missing", "not_configured", "offline", "rejected"].sort(),
    );
    expect(new Set(Object.values(STATE_TITLES)).size).toBe(states.length);
  });

  it.each(states)("%s is explained with the server's detail, and search is said to keep working", (state) => {
    const html = render({ ...OPENAI, state, detail: `Safe detail for ${state}.` });
    expect(html).toContain(`data-state="${state}"`);
    expect(html).toContain(asMarkup(STATE_TITLES[state]));
    expect(html).toContain(`Safe detail for ${state}.`);
    expect(html).toContain("Search and the code view keep working");
  });

  it("ready shows no unavailable note", () => {
    expect(render(OPENAI)).not.toContain("ai-unavailable");
  });
});

describe("spending", () => {
  it("gives what was counted, the budget and what is left, as RepoMind's count and not a bill", () => {
    const html = render(OPENAI);
    expect(html).toContain("$0.0123 of a $1.00 budget,");
    expect(html).toContain("$0.9877 left");
    expect(html).toContain("$0.4000 in and $1.60 out per million tokens");
    expect(html).toContain("not OpenAI&#x27;s bill");
    expect(html).not.toContain("partly estimated");
    expect(html).not.toContain("ai-spending-worst-case");
  });

  it("says which part is a cautious count for failed requests, and which part is estimated", () => {
    const html = render({ ...OPENAI, spending: { ...OPENAI.spending!, recorded_usd: 0.05, worst_case_usd: 0.002, estimated_usd: 0.0003 } });
    expect(html).toContain("partly estimated");
    expect(html).toContain("$0.0020 of it is a cautious count");
    expect(html).toContain("Whether they were charged is not known");
    expect(html).toContain("$0.0003 of it is estimated");
  });

  it("says which part was recorded before costs were labelled, and does not call it reported", () => {
    const html = render({ ...OPENAI, spending: { ...OPENAI.spending!, recorded_usd: 0.05, legacy_usd: 0.03 } });
    expect(html).toContain("partly of an unknown kind");
    expect(html).toContain("$0.0300 of it was recorded before RepoMind kept how each cost was counted");
    expect(html).not.toContain("partly estimated");
  });

  it("is not shown for a free model", () => {
    expect(render(LOCAL)).not.toContain("ai-spending");
  });

  it("formats small and large sums", () => {
    expect(usd(0.000664)).toBe("$0.0007");
    expect(usd(2.5)).toBe("$2.50");
    expect(usd(0.000012)).toBe("under $0.0001");
    expect(usd(0)).toBe("$0.0000");
  });
});
