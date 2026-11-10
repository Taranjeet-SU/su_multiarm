import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ApiClient, TokenStore } from "../api/client";
import { EMPTY_FEATURES } from "../api/features";
import { AppContext, type AppState } from "../context";
import { clientId, issueGraph, issueId, rowsForSelection, serviceId, type IssueRow } from "../lib/clientIssueGraph";
import { GraphView } from "../views/GraphView";
import { fakeFetch, json } from "./fixtures";

vi.mock("../components/GraphCanvas", () => ({ GraphCanvas: ({ data, onSelectNode }: { data: ReturnType<typeof issueGraph>; onSelectNode: (id: string) => void }) => <div data-testid="issue-graph">{data.nodes.map((node) => <button key={node.id} onClick={() => onSelectNode(node.id)}>{node.label}</button>)}</div> }));
const row = (tenant: string, service: string, signature_id: string, error_count: number, level: "ERROR" | "FATAL" = "ERROR"): IssueRow => ({ tenant, service, signature_id, error_count, level, error_type: signature_id, supporting_template: "Synthetic timeout <id>", evidence_id: `pattern:${signature_id}` });
const rows = [row("alpha", "api", "timeout", 4), row("beta", "api", "timeout", 7), row("beta", "worker", "memory", 2, "FATAL")];
const output = { status: "bounded", rows, rendering: "Classified events from the stored window.", exactness: "bounded", source_unavailable: ["2026-03-08"], truncated: true };

function state(fetch: ReturnType<typeof fakeFetch>): AppState {
  return { api: new ApiClient(new TokenStore(), "", fetch.impl), settings: { auth: "bearer", mount: "/ui", session_storage_token: false }, capabilities: null, features: { ...EMPTY_FEATURES, tools: new Set(["log_top"]) }, lastAnswer: null, setLastAnswer: () => {}, lastSearch: null, setLastSearch: () => {}, navigate: () => {} };
}

describe("client/service issue graph associations", () => {
  it("shares issue nodes but scopes same-name services to each client and sums only observed rows", () => {
    const graph = issueGraph(rows, true);
    expect(graph.nodes.filter((node) => node.type === "service")).toHaveLength(3);
    expect(graph.nodes.filter((node) => node.type === "issue")).toHaveLength(2);
    const shared = graph.edges.filter((edge) => edge.source === issueId(rows[0]));
    expect(shared).toHaveLength(1);
    expect(shared[0].predicate).toBe("11 errors");
    expect(rowsForSelection(rows, clientId(rows[0]), graph)).toEqual([rows[0]]);
    expect(rowsForSelection(rows, serviceId(rows[1]), graph)).toEqual([rows[1]]);
    const connection = graph.edges.find((edge) => edge.source === serviceId(rows[0]))!;
    expect(rowsForSelection(rows, connection.id, graph)).toEqual([rows[0]]);
    expect(graph.edges.every((edge) => edge.edgeKind === "derived" && edge.quotes.length === 0)).toBe(true);
  });
  it("does not invent a fatal path for an error-only service sharing a signature", () => {
    const fatal = row("beta", "api", "timeout", 3, "FATAL");
    const graph = issueGraph([rows[0], fatal], false);
    expect(issueId(rows[0])).not.toBe(issueId(fatal));
    expect(graph.edges.some((edge) => edge.source === serviceId(rows[0]) && edge.target === issueId(fatal))).toBe(false);
  });
});

describe("client issue graph view", () => {
  it("loads typed grouped counts, presents limits and evidence, and applies selected client on the server", async () => {
    const fetch = fakeFetch({
      "GET /v2/info": () => json({ as_of: "2026-03-10" }),
      "GET /v2/values/tenant?limit=50": () => json({ values: ["alpha", "beta"] }),
      "POST /v2/tools/log_top": () => json({ result: { output } }),
    });
    render(<AppContext.Provider value={state(fetch)}><GraphView /></AppContext.Provider>);
    await screen.findByTestId("issue-graph");
    const request = fetch.calls.find((call) => call.url.endsWith("/v2/tools/log_top"))!;
    expect(JSON.parse(String(request.init.body))).toMatchObject({ as_of: "2026-03-10", window: "last 3 days", group_by: ["tenant", "service", "signature_id", "level"], level: ["error", "fatal"], top_n: 30 });
    expect(screen.getByText(/Missing data for 2026-03-08/)).toBeTruthy();
    expect(screen.getByText(/More combinations exist/)).toBeTruthy();
    expect(screen.getByText(/not monitored alerts or confirmed outages/)).toBeTruthy();
    expect(screen.getAllByText(/Stored pattern template, not a raw event quote/)).toHaveLength(3);
    fireEvent.click(screen.getByRole("button", { name: "alpha" }));
    expect(screen.getAllByText("Pattern & evidence")).toHaveLength(1);
    fireEvent.change(screen.getByLabelText("Client"), { target: { value: "beta" } });
    fireEvent.click(screen.getByRole("button", { name: "Apply client" }));
    await waitFor(() => expect(fetch.calls.filter((call) => call.url.endsWith("/v2/tools/log_top"))).toHaveLength(2));
    expect(JSON.parse(String(fetch.calls.at(-1)!.init.body)).tenant).toBe("beta");
  });
  it("shows a visible failure and retry rather than a blank graph", async () => {
    let attempts = 0;
    const fetch = fakeFetch({
      "GET /v2/info": () => json({ as_of: "2026-03-10" }),
      "GET /v2/values/tenant?limit=50": () => json({ values: [] }),
      "POST /v2/tools/log_top": () => ++attempts === 1 ? new Response("{}", { status: 503 }) : json({ result: { output } }),
    });
    render(<AppContext.Provider value={state(fetch)}><GraphView /></AppContext.Provider>);
    await screen.findByRole("alert");
    expect(screen.queryByTestId("issue-graph")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    await screen.findByTestId("issue-graph");
  });
});
