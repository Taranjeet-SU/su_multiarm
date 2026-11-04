import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { GenerationDays } from "../views/OpsViews";

describe("generation days (rev 4)", () => {
  it("shows served and archived days and warns when several library versions are served", () => {
    const one = render(<GenerationDays info={{ served_days: ["2026-01-18", "2026-01-19"], archived_days: null, library_identities: ["a"] }} />);
    const text = one.getByTestId("generation-days").textContent ?? "";
    expect(text).toContain("2: 2026-01-18, 2026-01-19");
    expect(text).toContain("not recorded by this generation");
    expect(one.queryByTestId("multi-library")).toBeNull();
    const many = render(<GenerationDays info={{ served_days: ["d1"], archived_days: ["d0"], library_identities: ["a", "b", "c"] }} />);
    expect(many.getByTestId("multi-library").textContent).toContain("span 3 pattern-library identities");
  });
});
