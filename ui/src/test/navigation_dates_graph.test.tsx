import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ApiClient, TokenStore } from "../api/client";
import { EMPTY_FEATURES } from "../api/features";
import { AppContext, type AppState } from "../context";
import { todayDate } from "../lib/dates";
import { AskView } from "../views/AskView";
import { GraphView } from "../views/GraphView";
import { VIEWS } from "../views/registry";
import { fakeFetch, json, sse, answerResult } from "./fixtures";

vi.mock("../components/GraphCanvas", () => ({ GraphCanvas: ({ data }: { data: { nodes: unknown[]; edges: unknown[] } }) => <div data-testid="stored-graph">{data.nodes.length} nodes, {data.edges.length} edges</div> }));

function state(fetch: ReturnType<typeof fakeFetch>, routes: string[] = []): AppState {
  return { api: new ApiClient(new TokenStore(), "", fetch.impl), settings: { auth: "bearer", mount: "/ui", session_storage_token: false }, capabilities: null, features: { ...EMPTY_FEATURES, routes: new Set(routes), routesKnown: true }, lastAnswer: null, setLastAnswer: () => {}, lastSearch: null, setLastSearch: () => {}, navigate: () => {} };
}

describe("focused navigation and dates", () => {
  it("shows only Overview, Ask and Graph when log tools are available", () => {
    const features = { ...EMPTY_FEATURES, tools: new Set(["log_top"]) };
    expect(VIEWS.filter((view) => !view.hidden?.(features, "focused")).map((view) => view.label)).toEqual(["Overview", "Ask", "Graph"]);
  });
  it("preserves standard operational navigation by default", () => {
    const features = { ...EMPTY_FEATURES, tools: new Set(["log_top"]) };
    expect(VIEWS.filter((view) => !view.hidden?.(features)).map((view) => view.id)).toEqual([
      "overview", "ask", "graph", "charts", "logs", "patterns", "communities", "runs", "settings",
    ]);
  });
  it("uses the local calendar day and preserves an explicit linked date", async () => {
    expect(todayDate(new Date(2026, 2, 8, 23, 59))).toBe("2026-03-08");
    const fetch = fakeFetch({ "POST /v2/answer/stream": () => sse([{ event: "done", result: answerResult() }]) });
    const view = render(<AppContext.Provider value={state(fetch)}><AskView initialQuestion="What needs attention?" /></AppContext.Provider>);
    expect((screen.getByLabelText("As of") as HTMLInputElement).value).toBe(todayDate());
    fireEvent.click(screen.getByRole("button", { name: "Ask" }));
    await waitFor(() => expect(fetch.calls).toHaveLength(1));
    expect(JSON.parse(String(fetch.calls[0].init.body)).as_of).toBe(todayDate());
    view.rerender(<AppContext.Provider value={state(fetch)}><AskView initialAsOf="2026-03-01" /></AppContext.Provider>);
    expect((screen.getByLabelText("As of") as HTMLInputElement).value).toBe("2026-03-01");
  });
});

describe("stored graph on opening the tab", () => {
  it("loads actual service values and their native relationships without a search click", async () => {
    const fetch = fakeFetch({
      "GET /v2/values/service?limit=8": () => json({ values: ["svc-api"] }),
      "POST /v2/graph/neighbourhood": () => json({ nodes: [{ id: "a", name: "svc-api", type: "service" }, { id: "b", name: "timeout", type: "signature" }], edges: [{ id: "ab", source: "a", target: "b", predicate: "emits", edge_kind: "native_link", support: "unchecked" }], truncated: false }),
    });
    render(<AppContext.Provider value={state(fetch, ["POST /v2/graph/neighbourhood"])}><GraphView /></AppContext.Provider>);
    await waitFor(() => expect(screen.getByTestId("stored-graph").textContent).toBe("2 nodes, 1 edges"));
    expect(JSON.parse(String(fetch.calls[1].init.body))).toMatchObject({ query: "svc-api", max_nodes: 300, max_edges: 1000 });
    expect(screen.getByText(/initially around up to eight indexed services/)).toBeTruthy();
  });
  it("reports an unavailable service catalogue instead of drawing example data", async () => {
    const fetch = fakeFetch({ "GET /v2/values/service?limit=8": () => new Response("{}", { status: 503 }) });
    render(<AppContext.Provider value={state(fetch, ["POST /v2/graph/neighbourhood"])}><GraphView /></AppContext.Provider>);
    await screen.findByRole("alert");
    expect(screen.queryByTestId("stored-graph")).toBeNull();
    expect(fetch.calls).toHaveLength(1);
  });
});
