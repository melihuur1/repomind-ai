const STORAGE_KEY = "repomind:theme";

export type Theme = "light" | "dark";

function stored(): Theme | null {
  try {
    const value = localStorage.getItem(STORAGE_KEY);
    return value === "light" || value === "dark" ? value : null;
  } catch {
    return null;
  }
}

export function currentTheme(): Theme {
  return stored() ?? (window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark");
}

export function applyStoredTheme(): void {
  document.documentElement.dataset.theme = currentTheme();
}

export function setTheme(theme: Theme): void {
  document.documentElement.dataset.theme = theme;
  try {
    localStorage.setItem(STORAGE_KEY, theme);
  } catch {
    // Private windows may refuse. The choice still lasts for this page.
  }
}

export function readNumber(key: string, fallback: number): number {
  try {
    const value = Number(localStorage.getItem(key));
    return Number.isFinite(value) && value > 0 ? value : fallback;
  } catch {
    return fallback;
  }
}

export function writeNumber(key: string, value: number): void {
  try {
    localStorage.setItem(key, String(value));
  } catch {
    // Only a convenience.
  }
}
