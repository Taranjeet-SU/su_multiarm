import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it } from "vitest";
import { ApiClient, TokenStore } from "../api/client";
import { EMPTY_FEATURES, type Features } from "../api/features";
import { AppContext, type AppState } from "../context";
import { submitOnEnter } from "../lib/keys";
import { LogsView } from "../views/LogsView";
import { PatternsView } from "../views/PatternsView";
import { fakeFetch, json } from "./fixtures";

// The Ask field's wiring (a textarea in a form with onSubmit), isolated from the streaming view.
function Harness({ onSubmit }: { onSubmit: () => void }) {
  const [text, setText] = useState("");
  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        onSubmit();
      }}
    >
      <textarea aria-label="question" value={text} onChange={(e) => setText(e.target.value)} onKeyDown={submitOnEnter} />
      <input aria-label="search" onKeyDown={submitOnEnter} />
      <button type="submit">Ask</button>
    </form>
  );
}

function state(fetch: ReturnType<typeof fakeFetch>, features: Features = EMPTY_FEATURES): AppState {
  return { api: new ApiClient(new TokenStore(), "", fetch.impl), settings: { auth: "bearer", mount: "/ui", session_storage_token: false }, capabilities: null, features, lastAnswer: null, setLastAnswer: () => undefined, lastSearch: null, setLastSearch: () => undefined, navigate: () => undefined };
}

describe("Enter submits like the button", () => {
  it("submits on Enter, keeps Shift+Enter for a newline and never submits during IME composition", () => {
    let submitted = 0;
    render(<Harness onSubmit={() => (submitted += 1)} />);
    const question = screen.getByLabelText("question");
    fireEvent.keyDown(question, { key: "Enter" });
    expect(submitted).toBe(1);
    const shifted = fireEvent.keyDown(question, { key: "Enter", shiftKey: true });
    expect(submitted).toBe(1);
    expect(shifted).toBe(true); // default not prevented: the textarea inserts the newline
    fireEvent.keyDown(question, { key: "Enter", isComposing: true });
    fireEvent.keyDown(question, { key: "Enter", keyCode: 229 });
    expect(submitted).toBe(1);
    fireEvent.keyDown(question, { key: "a" });
    expect(submitted).toBe(1);
    fireEvent.keyDown(screen.getByLabelText("search"), { key: "Enter" });
    expect(submitted).toBe(2);
  });

  it("runs a Logs shortcut on Enter in one of its fields", async () => {
    const schema = { type: "object", properties: { window: { anyOf: [{ type: "string" }, { type: "null" }], default: null }, by: { enum: ["service", "tenant"], type: "string" } }, required: ["by"] };
    const output = { scope: { group_by: ["service"] }, result: { exactness: "exact", denominator: 1, basis: [], groups: [], total: 0 }, rendering: "synthetic rendering", document_ids: [], truncated: false };
    const fetch = fakeFetch({ "POST /v2/tools/log_top": () => json({ tool: "log_top", result: { output, evidence_ids: [] }, trace: {} }) });
    const features: Features = { ...EMPTY_FEATURES, tools: new Set(["log_top"]), toolSchemas: new Map([["log_top", schema]]) };
    render(
      <AppContext.Provider value={state(fetch, features)}>
        <LogsView />
      </AppContext.Provider>,
    );
    const shortcut = await screen.findByTestId("shortcut-log_top");
    const before = fetch.calls.filter((c) => c.url === "/v2/tools/log_top").length;
    const field = shortcut.querySelector("input")!;
    fireEvent.change(field, { target: { value: "last 7 days" } });
    fireEvent.keyDown(field, { key: "Enter", isComposing: true });
    expect(fetch.calls.filter((c) => c.url === "/v2/tools/log_top").length).toBe(before);
    fireEvent.keyDown(field, { key: "Enter" });
    await waitFor(() => expect(fetch.calls.filter((c) => c.url === "/v2/tools/log_top").length).toBe(before + 1));
    const posted = fetch.calls.filter((c) => c.url === "/v2/tools/log_top").map((c) => JSON.parse(String(c.init.body)));
    expect(posted[posted.length - 1]).toMatchObject({ window: "last 7 days" });
  });

  it("reloads the Patterns review on Enter in a filter field, like the Reload button", async () => {
    const queue = { available: true, source: "working_files", library: "synthetic", file_shas: {}, lint_issues: 0, kinds: [], proposal_status_counts: {}, proposals: [], learned: [], decisions: [] };
    const fetch = fakeFetch({ "GET /v2/patterns/review": () => json(queue), "GET /v2/patterns/view": () => json({ available: true, entries: [], decisions: [] }) });
    render(
      <AppContext.Provider value={state(fetch)}>
        <PatternsView />
      </AppContext.Provider>,
    );
    const field = await screen.findByLabelText("config path");
    const reviews = () => fetch.calls.filter((c) => c.url.startsWith("/v2/patterns/review")).length;
    const before = reviews();
    fireEvent.keyDown(field, { key: "Enter" });
    await waitFor(() => expect(reviews()).toBe(before + 1));
    fireEvent.click(screen.getByText("Reload"));
    await waitFor(() => expect(reviews()).toBe(before + 2));
  });
});
