import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ApiClient, TokenStore } from "../api/client";
import { EMPTY_FEATURES, type Features } from "../api/features";
import { AppContext, type AppState } from "../context";
import { formFields, initialValues, toArgs } from "../lib/schemaForm";
import { asAggregate, hasLogTools, LogsView } from "../views/LogsView";
import { fakeFetch, json } from "./fixtures";

// The shape pydantic emits for a LogTopInput-like model (synthetic; the real one comes from GET /v2/tools).
const TOP_SCHEMA = {
  type: "object",
  properties: {
    window: { anyOf: [{ type: "string" }, { type: "null" }], default: null },
    by: { enum: ["service", "signature", "tenant"], type: "string" },
    level: { anyOf: [{ type: "array", items: { type: "string" } }, { type: "null" }], default: null },
    top_n: { type: "integer", default: 10 },
    scope: { $ref: "#/$defs/Nested" },
  },
  required: ["by"],
  $defs: { Nested: { type: "object", properties: {} } },
};

const OUTPUT = {
  scope: { group_by: ["service"] },
  result: { exactness: "approximate", denominator: 120, basis: ["lines containing 'error' (token match)"], groups: [{ key: { service: "gateway" }, value: 80, count: 80 }, { key: { service: "scheduler" }, value: 40, count: 40 }], total: 120 },
  rendering: "lines containing 'error' on index date 2026-01-19 (index_date, cut in UTC), approximate (hourly quota)",
  document_ids: ["a"],
  truncated: true,
};

describe("schema-driven forms", () => {
  it("derives fields from the input JSON Schema and converts values without coercion", () => {
    const fields = formFields(TOP_SCHEMA);
    expect(fields.map((f) => [f.name, f.kind, f.required])).toEqual([
      ["window", "string", false],
      ["by", "enum", true],
      ["level", "string_list", false],
      ["top_n", "integer", false],
      ["scope", "json", false],
    ]);
    const values = initialValues(fields, { by: "service", window: "yesterday" });
    expect(values.top_n).toBe("10");
    expect(toArgs(fields, { ...values, level: "ERROR, FATAL" })).toEqual({ window: "yesterday", by: "service", level: ["ERROR", "FATAL"], top_n: 10 });
    expect(() => toArgs(fields, { ...values, top_n: "ten" })).toThrow("top_n must be an integer");
    expect(() => toArgs(fields, { ...values, by: "" })).toThrow("by is required");
  });
});

describe("log views", () => {
  it("are hidden without log tools", () => {
    expect(hasLogTools(EMPTY_FEATURES)).toBe(false);
    expect(hasLogTools({ ...EMPTY_FEATURES, tools: new Set(["search", "log_top"]) })).toBe(true);
  });

  it("maps a tool output onto the aggregate card shape with the server's rendering", () => {
    const agg = asAggregate("log_top", OUTPUT)!;
    expect(agg.exactness).toBe("approximate");
    expect(agg.rendering).toBe(OUTPUT.rendering);
    expect(agg.document_ids_truncated).toBe(true);
    expect(agg.field).toBe("service");
  });

  it("shows overview tiles only for the advertised tools and runs a shortcut", async () => {
    const fetch = fakeFetch({ "POST /v2/tools/log_top": () => json({ tool: "log_top", result: { output: OUTPUT, evidence_ids: [] }, trace: {} }) });
    const features: Features = { ...EMPTY_FEATURES, tools: new Set(["log_top"]), toolSchemas: new Map([["log_top", TOP_SCHEMA]]) };
    const state: AppState = { api: new ApiClient(new TokenStore(), "", fetch.impl), settings: { auth: "bearer", mount: "/ui", session_storage_token: false }, capabilities: null, features, lastAnswer: null, setLastAnswer: () => undefined, lastSearch: null, setLastSearch: () => undefined, navigate: () => undefined };
    render(
      <AppContext.Provider value={state}>
        <LogsView />
      </AppContext.Provider>,
    );
    await waitFor(() => expect(screen.getByTestId("tile-log_top").textContent).toContain("approximate (hourly quota)"));
    expect(screen.queryByTestId("tile-log_new")).toBeNull();
    expect(screen.queryByTestId("shortcut-log_trend")).toBeNull();
    const shortcut = screen.getByTestId("shortcut-log_top");
    fireEvent.click(shortcut.querySelector('button[type="submit"]')!);
    await waitFor(() => expect(shortcut.querySelector('[data-testid="aggregate-rendering"]')).toBeTruthy());
    const posted = fetch.calls.filter((c) => c.url === "/v2/tools/log_top").map((c) => JSON.parse(String(c.init.body)));
    expect(posted).toContainEqual({ window: "yesterday", by: "service", top_n: 10 });
  });
});
