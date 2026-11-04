import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { CoverageGauges, Heatmap, IntervalTimeline, RankedBars, TrendLine } from "../components/Charts";
import { LogResult } from "../components/LogResult";
import { barsOf, keysOf, pointsOf } from "../lib/logRows";
import { XSS_STRINGS } from "./xss_corpus";

// Shapes as returned by the gold-set log tools (synthetic values).
const TREND = { status: "answered", exactness: "exact", rows: [{ date: "2026-03-01", lines: 8 }, { date: "2026-03-02", lines: 12 }, { date: "2026-03-05", lines: 24 }], source_unavailable: ["2026-03-04"], rendering: "Lines per day ...", window: { label: "last 10 days" } };
const TOP = { status: "answered", exactness: "approximate", rows: [{ service: "svc-api", lines: 4 }, { service: "svc-auth", lines: 9 }], rendering: "Top services ...", methods: ["hourly_quota"] };

describe("log rows adapters", () => {
  it("picks dimension and measure keys and keeps missing days as gaps", () => {
    expect(keysOf(TREND.rows)).toEqual({ label: "date", value: "lines" });
    expect(pointsOf(TREND)).toEqual([
      { label: "2026-03-01", value: 8 },
      { label: "2026-03-02", value: 12 },
      { label: "2026-03-04", value: null },
      { label: "2026-03-05", value: 24 },
    ]);
    expect(barsOf(TOP)).toEqual([
      { label: "svc-api", value: 4, approximate: true },
      { label: "svc-auth", value: 9, approximate: true },
    ]);
  });
});

describe("charts", () => {
  it("ranked bars sort, label directly, hatch approximate bars and state the finding", () => {
    const { container } = render(<RankedBars title="Top services" bars={[{ label: "a", value: 3 }, { label: "b", value: 7, approximate: true }]} unit="lines" />);
    expect(container.textContent).toContain("b leads with ≈7 lines, 4 ahead of a.");
    expect(container.querySelectorAll("rect.bar.approx").length).toBe(1);
    expect(Array.from(container.querySelectorAll("text.value-label")).map((t) => t.textContent)).toEqual(["≈7", "3"]);
  });

  it("trend line breaks at a gap and never plots it as zero", () => {
    const { container } = render(<TrendLine title="Trend" points={pointsOf(TREND)} />);
    expect(container.querySelectorAll("path.line").length).toBe(2);
    expect(container.querySelectorAll("circle.dot.gap").length).toBe(1);
    expect(container.textContent).toContain("1 bucket(s) without data");
  });

  it("heatmap prints every value and quantises the ramp; gauges print regex and model apart", () => {
    const heat = render(<Heatmap title="Tenants × services" rows={["t1", "t2"]} cols={["svc-a", "svc-b"]} value={(r, c) => (r === "t1" && c === "svc-a" ? 10 : r === "t2" && c === "svc-b" ? null : 1)} rowLabel="tenant" colLabel="service" />);
    expect(heat.container.querySelector("td.h5")?.textContent).toBe("10");
    expect(heat.container.querySelector("td.h0")?.textContent).toBe("·");
    const gauges = render(<CoverageGauges title="Coverage" gauges={[{ label: "svc-a", regex: 0.85, withModel: null }, { label: "svc-b", regex: 0.96, withModel: 0.995 }]} />);
    expect(gauges.container.textContent).toContain("85.0 % regex · model n/a");
    expect(gauges.container.textContent).toContain("96.0 % regex · 99.5 % with model");
    expect(gauges.container.textContent).toContain("1 of 2 service(s) below the 90 % regex-only target: svc-a.");
  });

  it("interval timeline and log results render hostile labels as text", () => {
    const t = Date.parse("2026-03-05T13:00:00Z");
    const tl = render(<IntervalTimeline title="Incidents" intervals={[{ label: XSS_STRINGS[0], sublabel: XSS_STRINGS[1], start: t, end: t + 3 * 3600_000, peak: t, value: 9 }]} />);
    expect(tl.container.querySelectorAll("script, img").length).toBe(0);
    render(<LogResult tool="log_top" output={{ ...TOP, rows: [{ service: XSS_STRINGS[1], lines: 2 }], rendering: XSS_STRINGS[0] }} />);
    expect(screen.getByTestId("log-result-log_top").querySelectorAll("script, img").length).toBe(0);
    expect(screen.getByTestId("log-result-log_top").textContent).toContain(XSS_STRINGS[0]);
  });
});
