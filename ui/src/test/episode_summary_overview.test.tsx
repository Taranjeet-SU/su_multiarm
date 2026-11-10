import { render, screen, waitFor } from "@testing-library/react";
import { expect, it } from "vitest";
import { ApiClient, TokenStore } from "../api/client";
import { EMPTY_FEATURES } from "../api/features";
import { AppContext, type AppState } from "../context";
import { DashboardView } from "../views/DashboardView";
import { todayDate } from "../lib/dates";
import { fakeFetch, json } from "./fixtures";

it("shows source counts, bounded evidence and unknown recovery without parser-coverage claims", async () => {
  const fetch = fakeFetch({
    "POST /v2/tools/incident_episodes": () => json({ result: { output: {
      rows: [{ id: "synthetic", tenant: "client-a", service: "worker", issue: "Redis connection refused", first_failure: "2026-01-01T10:00:00Z", last_failure: "2026-01-01T10:01:59Z", occurrences: 10000, observed_window_seconds: 119, resolution_seconds: null, evidence: "<script>unsafe()</script>", evidence_count: 1 }],
      statistics: { keyword_documents: 10001, evidence_documents: 2, rule_documents: 10000, unclassified_documents: 1, episodes_total: 1, source_scopes: 1, document_download_reduction_percent: 99.98 },
      notes: [], rendering: "Observed signals only", source_unavailable: [], truncated: false,
    } } }),
  });
  const state: AppState = { api: new ApiClient(new TokenStore(), "", fetch.impl), settings: { auth: "none", mount: "/ui", session_storage_token: false }, capabilities: null, features: { ...EMPTY_FEATURES, tools: new Set(["incident_episodes"]), routes: new Set() }, lastAnswer: null, setLastAnswer: () => {}, lastSearch: null, setLastSearch: () => {}, navigate: () => {} };
  const view = render(<AppContext.Provider value={state}><DashboardView /></AppContext.Provider>);
  await screen.findByText("Redis connection refused");
  expect(screen.getByText("Unknown")).toBeTruthy();
  expect(screen.getByText("99.98%")).toBeTruthy();
  expect(screen.getByText(/unclassified candidates are not confirmed incidents/i)).toBeTruthy();
  expect(view.container.querySelector("script")).toBeNull();
  expect(screen.queryByText(/Coverage =/)).toBeNull();
  await waitFor(() => expect(fetch.calls.length).toBe(1));
  expect(JSON.parse(String(fetch.calls[0].init.body)).as_of).toBe(todayDate());
  expect(String(fetch.calls[0].url)).toContain("incident_episodes");
});
