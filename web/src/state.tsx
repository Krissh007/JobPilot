// App-wide UI state: tab, filters, open drawer, toasts, theme. Remembered per browser where harmless.
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import type { Stage } from "./api";

export const TABS = [
  ["board", "Board"], ["jobs", "Jobs"], ["review", "Review"], ["runs", "Runs"], ["answers", "Answers"],
  ["analytics", "Analytics"], ["database", "Database"], ["settings", "Settings"],
] as const;
export type Tab = (typeof TABS)[number][0];
export type SourceFilter = "all" | "linkedin" | "naukri";
export type Theme = "system" | "light" | "dark";

interface Filters { stage: Stage | null; q: string; source: SourceFilter; aboveThreshold: boolean; showExamples: boolean }

interface UI {
  tab: Tab; setTab: (t: Tab) => void;
  filters: Filters; setFilters: (patch: Partial<Filters>) => void;
  drawer: number | "new" | null; openJob: (id: number | "new" | null) => void;
  toast: (message: string) => void;
  theme: Theme; setTheme: (t: Theme) => void;
}

const Ctx = createContext<UI | null>(null);

function remembered<T>(key: string, fallback: T): T {
  try { const v = localStorage.getItem(key); return v ? (JSON.parse(v) as T) : fallback; } catch { return fallback; }
}
function remember(key: string, value: unknown) {
  try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* storage unavailable */ }
}

export function UIProvider({ children }: { children: ReactNode }) {
  const [tab, setTabState] = useState<Tab>(() => remembered("jp.tab", "board"));
  const [filters, setFilterState] = useState<Filters>(() => ({
    stage: null, q: "", source: "all", aboveThreshold: false, showExamples: remembered("jp.examples", true),
  }));
  const [drawer, setDrawer] = useState<number | "new" | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [theme, setThemeState] = useState<Theme>(() => {
    try { const t = localStorage.getItem("jp.theme"); return t === "light" || t === "dark" ? t : "system"; } catch { return "system"; }
  });
  const timer = useRef<number | undefined>(undefined);

  const setTab = useCallback((t: Tab) => { setTabState(t); remember("jp.tab", t); }, []);
  const setFilters = useCallback((patch: Partial<Filters>) => {
    setFilterState((f) => {
      const next = { ...f, ...patch };
      if (patch.showExamples !== undefined) remember("jp.examples", next.showExamples);
      return next;
    });
  }, []);
  const toast = useCallback((m: string) => {
    setMessage(m);
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => setMessage(null), 2800);
  }, []);
  const setTheme = useCallback((t: Theme) => {
    setThemeState(t);
    try { if (t === "system") localStorage.removeItem("jp.theme"); else localStorage.setItem("jp.theme", t); } catch { /* ignore */ }
  }, []);

  useEffect(() => {
    if (theme === "system") delete document.documentElement.dataset.theme;
    else document.documentElement.dataset.theme = theme;
  }, [theme]);

  const value = useMemo(() => ({ tab, setTab, filters, setFilters, drawer, openJob: setDrawer, toast, theme, setTheme }),
    [tab, setTab, filters, setFilters, drawer, toast, theme, setTheme]);

  return (
    <Ctx.Provider value={value}>
      {children}
      {message && (
        <div role="status" className="fixed bottom-5 left-1/2 z-50 max-w-[calc(100%-32px)] -translate-x-1/2 rounded-md bg-fg px-4 py-2 text-[13px] text-bg shadow-lg">
          {message}
        </div>
      )}
    </Ctx.Provider>
  );
}

export function useUI(): UI {
  const ui = useContext(Ctx);
  if (!ui) throw new Error("useUI outside UIProvider");
  return ui;
}
