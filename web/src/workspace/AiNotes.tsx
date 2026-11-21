import type { AiStatus } from "../api/client";

type State = AiStatus["state"];

/** What each state means, in a sentence. The server's own detail follows it. */
export const STATE_TITLES: Record<Exclude<State, "ready">, string> = {
  not_configured: "No model is set up.",
  misconfigured: "The model settings are not valid.",
  auth_failed: "The API key was refused.",
  budget_reached: "The budget for the model is spent.",
  limited: "A usage limit was reached.",
  rejected: "The model's server did not accept RepoMind's request.",
  model_missing: "The model is not available.",
  offline: "The model cannot be reached.",
  low_memory: "The model is paused for lack of memory.",
};

export function usd(value: number): string {
  if (value > 0 && value < 0.0001) return "under $0.0001";
  return `$${value >= 1 ? value.toFixed(2) : value.toFixed(4)}`;
}

/** A figure only for the model it was measured with, and none made up for any other. */
export function accuracyText(ai: AiStatus): string {
  const measured = ai.accuracy;
  if (measured) {
    return (
      `Experimental. Measured on ${measured.measured_on} with ${measured.model}: a fully right source for ` +
      `${measured.fully_right} of ${measured.questions} questions, and a wrong one for ${measured.wrong}. ` +
      "Changes made since are not measured. Read the code it points at."
    );
  }
  if (ai.model) {
    return `Experimental. ${ai.model} has not been evaluated in RepoMind yet, so how often it points at the right code is not known. Read the code it points at.`;
  }
  return "Experimental. Read the code it points at.";
}

export function providerText(ai: AiStatus): string | null {
  if (!ai.provider || !ai.model) return null;
  if (ai.provider === "openai") return `${ai.model} through the OpenAI API, in the cloud.`;
  if (ai.remote) return `${ai.model}, on another machine: ${ai.endpoint ?? "a remote server"}.`;
  return `${ai.model}, a local model on this machine (Ollama).`;
}

function Spending({ spending, provider }: { spending: NonNullable<AiStatus["spending"]>; provider: AiStatus["provider"] }) {
  const bill = provider === "openai" ? "OpenAI's bill. OpenAI's usage page has the real figures." : "the provider's bill.";
  return (
    // Folded, so that the conversation keeps its room. The cautious and estimated parts are inside, and are said.
    <details className="note ai-spending" data-testid="ai-spending">
      <summary>
        <strong>Spending counted by RepoMind:</strong> {usd(spending.recorded_usd)} of a {usd(spending.budget_usd)} budget,{" "}
        {usd(spending.left_usd)} left{spending.worst_case_usd > 0 || spending.estimated_usd > 0 ? ", partly estimated" : ""}
        {spending.legacy_usd > 0 ? ", partly of an unknown kind" : ""}.
      </summary>
      <p>
        Prices set: {usd(spending.input_usd_per_mtok)} in and {usd(spending.output_usd_per_mtok)} out per million tokens.
      </p>
      {spending.worst_case_usd > 0 ? (
        <p data-testid="ai-spending-worst-case">
          {usd(spending.worst_case_usd)} of it is a cautious count: requests that failed after they may have reached the model, counted at
          the most they could cost. Whether they were charged is not known.
        </p>
      ) : null}
      {spending.estimated_usd > 0 ? (
        <p data-testid="ai-spending-estimated">
          {usd(spending.estimated_usd)} of it is estimated, because the server did not report the token counts.
        </p>
      ) : null}
      {spending.legacy_usd > 0 ? (
        <p data-testid="ai-spending-legacy">
          {usd(spending.legacy_usd)} of it was recorded before RepoMind kept how each cost was counted, so whether it was reported by the
          server or estimated is not known.
        </p>
      ) : null}
      <p className="muted">This is RepoMind's own count from the prices you set, not {bill}</p>
    </details>
  );
}

/** What the AI panel says about the model in use: where it runs, what is sent, what it cost, and what was measured. */
export function AiNotes({ ai }: { ai: AiStatus }) {
  const ready = ai.state === "ready";
  const provider = providerText(ai);
  return (
    <>
      <p className="ai-warning" data-testid="ai-warning">
        {accuracyText(ai)}
      </p>
      {provider ? (
        <p className="ai-provider muted" data-testid="ai-provider">
          Model: {provider}
        </p>
      ) : null}
      {ai.provider === "openai" ? (
        <div className="note ai-privacy" data-testid="ai-privacy">
          <p>
            <strong>Sent to OpenAI.</strong> Each question, and the lines of code chosen for it, are sent to OpenAI ({ai.endpoint ?? "api.openai.com"}) to
            be answered. Lines that may hold a credential are left out first.
          </p>
          <p>
            RepoMind asks OpenAI not to store the exchange for its own products (store: false). That is not a promise that nothing is kept:
            OpenAI's own data policy decides that. Use it only for code you may share with OpenAI.
          </p>
        </div>
      ) : null}
      {ready && ai.provider !== "openai" && ai.remote ? (
        <p className="note" data-testid="ai-remote">
          <strong>Remote model.</strong> {ai.detail}
        </p>
      ) : null}
      {ai.state !== "ready" ? (
        <div className="note" data-testid="ai-unavailable" data-state={ai.state}>
          <p>
            <strong>{STATE_TITLES[ai.state]}</strong> {ai.detail}
          </p>
          <p>Search and the code view keep working, and a question will return the closest lines without a model.</p>
        </div>
      ) : null}
      {ai.spending ? <Spending spending={ai.spending} provider={ai.provider} /> : null}
    </>
  );
}
