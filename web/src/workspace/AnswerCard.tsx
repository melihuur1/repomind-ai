import { CheckCircle2, CircleHelp, CircleSlash, GitCommitHorizontal, Info, Link2, ScanSearch, Sparkles, TriangleAlert } from "lucide-react";
import { Link } from "react-router-dom";
import type { AskResponse, AskSource } from "../api/client";
import { CodeSnippet } from "../components/CodeLines";
import { blobUrl, shortCommit } from "../lib/links";
import { STATE_TITLES, usd } from "./AiNotes";

const VERDICTS = {
  lines_chosen: { icon: Sparkles, title: "The model chose these lines", tone: "chosen" },
  not_enough_evidence: { icon: CircleSlash, title: "Not enough evidence", tone: "refused" },
  invalid_response: { icon: TriangleAlert, title: "No valid answer", tone: "refused" },
  ai_unavailable: { icon: CircleHelp, title: "AI unavailable", tone: "offline" },
} as const;

const ANSWER_CHECK = {
  passed: { label: "Passed the evidence rule, not proof of an answer", className: "check-weak" },
  failed: { label: "Answer not verified", className: "check-failed" },
  not_made: { label: "Not judged", className: "check-none" },
} as const;

function SourceCard({ source }: { source: AskSource }) {
  const check = ANSWER_CHECK[source.answer_check];
  const lines = { start: source.start_line, end: source.end_line };
  return (
    <div className={`source source-${source.role}`} data-testid={`source-${source.role}`}>
      <div className="source-head">
        <Link to={blobUrl(source.repo, source.commit, source.path, lines)} className="source-link" title={source.path} data-testid="source-link">
          {source.path}
          <span className="source-lines">
            :{source.start_line}
            {source.end_line !== source.start_line ? `-${source.end_line}` : ""}
          </span>
        </Link>
      </div>
      <CodeSnippet text={source.text} language={source.language} startLine={source.start_line} />
      <div className="source-checks">
        {source.exists_in_git ? (
          <span className="check check-git" title={`These exact lines are in the file at commit ${source.commit}. That does not mean they answer the question.`}>
            <CheckCircle2 size={12} aria-hidden /> In Git at
            <GitCommitHorizontal size={12} aria-hidden />
            <code>{shortCommit(source.commit)}</code>
          </span>
        ) : null}
        <span className={`check ${check.className}`}>{check.label}</span>
      </div>
    </div>
  );
}

function Section({ title, hint, sources, icon }: { title: string; hint: string; sources: AskSource[]; icon?: React.ReactNode }) {
  if (!sources.length) return null;
  return (
    <section className="answer-section">
      <h4>
        {icon}
        {title}
      </h4>
      <p className="muted">{hint}</p>
      {sources.map((source) => (
        <SourceCard key={`${source.path}:${source.start_line}`} source={source} />
      ))}
    </section>
  );
}

export function AnswerCard({ response }: { response: AskResponse }) {
  const verdict = VERDICTS[response.verdict];
  const Icon = verdict.icon;
  const of = (role: AskSource["role"]) => response.sources.filter((source) => source.role === role);
  return (
    <div className={`answer answer-${verdict.tone}`} data-testid="answer" data-verdict={response.verdict}>
      <div className="answer-verdict">
        <Icon size={15} aria-hidden />
        <strong>{verdict.title}</strong>
        <span className="badge badge-experimental">Experimental</span>
      </div>
      <p className="answer-message">{response.message}</p>
      {!response.ai_used && response.ai.state !== "ready" ? (
        <p className="note" data-testid="answer-ai-state" data-state={response.ai.state}>
          <strong>{STATE_TITLES[response.ai.state]}</strong> {response.ai.detail}
        </p>
      ) : null}

      <Section title="Lines the model chose" hint="They exist at this commit. Whether they answer the question is the model’s judgement." sources={of("answer")} />
      <Section
        title="Value of a constant the chosen lines use"
        hint="Added by RepoMind, not chosen by the model. These lines declare a constant that the lines above use, so that its value is shown with them. They exist at this commit."
        icon={<Link2 size={14} aria-hidden />}
        sources={of("declaration")}
      />
      <Section
        title="Added by RepoMind, not chosen by the model"
        hint="These lines use a constant that is declared in a line the model chose. They exist at this commit. The model did not pick them, and nothing confirms that they answer the question."
        icon={<Link2 size={14} aria-hidden />}
        sources={of("linked_use")}
      />

      {response.model_reading ? (
        <details className="reading" data-testid="model-reading">
          <summary>
            <TriangleAlert size={13} aria-hidden /> Model’s reading: unverified, may be false
          </summary>
          <p>{response.model_reading}</p>
        </details>
      ) : null}
      {response.model_reading_removed_because ? (
        <p className="muted answer-small" data-testid="reading-removed">
          The model’s sentence was left out: {response.model_reading_removed_because}.
        </p>
      ) : null}

      <Section
        title="Source to review"
        hint="The model pointed here, and the evidence rule did not accept it. Nothing confirms that it answers the question, so it is not an answer."
        icon={<ScanSearch size={14} aria-hidden />}
        sources={of("review_candidate")}
      />
      {of("review_use").length ? (
        // Folded, and named as not an answer before it is opened: most such lines are not the right place.
        <details className="closest review-uses" data-testid="review-uses">
          <summary>
            <Link2 size={13} aria-hidden /> Lines that use the constant above, added by RepoMind: not an answer ({of("review_use").length})
          </summary>
          <p className="muted answer-small">
            The model did not choose these lines. They use a constant declared in a line the model pointed to, and they exist at this commit.
            Nothing confirms that they answer the question.
          </p>
          {of("review_use").map((source) => (
            <SourceCard key={`${source.path}:${source.start_line}`} source={source} />
          ))}
        </details>
      ) : null}

      {of("closest").length ? (
        <details className="closest" open={response.verdict === "ai_unavailable"}>
          <summary>
            <Info size={13} aria-hidden /> {response.ai_used ? "Closest lines found" : "Closest lines found by search, without the model"}: not an
            answer ({of("closest").length})
          </summary>
          {of("closest").map((source) => (
            <SourceCard key={`${source.path}:${source.start_line}`} source={source} />
          ))}
        </details>
      ) : null}

      {response.warnings.length ? (
        <ul className="answer-warnings muted">
          {response.warnings.map((warning) => (
            <li key={warning}>{warning}</li>
          ))}
        </ul>
      ) : null}
      <div className="answer-meta muted">
        {response.translated_query ? <span title="Translations can be wrong.">Searched as “{response.translated_query}”</span> : null}
        {response.ai_used && (response.input_tokens || response.output_tokens) ? (
          <span data-testid="answer-usage" title="What this answer used. A cost is RepoMind's count from the prices set, not a bill.">
            {response.input_tokens.toLocaleString("en")} in, {response.output_tokens.toLocaleString("en")} out tokens
            {response.cost_usd > 0 ? ` · ${usd(response.cost_usd)} by the prices set` : ""}
            {response.tokens_are_estimated ? " (estimated)" : response.cost_is_cautious && response.cost_usd > 0 ? " (at most)" : ""}
          </span>
        ) : null}
        <span>
          {response.model ?? "model not asked"} · {response.seconds.toFixed(1)} s
        </span>
      </div>
    </div>
  );
}
