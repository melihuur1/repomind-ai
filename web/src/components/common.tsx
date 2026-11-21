import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, Check, Copy, Loader2, Moon, Sparkles, Sun } from "lucide-react";
import { useState, type ReactNode } from "react";
import { api, ApiError } from "../api/client";
import { currentTheme, setTheme, type Theme } from "../lib/theme";

export function Spinner({ label }: { label?: string }) {
  return (
    <span className="spinner" role="status">
      <Loader2 size={14} className="spin" aria-hidden />
      {label ?? "Loading…"}
    </span>
  );
}

export function ErrorNote({ error, children }: { error: unknown; children?: ReactNode }) {
  const message = error instanceof ApiError ? error.message : "Something went wrong.";
  return (
    <div className="note note-error" role="alert">
      <AlertTriangle size={15} aria-hidden />
      <div>
        {message}
        {children}
      </div>
    </div>
  );
}

export function EmptyNote({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="empty">
      <strong>{title}</strong>
      {children ? <div className="muted">{children}</div> : null}
    </div>
  );
}

export function CopyButton({ text, label, title }: { text: string; label?: string; title: string }) {
  const [done, setDone] = useState(false);
  return (
    <button
      type="button"
      className="btn btn-ghost"
      title={title}
      aria-label={title}
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(text);
          setDone(true);
          window.setTimeout(() => setDone(false), 1500);
        } catch {
          // Clipboard access can be refused. The text is still on screen.
        }
      }}
    >
      {done ? <Check size={14} aria-hidden /> : <Copy size={14} aria-hidden />}
      {label}
    </button>
  );
}

export function ThemeToggle() {
  const [theme, setThemeState] = useState<Theme>(currentTheme);
  const next = theme === "dark" ? "light" : "dark";
  return (
    <button
      type="button"
      className="btn btn-ghost btn-icon"
      title={`Switch to the ${next} theme`}
      aria-label={`Switch to the ${next} theme`}
      onClick={() => {
        setTheme(next);
        setThemeState(next);
      }}
    >
      {theme === "dark" ? <Sun size={15} aria-hidden /> : <Moon size={15} aria-hidden />}
    </button>
  );
}

const AI_LABELS = {
  ready: "AI ready",
  offline: "AI offline",
  model_missing: "AI model missing",
  not_configured: "AI not set up",
  low_memory: "AI paused",
  // Labels only. How these states are shown is the next step.
  misconfigured: "AI settings invalid",
  auth_failed: "AI key refused",
  budget_reached: "AI budget spent",
  limited: "AI limited",
  rejected: "AI request refused",
} as const;

export function useHealth() {
  return useQuery({ queryKey: ["health"], queryFn: api.health, refetchInterval: 15_000, staleTime: 5_000 });
}

export function AiStatusPill() {
  const { data, isError } = useHealth();
  if (isError) {
    return (
      <span className="pill pill-danger" title="The RepoMind server cannot be reached.">
        <span className="dot" aria-hidden /> Server offline
      </span>
    );
  }
  if (!data) return null;
  const ready = data.ai.state === "ready";
  return (
    <span className={`pill ${ready ? "pill-ok" : "pill-muted"}`} title={data.ai.detail} data-testid="ai-status">
      <Sparkles size={12} aria-hidden />
      {AI_LABELS[data.ai.state]}
      {ready && data.ai.model ? <span className="pill-detail">{data.ai.model}</span> : null}
      {ready && data.ai.remote ? (
        <span className="pill-detail" data-testid="ai-remote-pill">
          remote
        </span>
      ) : null}
    </span>
  );
}
