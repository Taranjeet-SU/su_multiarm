import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { AnswerCard } from "../components/AnswerCard";
import { AnswerMarkdown } from "../components/AnswerMarkdown";
import { AnswerRows } from "../components/AnswerRows";
import { answerResult, groupedAggregate, searchResult } from "./fixtures";
import { XSS_STRINGS } from "./xss_corpus";

export const attentionAnswer = `## Needs attention

**Two services need investigation.** The counts below cover the sampled window.

| Issue | Supporting evidence | Next check |
| :--- | ---: | :--- |
| Worker timeouts | 12 parsed errors [aggregate:1] | Inspect the retry queue. |
| Gateway latency | See [doc:12] | Check upstream health. |

### Follow-up

- Treat the counts as approximate.
- Suggested checks are advice, not established causes.

Use \`timeout_ms\` when checking configuration.

\`\`\`text
doc:12 is literal inside code
\`\`\`
`;

describe("checked answer Markdown", () => {
  it("renders an attention summary as headings, a table and lists with working evidence chips", () => {
    const onCite = vi.fn();
    const { container } = render(<AnswerMarkdown text={attentionAnswer} citations={["aggregate:1", "doc:12"]} onCite={onCite} />);
    expect(screen.getByRole("heading", { name: "Needs attention" })).toBeTruthy();
    const table = screen.getByRole("table");
    expect(within(table).getAllByRole("columnheader")).toHaveLength(3);
    expect(within(table).getAllByRole("row")).toHaveLength(3);
    expect(container.querySelector("strong")?.textContent).toBe("Two services need investigation.");
    expect(screen.getAllByRole("listitem")).toHaveLength(2);
    fireEvent.click(within(table).getByRole("button", { name: "Show evidence doc:12" }));
    expect(onCite).toHaveBeenCalledWith("doc:12");
    expect(container.querySelector("pre code")?.textContent).toContain("doc:12 is literal");
    expect(container.querySelector("pre button")).toBeNull();
    expect(container.querySelectorAll("[style]")).toHaveLength(0);
    expect(table.querySelector("th.align-right")).toBeTruthy();
  });

  it.each(XSS_STRINGS)("keeps hostile Markdown inert: %.60s", (payload) => {
    const { container } = render(<AnswerMarkdown text={payload} citations={[]} onCite={() => {}} />);
    expect(container.querySelectorAll("script, iframe, object, embed, style, img, svg")).toHaveLength(0);
    for (const element of Array.from(container.querySelectorAll("*"))) {
      for (const attr of Array.from(element.attributes)) expect(attr.name.startsWith("on")).toBe(false);
    }
    for (const link of Array.from(container.querySelectorAll("a"))) expect(link.href).toMatch(/^https?:\/\//);
    expect(window.__xss).toBeUndefined();
  });

  it("keeps external links safe and does not decorate code or link destinations", () => {
    const { container } = render(<AnswerMarkdown text={'[docs](https://example.test/doc:12) and `doc:12`\n\n<script>alert(1)</script>\n\n[bad](javascript:alert%281%29)'} citations={["doc:12"]} onCite={() => {}} />);
    expect(screen.getByRole("link").getAttribute("rel")).toBe("noopener noreferrer nofollow");
    expect(screen.getByRole("link").getAttribute("href")).toBe("https://example.test/doc:12");
    expect(container.querySelectorAll("button")).toHaveLength(0);
    expect(container.textContent).toContain("<script>alert(1)</script>");
  });

  it("keeps ordinary multi-paragraph answers readable", () => {
    const { container } = render(<AnswerMarkdown text={"First finding.\n\nSecond finding."} citations={[]} onCite={() => {}} />);
    expect(container.querySelectorAll("p")).toHaveLength(2);
  });
});

describe("structured rows in the answer", () => {
  it("presents service failures as one business table, with supporting detail collapsed", () => {
    const result = answerResult({ retrieval: searchResult({ aggregates: [groupedAggregate({ id: "logs:signature:aaa", exactness: "bounded", rendering: "Counts cover classified errors only.", rows: [{ service: "svc-api", error_type: "Connection timeout", error_count: 12, signature_id: "connection.timeout", supporting_template: "Request timed out after <n> ms" }] })] }) });
    render(<AnswerCard result={result} history={null} onCite={() => {}} />);
    expect(screen.getAllByRole("table")).toHaveLength(1);
    expect(screen.getAllByRole("columnheader").map((cell) => cell.textContent)).toEqual(["Service", "Error type", "Error count"]);
    expect(screen.getByRole("cell", { name: "12" })).toBeTruthy();
    expect(screen.queryByTestId("answer-numbers")).toBeNull();
    expect(screen.getByText("Supporting log patterns and count limitations").closest("details")?.open).toBe(false);
    expect(screen.getByText("Counts cover classified errors only.")).toBeTruthy();
  });
  it("shows returned rows, including later-row fields and zero values, with explicit expansion", () => {
    const rows = Array.from({ length: 12 }, (_, i) => i ? { service: `svc-${i}`, lines: i, note: "approximate" } : { service: "svc-0", lines: 0 });
    render(<AnswerRows aggregate={groupedAggregate({ rows })} />);
    expect(screen.getAllByRole("row")).toHaveLength(11);
    expect(screen.getByRole("columnheader", { name: "note" })).toBeTruthy();
    expect(screen.getByRole("cell", { name: "0" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Show all 12 returned rows" }));
    expect(screen.getAllByRole("row")).toHaveLength(13);
  });

  it("shows cited result tables while preserving exactness, and does not promote uncited evidence", () => {
    const result = answerResult({ retrieval: searchResult({ aggregates: [groupedAggregate({ id: "logs:signature:aaa", exactness: "bounded", rendering: "Partial sample.", rows: [{ service: "svc-a", lines: 12 }] }), groupedAggregate({ id: "not-cited", rows: [{ service: "hidden", lines: 999 }] })] }) });
    const view = render(<AnswerCard result={result} history={null} onCite={() => {}} />);
    expect(screen.getAllByRole("table")).toHaveLength(1);
    expect(screen.getByRole("table").textContent).toContain("bounded");
    expect(screen.queryByRole("cell", { name: "hidden" })).toBeNull();
    view.rerender(<AnswerCard result={{ ...result, contract: "abstained" }} history={null} onCite={() => {}} />);
    expect(screen.queryByRole("table")).toBeNull();
  });
});

it("honors planner columns for client/service issues and puts contract checks in diagnostics", () => {
  const aggregate = groupedAggregate({ id: "logs:signature:aaa", columns: ["tenant", "service", "error_type", "error_count"], rows: [{ tenant: "client-demo", service: "svc-api", error_type: "Connection timeout", error_count: 12, signature_id: "timeout", supporting_template: "Request timed out" }] });
  render(<AnswerCard result={answerResult({ retrieval: searchResult({ aggregates: [aggregate] }) })} history={null} onCite={() => {}} />);
  expect(screen.getAllByRole("columnheader").map((cell) => cell.textContent)).toEqual(["Client", "Service", "Error type", "Error count"]);
  expect(screen.getByRole("cell", { name: "client-demo" })).toBeTruthy();
  expect(screen.getByText("Answer diagnostics").closest("details")?.open).toBe(false);
});
