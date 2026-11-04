import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { StreamEvent } from "../api/schema";
import { AnswerCard, citationParts } from "../components/AnswerCard";
import { EMPTY_RUN, reduceRun, ThinkingTimeline, type RunLog } from "../components/ThinkingTimeline";
import { answerResult, groupedAggregate, searchResult } from "./fixtures";
import { XSS_STRINGS } from "./xss_corpus";

function runOf(events: StreamEvent[]): RunLog {
  return events.reduce<RunLog>((run, e, i) => reduceRun(run, e, i * 0.1), { ...EMPTY_RUN, startedAt: 0 });
}

const DECISION = { seq: 1, event: "decision", pipeline: "default", mode: "rules", agent: "rules", outcome: "decided", intents: ["log_top"], steps: [{ arm: "structured" }] } as StreamEvent;
const ARM = { seq: 2, event: "arm_result", round: 1, arm: "lexical", record_ids: ["a", "b"], aggregates: 0 } as StreamEvent;
const EVIDENCE = { seq: 3, event: "evidence", ids: ["a", "b"], omitted_ids: [], truncated_ids: ["b"] } as StreamEvent;
const TOKEN = { seq: 4, event: "token", stage: "generator", text: XSS_STRINGS[0], provisional: true } as StreamEvent;

describe("run timeline", () => {
  it("advances step by step from the events alone", () => {
    const partial = render(<ThinkingTimeline run={runOf([DECISION, ARM])} running />);
    const states = Array.from(partial.container.querySelectorAll(".tl-step")).map((li) => li.getAttribute("data-state"));
    expect(states).toEqual(["done", "done", "active", "pending", "pending", "pending"]);
    expect(partial.container.textContent).toContain("intent log_top");
    expect(partial.container.textContent).toContain("lexical");
    partial.unmount();
    const full = render(<ThinkingTimeline run={runOf([DECISION, ARM, EVIDENCE, TOKEN, { seq: 5, event: "contract", status: "stripped", violations: [{ rule: "factual_without_citations", detail: XSS_STRINGS[1], action: "strip" }], unsupported_constraints: [], tokens_streamed: 1, tokens_superseded: true } as StreamEvent, { seq: 6, event: "done", result: answerResult() } as StreamEvent])} running={false} />);
    const text = full.container.textContent ?? "";
    expect(text).toContain("1 truncated to fit");
    expect(text).toContain("contract stripped");
    expect(full.container.querySelectorAll("script, img").length).toBe(0);
    expect(full.container.querySelector('[data-state="warn"]')).toBeTruthy();
  });

  it("marks the failing step on an error event", () => {
    const view = render(<ThinkingTimeline run={runOf([DECISION, { seq: 2, event: "error", category: "budget_exhausted", message: "budget exhausted (usd)" } as StreamEvent])} running={false} />);
    expect(view.container.querySelector('[data-state="failed"]')?.textContent).toContain("budget_exhausted");
  });
});

describe("answer card", () => {
  it("turns cited ids in the text into chips and shows the numbers with their rendering", () => {
    expect(citationParts("see doc:1 and doc:12.", ["doc:1", "doc:12"]).map((p) => [p.text, p.cite])).toEqual([
      ["see ", null],
      ["doc:1", "doc:1"],
      [" and ", null],
      ["doc:12", "doc:12"],
      [".", null],
    ]);
    const cited: string[] = [];
    const result = answerResult({ retrieval: searchResult({ aggregates: [groupedAggregate({ exactness: "approximate", rendering: "Top 3 services by lines (approximate, hourly quota)." })] }) });
    render(<AnswerCard result={result} history={{ library_identities: ["a", "b"] }} onCite={(id) => cited.push(id)} />);
    fireEvent.click(screen.getByTestId("answer-text").querySelector("button")!);
    expect(cited).toEqual(["logs:signature:aaa"]);
    expect(screen.getByTestId("answer-numbers").textContent).toContain("≈ approximate");
    expect(screen.getByTestId("answer-numbers").textContent).toContain("hourly quota");
    expect(screen.getByTestId("answer-multi-library").textContent).toContain("2 pattern-library identities");
  });
});
