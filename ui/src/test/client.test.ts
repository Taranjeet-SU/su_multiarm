import { describe, expect, it } from "vitest";
import { ApiClient, ApiError, TokenStore } from "../api/client";
import { loadFeatures } from "../api/features";
import { SearchResult, type StreamEvent } from "../api/schema";
import { SseParser } from "../api/sse";
import { answerResult, fakeFetch, json, searchResult, sse } from "./fixtures";

describe("SSE parser", () => {
  it("joins messages split at arbitrary boundaries and ignores heartbeats", () => {
    const parser = new SseParser();
    const text = ': heartbeat\n\nid: 1\nevent: token\ndata: {"a":1}\n\n: heartbeat\n\nevent: done\r\ndata: {"b":\r\ndata: 2}\r\n\r\n';
    const out = [];
    for (let i = 0; i < text.length; i += 3) out.push(...parser.push(text.slice(i, i + 3)));
    expect(out).toEqual([
      { id: "1", event: "token", data: '{"a":1}' },
      { id: undefined, event: "done", data: '{"b":\n2}' },
    ]);
    expect(parser.pending).toBe("");
  });
});

describe("ApiClient", () => {
  it("sends the bearer token from memory and maps statuses to typed errors", async () => {
    const tokens = new TokenStore();
    const fetch = fakeFetch({
      "POST /v2/search": (init) => (new Headers(init.headers).get("authorization") === "Bearer t0k" ? json(searchResult()) : json({ detail: "Invalid bearer token" }, 401)),
      "GET /v2/tools": () => json({ detail: "principal lacks the 'tools' scope" }, 403),
      "GET /v2/capabilities": () => json({ detail: "Search backend unavailable; check server logs" }, 503),
    });
    const api = new ApiClient(tokens, "", fetch.impl);
    await expect(api.post("/v2/search", { query: "x" }, SearchResult)).rejects.toMatchObject({ kind: "unauthenticated", status: 401 });
    tokens.set("t0k");
    expect((await api.post("/v2/search", { query: "x" }, SearchResult)).generation).toBe("g-0001");
    await expect(api.get("/v2/capabilities", SearchResult)).rejects.toMatchObject({ kind: "unavailable", detail: "Search backend unavailable; check server logs" });
    expect(fetch.calls.every((c) => c.init.credentials === "omit")).toBe(true);
    expect(window.localStorage.length).toBe(0);
    expect(window.sessionStorage.length).toBe(0); // session storage only when the server allows it
  });

  it("keeps the token in sessionStorage only when the server allows it", () => {
    const tokens = new TokenStore();
    tokens.configure(true);
    tokens.set("abc");
    expect(window.sessionStorage.getItem("su_multi_arm.ui.token")).toBe("abc");
    tokens.configure(false);
    expect(window.sessionStorage.length).toBe(0);
    expect(tokens.get()).toBe("abc");
    tokens.set(null);
    expect(tokens.get()).toBeNull();
  });

  it("rejects a payload that does not match the contract instead of guessing", async () => {
    const fetch = fakeFetch({ "POST /v2/search": () => json({ query: "x", hits: "not a list" }) });
    const api = new ApiClient(new TokenStore(), "", fetch.impl);
    await expect(api.post("/v2/search", {}, SearchResult)).rejects.toMatchObject({ kind: "bad_response" });
  });

  it("streams typed events in order and requires a terminal event", async () => {
    const events: Array<Record<string, unknown>> = [
      { event: "decision", pipeline: "default", mode: "rules", agent: "rules", outcome: "decided", intents: [] },
      { event: "arm_result", round: 1, arm: "lexical", record_ids: ["r1"], aggregates: 0 },
      { event: "evidence", ids: ["r1"] },
      { event: "token", text: "{\"answer\":", provisional: true },
      { event: "contract", status: "passed", violations: [], tokens_streamed: 1, tokens_superseded: true },
      { event: "done", result: answerResult() },
    ];
    const fetch = fakeFetch({ "POST /v2/answer/stream": () => sse(events) });
    const api = new ApiClient(new TokenStore(), "", fetch.impl);
    const seen: StreamEvent[] = [];
    await api.stream({ query: "q" }, { onEvent: (e) => seen.push(e) }, new AbortController().signal);
    expect(seen.map((e) => e.event)).toEqual(["decision", "arm_result", "evidence", "token", "contract", "done"]);

    const truncated = fakeFetch({ "POST /v2/answer/stream": () => sse(events.slice(0, 3)) });
    await expect(new ApiClient(new TokenStore(), "", truncated.impl).stream({ query: "q" }, { onEvent: () => undefined }, new AbortController().signal)).rejects.toMatchObject({
      kind: "bad_response",
    });
  });

  it("feature detection reads the route list and tool names; absence is not an error", async () => {
    const fetch = fakeFetch({
      "GET /openapi.json": () => json({ paths: { "/v2/search": { post: {} }, "/v2/info": { get: {} } } }),
      "GET /v2/tools": () => json({ tools: [{ name: "search" }, { name: "log_top" }] }),
    });
    const features = await loadFeatures(new ApiClient(new TokenStore(), "", fetch.impl));
    expect(features.routesKnown).toBe(true);
    expect([...features.routes].sort()).toEqual(["GET /v2/info", "POST /v2/search"]);
    expect(features.tools.has("log_top")).toBe(true);
    const none = await loadFeatures(new ApiClient(new TokenStore(), "", fakeFetch({}).impl));
    expect(none.routesKnown).toBe(false);
    expect(none.tools.size).toBe(0);
  });

  it("ApiError message carries the title and the server's safe detail", () => {
    const err = new ApiError("budget", 429, "budget exhausted (usd)");
    expect(err.message).toBe("Budget exhausted (429): budget exhausted (usd)");
  });
});
