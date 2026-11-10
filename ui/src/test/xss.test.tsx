import { render, screen } from "@testing-library/react";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { AggregateCard } from "../components/AggregateCard";
import { SafeLink } from "../components/common";
import { EvidenceTabs } from "../components/EvidenceTabs";
import { HitCard } from "../components/HitCard";
import { TracePanel } from "../components/TracePanel";
import { displayText, safeHref } from "../lib/text";
import { groupedAggregate, hit, searchResult } from "./fixtures";
import { XSS_STRINGS, XSS_URLS } from "./xss_corpus";

declare global {
  interface Window {
    __xss?: number;
  }
}

function assertInert(container: HTMLElement) {
  expect(container.querySelectorAll("script, iframe, object, embed, style, img, svg image").length).toBe(0);
  for (const el of Array.from(container.querySelectorAll("*"))) {
    for (const attr of Array.from(el.attributes)) {
      expect(attr.name.startsWith("on"), `${el.tagName} has ${attr.name}`).toBe(false);
      if (attr.name === "href" || attr.name === "src") expect(attr.value).toMatch(/^(https?:\/\/|#)/);
    }
  }
  for (const a of Array.from(container.querySelectorAll("a"))) {
    expect(a.getAttribute("rel")).toContain("noopener");
    expect(a.getAttribute("rel")).toContain("noreferrer");
  }
  expect(window.__xss).toBeUndefined();
}

beforeEach(() => {
  delete window.__xss;
});
afterEach(() => {
  expect(window.__xss).toBeUndefined();
});

describe("XSS corpus is rendered inert", () => {
  it.each(XSS_STRINGS.map((s, i) => [i, s] as const))("hit card #%i", (_i, payload) => {
    const { container } = render(<HitCard hit={hit({ title: payload, text: payload, source: payload, provenance: [{ quote: payload, entity: payload, field: payload }] })} rank={1} terms={["script", "img"]} />);
    assertInert(container);
    if (payload.length < 200 && !/[\u001b‮]/.test(payload)) expect(container.textContent).toContain(payload);
  });

  it("strips ANSI escapes and makes bidi overrides visible", () => {
    expect(displayText("\u001b[31mERROR\u001b[0m x \u001b]0;t\u0007")).toBe("ERROR x ");
    expect(displayText("a‮b‬c")).toBe("a[U+202E]b[U+202C]c");
    expect(displayText("bell\u0007 nul\u0000 ok\nnext\ttab")).toBe("bell nul ok\nnext\ttab");
  });

  it.each(XSS_URLS)("hostile URL %s is never an href", (url) => {
    expect(safeHref(url)).toBeNull();
    const { container } = render(<SafeLink url={url} />);
    expect(container.querySelector("a")).toBeNull();
    expect(container.textContent).toBe(displayText(url));
    assertInert(container);
  });

  it("an http(s) URL becomes a noopener link that shows the URL", () => {
    const { container } = render(<SafeLink url="https://docs.example.test/page?q=1" />);
    const a = container.querySelector("a")!;
    expect(a.getAttribute("href")).toBe("https://docs.example.test/page?q=1");
    expect(a.getAttribute("rel")).toBe("noopener noreferrer nofollow");
    expect(a.textContent).toBe("https://docs.example.test/page?q=1");
  });

  it("evidence tabs, aggregates and trace with hostile values", () => {
    const evil = XSS_STRINGS[1];
    const result = searchResult({
      query: evil,
      hits: [hit({ title: evil, text: evil, url: "javascript:window.__xss=1" })],
      arm_hits: { [evil]: [hit({ text: evil })], lexical: [hit({ text: XSS_STRINGS[0] })] },
      aggregates: [groupedAggregate({ field: evil, basis: [evil], note: evil, groups: [{ key: { [evil]: evil }, value: 3, count: 3 }] })],
      unsupported_constraints: [{ constraint_text: evil, reason: "value_not_observed", suggestion: evil }],
      trace: { decision: { agent: evil, outcome: evil, intents: [evil] }, steps: [{ arm: evil, documents: 1 }], model_calls: [{ stage: evil, provider: evil, model: evil, usd: 0.001 }] },
    });
    const { container } = render(<EvidenceTabs result={result} timeFields={new Set()} />);
    assertInert(container);
    for (const tab of screen.getAllByRole("tab")) {
      tab.click();
      assertInert(container);
    }
    const agg = render(<AggregateCard aggregate={result.aggregates[0]} timeFields={new Set()} />);
    assertInert(agg.container);
    const trace = render(<TracePanel retrievalTrace={result.trace} />);
    assertInert(trace.container);
  });

  it("no source file uses raw-HTML APIs", () => {
    const root = join(__dirname, "..");
    const files: string[] = [];
    const walk = (dir: string) => {
      for (const name of readdirSync(dir)) {
        const path = join(dir, name);
        if (statSync(path).isDirectory()) walk(path);
        else if (/\.(ts|tsx)$/.test(name) && !path.includes(`${join("src", "test")}`)) files.push(path);
      }
    };
    walk(root);
    expect(files.length).toBeGreaterThan(10);
    for (const file of files) {
      const text = readFileSync(file, "utf8");
      expect(text, file).not.toMatch(/dangerouslySetInnerHTML|\.innerHTML\s*=|outerHTML|insertAdjacentHTML|document\.write|new Function\(|\beval\(/);
      expect(text, file).not.toMatch(/window\.localStorage|\blocalStorage\.(get|set)Item|document\.cookie/);
    }
  });
});
