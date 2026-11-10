import { render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { ViewBoundary } from "../components/ViewBoundary";

it("keeps a failed view visible with a recovery action", () => {
  const error = vi.spyOn(console, "error").mockImplementation(() => {});
  function MissingBundle(): never { throw new Error("Failed to fetch dynamically imported module"); }
  try {
    render(<ViewBoundary><MissingBundle /></ViewBoundary>);
    expect(screen.getByRole("alert").textContent).toContain("This view could not load");
    expect(screen.getByRole("button", { name: "Reload page" })).toBeTruthy();
  } finally { error.mockRestore(); }
});
