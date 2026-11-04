import { describe, expect, it } from "vitest";
import { Aggregate } from "../api/schema";
import { seriesOf } from "../lib/charts";
import { fromNeighbourhood, fromProvenance } from "../lib/graph";
import { summarise } from "../lib/trace";
import { highlightParts } from "../components/HitCard";
import { groupedAggregate, hit } from "./fixtures";

describe("charts", () => {
  it("plots groups as bars and date groups as a sorted line", () => {
    const bars = seriesOf(Aggregate.parse(groupedAggregate()));
    expect(bars?.kind).toBe("bar");
    expect(bars?.points).toEqual([
      { label: "doc-fetcher", value: 22 },
      { label: "ingest-worker", value: 22 },
      { label: "query-gateway", value: 20 },
    ]);
    const days = seriesOf(
      Aggregate.parse(groupedAggregate({ field: "date", groups: [{ key: { date: "2026-01-19" }, value: 5, count: 5 }, { key: { date: "2026-01-18" }, value: 3, count: 3 }] })),
      new Set(["date"]),
    );
    expect(days?.kind).toBe("line");
    expect(days?.points.map((p) => p.label)).toEqual(["2026-01-18", "2026-01-19"]);
  });

  it("never coerces a non-numeric value to zero and returns null for scalars", () => {
    const mixed = seriesOf(Aggregate.parse(groupedAggregate({ groups: [{ key: { service: "a" }, value: { p95: 3 }, count: 2 }, { key: { service: "b" }, value: null, count: 0 }] })));
    expect(mixed?.measure).toBe("count");
    expect(mixed?.points).toEqual([
      { label: "a", value: 2 },
      { label: "b", value: 0 },
    ]);
    expect(seriesOf(Aggregate.parse({ operation: "count", value: 7, exactness: "exact", denominator: 7 }))).toBeNull();
  });
});

describe("graph from provenance", () => {
  it("draws stored edges and mentions only; co-mentioned entities stay unconnected", () => {
    const data = fromProvenance([
      hit({ document_id: "d1", provenance: [{ entity: "redis", quote: "redis", span: [10, 15], match_level: "exact" }, { entity: "db-client", quote: "db-client", match_level: "exact" }, { chunk_id: "c", field: "f", start: 0, end: 3 }] }),
      hit({ document_id: "d2", provenance: [{ kind: "graph_path", subject: "e:db-client", predicate: "fails_to_connect", object: "e:redis", edge_kind: "fact", support: "trigger_in_span", quote: "db-client failed to connect redis" }] }),
    ]);
    expect(data.origin).toBe("provenance");
    const pairs = data.edges.map((e) => `${e.source} ${e.predicate} ${e.target}`).sort();
    expect(pairs).toEqual(["e:db-client fails_to_connect e:redis", "entity:db-client mentioned_in doc:d1", "entity:redis mentioned_in doc:d1"]);
    expect(data.skippedRows).toBe(1);
    expect(data.edges.find((e) => e.predicate === "fails_to_connect")?.quotes[0].quote).toBe("db-client failed to connect redis");
  });

  it("drops server edges whose endpoints were not returned", () => {
    const data = fromNeighbourhood({
      nodes: [{ id: "a", type: "component", name: "a" }, { id: "b", type: "dependency", name: "b" }],
      edges: [{ id: "e1", source: "a", target: "b", predicate: "p", edge_kind: "fact", support: "trigger_in_span" }, { id: "e2", source: "a", target: "ghost", predicate: "p", edge_kind: "fact", support: "x" }],
      truncated: true,
    });
    expect(data.edges.map((e) => e.id)).toEqual(["e1"]);
    expect(data.skippedRows).toBe(1);
    expect(data.truncated).toBe(true);
  });
});

describe("trace summary", () => {
  it("sums priced calls only and counts the unpriced ones", () => {
    const t = summarise(
      { seconds: 1.5, rounds: [{ steps: [{ arm: "dense", documents: 3, seconds: 0.1 }] }, { steps: [{ arm: "lexical", documents: 2 }] }], model_calls: [{ stage: "generator", usd: 0.002, input_tokens: 10 }, { stage: "review", usd: null }] },
      { seconds: 0.4, decision: { agent: "rules", seconds: 0.01 } },
    );
    expect(t.armsFired).toEqual(["dense", "lexical"]);
    expect(t.steps.map((s) => s.round)).toEqual([1, 2]);
    expect(t.usdTotal).toBeCloseTo(0.002);
    expect(t.callsWithoutPrice).toBe(1);
    expect(t.totalSeconds).toBe(1.5);
    expect(t.decisionSeconds).toBe(0.01);
  });
});

describe("highlight", () => {
  it("splits text around terms without changing it", () => {
    const parts = highlightParts("Redis refused by redis-client", ["redis"]);
    expect(parts.map((p) => p.text).join("")).toBe("Redis refused by redis-client");
    expect(parts.filter((p) => p.match).map((p) => p.text)).toEqual(["Redis", "redis"]);
  });
});

import { render } from "@testing-library/react";
import { createElement } from "react";
import { AggregateCard } from "../components/AggregateCard";

describe("aggregate card", () => {
  it("shows the server's rendering sentence, the exactness word and the truncated-ids flag", () => {
    const agg = Aggregate.parse(groupedAggregate({ exactness: "approximate", document_ids_truncated: true, rendering: "lines containing 'error' on index date 2026-01-19 (UTC)" }));
    const { getByTestId, container } = render(createElement(AggregateCard, { aggregate: agg, timeFields: new Set<string>() }));
    expect(getByTestId("aggregate-rendering").textContent).toBe("lines containing 'error' on index date 2026-01-19 (UTC)");
    expect(getByTestId("exactness-badge").textContent).toBe("≈ approximate");
    expect(getByTestId("ids-truncated").textContent).toContain("truncated ids");
    expect(container.textContent).toContain("denominator");
  });
});
