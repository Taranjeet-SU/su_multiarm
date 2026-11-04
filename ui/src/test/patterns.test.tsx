import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ApiClient, TokenStore } from "../api/client";
import { EMPTY_FEATURES } from "../api/features";
import { AppContext, type AppState } from "../context";
import { decisionCommand } from "../lib/cli";
import { PatternsView } from "../views/PatternsView";
import { fakeFetch, json } from "./fixtures";
import { XSS_STRINGS } from "./xss_corpus";

function proposal(id: string, status: string, example: string) {
  return {
    id,
    kind: "noise",
    status,
    owner: "proposed",
    regex: "^metrics flushed in [0-9]{1,4}ms$",
    signature: null,
    category: null,
    precedence: "before_message",
    target: null,
    examples: [example],
    examples_total: 1,
    negative_examples: 0,
    matched_lines: 41,
    gate: { passed: true, engine: "re2", matched: 41, checks: { support: { passed: true }, out_of_cluster: { passed: true, detail: XSS_STRINGS[1] } } },
    provenance: { author: "system:learner" },
    decisions: [],
  };
}

const QUEUE = {
  available: true,
  source: "working_files",
  library: "synthetic-logs",
  file_shas: { "proposed.yaml": "abcdef0123456789" },
  lint_issues: 0,
  kinds: ["message", "noise"],
  proposal_status_counts: { proposed: 2, approved: 1 },
  proposals: [proposal("noise.metrics_flush", "proposed", "metrics flushed in 12ms"), proposal("noise.evil", "proposed", XSS_STRINGS[0]), proposal("noise.done", "approved", "x")],
  learned: [],
  decisions: [{ op: "approve", id: "noise.done", by: "user:alice", at: "2026-01-20T00:00:00Z", reason: XSS_STRINGS[4] }],
};

function renderView() {
  const fetch = fakeFetch({ "GET /v2/patterns/review": () => json(QUEUE), "GET /v2/patterns/view": () => json({ available: true, entries: [], decisions: [] }) });
  const api = new ApiClient(new TokenStore(), "", fetch.impl);
  const state: AppState = { api, settings: { auth: "bearer", mount: "/ui", session_storage_token: false }, capabilities: null, features: EMPTY_FEATURES, lastAnswer: null, setLastAnswer: () => undefined, lastSearch: null, setLastSearch: () => undefined, navigate: () => undefined };
  return render(
    <AppContext.Provider value={state}>
      <PatternsView />
    </AppContext.Provider>,
  );
}

describe("pattern review view", () => {
  it("lists pending noise proposals with examples, matched lines and gate, rendered inert", async () => {
    const { container } = renderView();
    await waitFor(() => expect(screen.getAllByTestId("proposal").length).toBe(2)); // status filter: proposed
    expect(screen.getByTestId("review-count").textContent).toBe("2 of 3 proposals shown");
    expect(container.textContent).toContain("metrics flushed in 12ms");
    expect(container.textContent).toContain(XSS_STRINGS[0]);
    expect(container.querySelectorAll("script, img, iframe").length).toBe(0);
    expect(container.textContent).toContain("41");
  });

  it("copy buttons produce the shlex-quoted command and show it when the clipboard is unavailable", async () => {
    renderView();
    await waitFor(() => expect(screen.getAllByTestId("proposal").length).toBe(2));
    fireEvent.change(screen.getByLabelText("config path"), { target: { value: "/srv/logs config.yaml" } });
    fireEvent.change(screen.getByLabelText("your name (--by)"), { target: { value: "alice" } });
    const card = screen.getAllByTestId("proposal")[0];
    fireEvent.change(card.querySelector("input")!, { target: { value: "benign; it's noise" } });
    fireEvent.click(card.querySelector('[data-testid="copy-deprecate"]')!);
    await waitFor(() => expect(screen.getByTestId("copied-command")).toBeTruthy());
    const expected = decisionCommand({ config: "/srv/logs config.yaml", op: "deprecate", id: "noise.metrics_flush", by: "alice", reason: "benign; it's noise" });
    expect(screen.getByTestId("copied-command").querySelector("pre")!.textContent).toBe(expected);
    expect(expected).toBe("su-multi-arm '--config=/srv/logs config.yaml' patterns deprecate --by=alice '--reason=benign; it'\"'\"'s noise' -- noise.metrics_flush");
  });
});
