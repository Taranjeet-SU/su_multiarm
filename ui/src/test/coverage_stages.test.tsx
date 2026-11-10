import { render, screen, within } from "@testing-library/react";
import { expect, it } from "vitest";
import { CoverageStages } from "../components/CoverageStages";

function row(service: string, collected: number, matching: number, extracted: number) {
  return { service, completeness: collected / matching, completeness_status: "available", documents_collected: collected, documents_matching: matching,
    scanned: 1, scanned_status: "available", records_collected: 10, records_scanned: 10,
    extracted: extracted / 10, extracted_status: "available", records_extracted: extracted, collection_scope: "includes_selected_records_tier", deterministic_coverage: 1 };
}

it("shows three distinct stages and weighted totals, with noise excluded from extracted", () => {
  render(<CoverageStages output={{ rows: [row("api", 100, 1000, 2), row("worker", 90, 100, 8)] }} />);
  const table = screen.getByRole("table");
  expect(within(table).getAllByRole("columnheader").map((element) => element.textContent)).toEqual(["Service", "Completeness", "Scanned", "Extracted"]);
  expect(screen.getByText("17.3%")).toBeTruthy(); // 190 / 1100, not mean(10%,90%)
  expect(screen.getByText("50.0%")).toBeTruthy(); // 10 useful / 20 scanned
  expect(screen.getByText(/Noise never increases extraction/)).toBeTruthy();
  expect(screen.getAllByText("Selected-tier collection included")).toHaveLength(2);
  expect(screen.getByLabelText("api: Extracted").getAttribute("value")).toBe("0.2");
});

it("keeps old missing metrics unavailable and surfaces missing days", () => {
  render(<CoverageStages output={{ rows: [{ service: "legacy", deterministic_coverage: 1 }], source_unavailable: ["2026-03-09"] }} />);
  expect(screen.getAllByText("Unavailable")).toHaveLength(6);
  expect(screen.queryByRole("progressbar")).toBeNull();
  expect(screen.getByText(/Missing days: 2026-03-09/)).toBeTruthy();
});

it("shows incident capture separately without claiming whole-source completeness", () => {
  render(<CoverageStages output={{ rows: [{ ...row("filtered-api", 7, 1059, 2), completeness: null,
    completeness_status: "scope_filtered", collection_scope: "incident_seed_policy_with_bounded_context",
    selected_seed_documents: 1, seeds_collected: 1, context_documents: 6, context_pending_seeds: 0 }] }} />);
  expect(screen.getAllByText("Filtered scope")).toHaveLength(2);
  expect(screen.getAllByText("1 / 1 selected matches captured (100.0%)")).toHaveLength(2);
  expect(screen.getAllByText("6 additional context documents")).toHaveLength(2);
  expect(screen.getAllByText("0 matches awaiting context")).toHaveLength(2);
  expect(screen.queryByLabelText("filtered-api: Completeness")).toBeNull();
});

it("does not turn missing incident counters into zero matches or complete context", () => {
  render(<CoverageStages output={{ rows: [{ service: "pending-api", completeness_status: "scope_filtered",
    collection_scope: "incident_seed_policy_with_bounded_context" }] }} />);
  expect(screen.getAllByText("unavailable / unavailable selected matches captured")).toHaveLength(2);
  expect(screen.getAllByText("Context completion unavailable")).toHaveLength(2);
  expect(screen.queryByText(/0 matches awaiting context/)).toBeNull();
});
