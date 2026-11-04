import { useCallback, useEffect, useMemo, useState } from "react";
import { ApiClient, ApiError, TokenStore } from "./api/client";
import { EMPTY_FEATURES, loadFeatures, type Features } from "./api/features";
import { Capabilities, UiSettings, type AnswerResult, type SearchResult } from "./api/schema";
import { ErrorBanner, NotAvailable } from "./components/common";
import { AppContext, type AppState } from "./context";
import { displayText } from "./lib/text";
import { VIEWS } from "./views/registry";
import { TokenForm } from "./views/SettingsView";

/** Hash routes (``#/ask?q=...``): the server serves one static ``index.html``; no SPA fallback route. */
export function parseHash(hash: string): { route: string; params: Record<string, string> } {
  const raw = hash.replace(/^#\/?/, "");
  const [path, query = ""] = raw.split("?", 2);
  const params: Record<string, string> = {};
  new URLSearchParams(query).forEach((v, k) => (params[k] = v));
  return { route: path || "ask", params };
}

function useHashRoute() {
  const [state, setState] = useState(() => parseHash(window.location.hash));
  useEffect(() => {
    const on = () => setState(parseHash(window.location.hash));
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  return state;
}

type Boot = { phase: "loading" } | { phase: "error"; error: unknown } | { phase: "ready"; settings: UiSettings };

export function App({ api: injected }: { api?: ApiClient }) {
  const api = useMemo(() => injected ?? new ApiClient(new TokenStore()), [injected]);
  const [boot, setBoot] = useState<Boot>({ phase: "loading" });
  const [capabilities, setCapabilities] = useState<Capabilities | null>(null);
  const [features, setFeatures] = useState<Features>(EMPTY_FEATURES);
  const [loadError, setLoadError] = useState<unknown>(null);
  const [lastAnswer, setLastAnswer] = useState<AnswerResult | null>(null);
  const [lastSearch, setLastSearch] = useState<SearchResult | null>(null);
  const [tokenVersion, setTokenVersion] = useState(0);
  const { route, params } = useHashRoute();

  useEffect(() => api.tokens.subscribe(() => setTokenVersion((v) => v + 1)), [api]);

  useEffect(() => {
    const abort = new AbortController();
    // ``ui-config.json`` is served next to index.html by the mount (no secrets: auth kind, mount, storage policy).
    api
      .get("./ui-config.json", UiSettings, abort.signal)
      .then((settings) => {
        api.tokens.configure(settings.session_storage_token);
        setBoot({ phase: "ready", settings });
      })
      .catch((error) => !abort.signal.aborted && setBoot({ phase: "error", error }));
    return () => abort.abort();
  }, [api]);

  useEffect(() => {
    if (boot.phase !== "ready") return;
    const abort = new AbortController();
    setLoadError(null);
    Promise.all([api.get("/v2/capabilities", Capabilities, abort.signal), loadFeatures(api, abort.signal)])
      .then(([caps, feats]) => {
        setCapabilities(caps);
        setFeatures(feats);
      })
      .catch((error) => {
        if (abort.signal.aborted) return;
        setCapabilities(null);
        setFeatures(EMPTY_FEATURES);
        setLoadError(error);
      });
    return () => abort.abort();
  }, [api, boot.phase, tokenVersion]);

  const navigate = useCallback((to: string, p?: Record<string, string>) => {
    const query = p ? new URLSearchParams(p).toString() : "";
    window.location.hash = `#/${to}${query ? `?${query}` : ""}`;
  }, []);

  if (boot.phase === "loading") return <p className="boot">Loading…</p>;
  if (boot.phase === "error") {
    return (
      <main className="boot">
        <h1>su_multi_arm</h1>
        <ErrorBanner error={boot.error} />
      </main>
    );
  }
  const state: AppState = { api, settings: boot.settings, capabilities, features, lastAnswer, setLastAnswer, lastSearch, setLastSearch, navigate };
  const view = VIEWS.find((v) => v.id === route) ?? null;
  const needsToken = loadError instanceof ApiError && loadError.kind === "unauthenticated";
  const dataset = capabilities?.datasets[0];
  return (
    <AppContext.Provider value={state}>
      <header className="topbar">
        <strong>su_multi_arm</strong>
        <nav aria-label="views">
          {VIEWS.filter((v) => !v.hidden?.(features)).map((v) => {
            const why = v.unavailable?.(features) ?? null;
            return (
              <a key={v.id} href={`#/${v.id}`} className={v.id === route ? "active" : why ? "unavailable" : undefined} aria-current={v.id === route ? "page" : undefined} title={why ?? undefined}>
                {v.label}
                {why ? " (n/a)" : ""}
              </a>
            );
          })}
        </nav>
        <span className="muted small" data-testid="dataset">
          {dataset ? `${displayText(dataset.name)} · generation ${displayText(dataset.generation ?? "none")}` : ""}
        </span>
      </header>
      <main>
        {needsToken ? (
          <section className="banner banner-error" role="alert">
            <strong>This server requires a bearer token.</strong>
            <TokenForm />
          </section>
        ) : (
          <ErrorBanner error={loadError} onRetry={() => setTokenVersion((v) => v + 1)} />
        )}
        {view ? (
          view.unavailable?.(features) ? (
            <NotAvailable what={view.label} why={view.unavailable(features) ?? ""} />
          ) : (
            view.render(params)
          )
        ) : (
          <p>Unknown view.</p>
        )}
      </main>
    </AppContext.Provider>
  );
}
