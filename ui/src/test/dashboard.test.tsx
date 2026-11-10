import { render, screen, waitFor } from "@testing-library/react";
import { expect, it } from "vitest";
import { ApiClient, TokenStore } from "../api/client";
import { EMPTY_FEATURES } from "../api/features";
import { AppContext, type AppState } from "../context";
import { DashboardView } from "../views/DashboardView";
import { fakeFetch, json } from "./fixtures";

it("requests failure counts for affected clients/services and explains coverage separately", async () => {
  const fetch = fakeFetch({
    "GET /v2/info": () => json({ as_of: "2026-03-10" }),
    "GET /v2/values/service?limit=8": () => json({ values: ["svc-api"] }),
    "POST /v2/tools/log_top": () => json({ tool: "log_top", result: { output: { rows: [], rendering: "No errors" } } }),
    "POST /v2/tools/log_trend": () => json({ tool: "log_trend", result: { output: { rows: [] } } }),
    "POST /v2/tools/log_coverage": () => json({ tool: "log_coverage", result: { output: { rows: [] } } }),
  });
  const state: AppState = { api: new ApiClient(new TokenStore(), "", fetch.impl), settings: { auth: "bearer", mount: "/ui", session_storage_token: false }, capabilities: null, features: { ...EMPTY_FEATURES, tools: new Set(["log_top", "log_trend", "log_coverage"]), routes: new Set(["GET /v2/info"]) }, lastAnswer: null, setLastAnswer: () => {}, lastSearch: null, setLastSearch: () => {}, navigate: () => {} };
  render(<AppContext.Provider value={state}><DashboardView /></AppContext.Provider>);
  await screen.findByText(/Coverage =/);
  await waitFor(() => expect(fetch.calls.filter((c) => String(c.url).endsWith("/log_top"))).toHaveLength(3));
  for (const call of fetch.calls.filter((c) => /\/log_(top|trend)$/.test(String(c.url)))) {
    expect(JSON.parse(String(call.init.body)).level).toEqual(["error", "fatal"]);
  }
  expect(screen.getByText(/reference date does not establish source-data freshness/)).toBeTruthy();
  const coverageCall = fetch.calls.find((c) => String(c.url).endsWith("/log_coverage"));
  expect(JSON.parse(String(coverageCall?.init.body)).window).toBe("last 4 days");
  expect(screen.getByText(/not completion of the running backfill/)).toBeTruthy();
  expect(screen.queryByText(/Latest indexed day/)).toBeNull();
});
