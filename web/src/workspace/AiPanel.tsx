import { FlaskConical, Send } from "lucide-react";
import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent, type ReactNode } from "react";
import { api, ApiError, type AskResponse } from "../api/client";
import { ErrorNote, Spinner, useHealth } from "../components/common";
import { AnswerCard } from "./AnswerCard";

export interface Turn {
  id: number;
  question: string;
  response?: AskResponse;
  error?: ApiError;
}

interface Props {
  repo: string;
  turns: Turn[];
  setTurns: (update: (turns: Turn[]) => Turn[]) => void;
  closeButton: ReactNode;
}

let nextTurn = 1;

export function AiPanel({ repo, turns, setTurns, closeButton }: Props) {
  const [draft, setDraft] = useState("");
  const [pending, setPending] = useState(false);
  const scroller = useRef<HTMLDivElement>(null);
  const { data: health } = useHealth();
  const ready = health?.ai.state === "ready";

  // To the top of the newest turn: an answer can be long, and it starts with what matters most.
  useEffect(() => {
    scroller.current?.querySelector(".turn:last-of-type")?.scrollIntoView({ block: "start" });
  }, [turns.length, pending]);

  const submit = async (event?: FormEvent) => {
    event?.preventDefault();
    const question = draft.trim();
    if (!question || pending) return;
    const id = nextTurn++;
    setDraft("");
    setPending(true);
    setTurns((all) => [...all, { id, question }]);
    try {
      const response = await api.ask(repo, question);
      setTurns((all) => all.map((turn) => (turn.id === id ? { ...turn, response } : turn)));
    } catch (error) {
      const failure = error instanceof ApiError ? error : new ApiError(0, "unknown", "The question could not be sent.");
      setTurns((all) => all.map((turn) => (turn.id === id ? { ...turn, error: failure } : turn)));
    } finally {
      setPending(false);
    }
  };

  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && !event.shiftKey) void submit();
    if (event.key === "Enter" && !event.shiftKey) event.preventDefault();
  };

  return (
    <div className="ai-panel">
      <div className="ai-head">
        <h2>
          <FlaskConical size={15} aria-hidden /> Experimental AI Analysis
        </h2>
        {closeButton}
      </div>
      <p className="ai-warning" data-testid="ai-warning">
        Experimental. In our last measurement the model found the right source for 8 of 16 questions and showed a wrong one for 4. Read the code it points at.
      </p>
      {health && !ready ? (
        <div className="note" data-testid="ai-unavailable">
          <p>
            <strong>AI is unavailable.</strong> {health.ai.detail}
          </p>
          <p>Search and the code view keep working, and a question will return the closest lines without a model.</p>
        </div>
      ) : null}
      <div className="ai-conversation" ref={scroller} data-testid="ai-conversation">
        {turns.length === 0 ? (
          <div className="ai-empty muted">
            Ask about this repository in Turkish or English. The answer is the repository’s own lines, each checked against Git. The model only chooses among them, or says it does not know.
          </div>
        ) : null}
        {turns.map((turn) => (
          <article className="turn" key={turn.id}>
            <div className="turn-question">{turn.question}</div>
            {turn.response ? <AnswerCard response={turn.response} /> : null}
            {turn.error ? <ErrorNote error={turn.error} /> : null}
            {!turn.response && !turn.error ? <Spinner label="Reading the code…" /> : null}
          </article>
        ))}
      </div>
      <form className="ai-form" onSubmit={submit}>
        <textarea
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={onKeyDown}
          placeholder="Ask about this repository…"
          aria-label="Ask about this repository"
          rows={2}
          maxLength={1000}
          data-testid="ai-input"
        />
        <button type="submit" className="btn btn-primary btn-icon" disabled={pending || !draft.trim()} aria-label="Ask" title="Ask" data-testid="ai-send">
          <Send size={15} aria-hidden />
        </button>
      </form>
    </div>
  );
}
