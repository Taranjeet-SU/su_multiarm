import { lazy, Suspense, useEffect, useMemo, useState } from "react";
import type { SearchResult } from "../api/schema";
import { displayText, short } from "../lib/text";
import { AggregateCard } from "./AggregateCard";
import { KeyValues, Tabs, Text } from "./common";
import { HitCard, queryTerms } from "./HitCard";
import { TracePanel } from "./TracePanel";

const MiniGraph = lazy(() => import("./MiniGraph"));

// Tabs per arm. ``community`` is the arm name in ``arm_hits``; any other arm the server returns gets
// its own tab too, so a new arm is shown without a UI change (extension point).
const ARM_TABS: Array<{ id: string; label: string }> = [
  { id: "dense", label: "Dense" },
  { id: "lexical", label: "Lexical" },
  { id: "graph", label: "Graph" },
  { id: "community", label: "Communities" },
];

export interface Focus {
  id: string;
  nonce: number;
}

export function EvidenceTabs({ result, answerTrace, focus, timeFields }: { result: SearchResult; answerTrace?: unknown; focus?: Focus | null; timeFields: ReadonlySet<string> }) {
  const [active, setActive] = useState<string>("overview");
  const terms = useMemo(() => queryTerms(result.query), [result.query]);
  const extraArms = Object.keys(result.arm_hits).filter((a) => !ARM_TABS.some((t) => t.id === a));
  const tabs = [
    { id: "overview", label: "Overview" },
    { id: "fused", label: "Fused", count: result.hits.length },
    ...[...ARM_TABS, ...extraArms.map((a) => ({ id: a, label: a }))].map((t) => ({ ...t, count: result.arm_hits[t.id]?.length ?? 0 })),
    { id: "structured", label: "Structured", count: result.aggregates.length },
    { id: "trace", label: "Trace" },
  ];

  useEffect(() => {
    if (!focus) return;
    const isAggregate = result.aggregates.some((a) => a.id === focus.id);
    setActive(isAggregate ? "structured" : "fused");
    const timer = window.setTimeout(() => {
      const node = document.getElementById(`ev-${focus.id}`);
      if (node) {
        node.scrollIntoView?.({ block: "center" });
        node.focus?.();
      }
    }, 0);
    return () => window.clearTimeout(timer);
  }, [focus, result.aggregates]);

  const decision = (result.trace.decision ?? {}) as Record<string, unknown>;
  const flashId = focus?.id;
  return (
    <section className="evidence" aria-label="evidence">
      <Tabs label="evidence per arm" tabs={tabs} active={active} onChange={setActive} />
      <div className="tab-body" role="tabpanel">
        {active === "overview" ? (
          <div>
            <KeyValues
              rows={[
                ["generation", short(result.generation, 16)],
                ["decision", `${displayText(decision.agent)} → ${displayText(decision.outcome)}`],
                ["intents", Array.isArray(decision.intents) && decision.intents.length ? decision.intents.join(", ") : "none"],
                ["arms with hits", Object.entries(result.arm_hits).filter(([, h]) => h.length).map(([a, h]) => `${a} (${h.length})`).join(", ") || "none"],
                ["aggregates", result.aggregates.length],
              ]}
            />
            {result.unsupported_constraints.length ? <UnsupportedList items={result.unsupported_constraints} /> : null}
          </div>
        ) : null}
        {active === "fused" ? <HitList hits={result.hits} terms={terms} flashId={flashId} /> : null}
        {active === "graph" && (result.arm_hits.graph ?? []).length ? (
          <Suspense fallback={<p className="muted">loading the graph map…</p>}>
            <MiniGraph hits={result.arm_hits.graph ?? []} />
          </Suspense>
        ) : null}
        {[...ARM_TABS.map((t) => t.id), ...extraArms].includes(active) ? <HitList hits={result.arm_hits[active] ?? []} terms={active === "lexical" ? terms : []} flashId={flashId} empty={`The ${active} arm returned no hits for this request (or did not run).`} /> : null}
        {active === "structured" ? (
          result.aggregates.length ? (
            result.aggregates.map((a, i) => <AggregateCard key={a.id ?? i} aggregate={a} timeFields={timeFields} flash={a.id === flashId} />)
          ) : (
            <p className="muted">No structured aggregate for this request (the decision did not plan a structured step).</p>
          )
        ) : null}
        {active === "trace" ? <TracePanel answerTrace={answerTrace} retrievalTrace={result.trace} /> : null}
      </div>
    </section>
  );
}

function HitList({ hits, terms, flashId, empty = "No hits." }: { hits: SearchResult["hits"]; terms: string[]; flashId?: string; empty?: string }) {
  if (!hits.length) return <p className="muted">{empty}</p>;
  return (
    <div className="hits">
      {hits.map((h, i) => (
        <HitCard key={`${h.document_id}-${i}`} hit={h} rank={i + 1} terms={terms} flash={h.document_id === flashId} />
      ))}
    </div>
  );
}

export function UnsupportedList({ items }: { items: SearchResult["unsupported_constraints"] }) {
  return (
    <div className="chips" aria-label="unsupported constraints">
      <span className="label">not applied:</span>
      {items.map((u, i) => (
        <span className="chip chip-warn" key={i} title={u.suggestion ? displayText(u.suggestion) : undefined}>
          <Text value={u.constraint_text} max={120} /> — {displayText(u.reason)}
        </span>
      ))}
    </div>
  );
}
