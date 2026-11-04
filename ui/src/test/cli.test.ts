import { describe, expect, it } from "vitest";
import cases from "../lib/cli_cases.json";
import { decisionCommand, shellQuote, type DecisionCommand } from "../lib/cli";

describe("copy CLI command", () => {
  it.each(cases.map((c) => [c.input.id, c] as const))("matches the shared case %s (Python round-trips it)", (_id, c) => {
    expect(decisionCommand(c.input as DecisionCommand)).toBe(c.expected);
  });

  it("quotes like shlex.quote", () => {
    expect(shellQuote("")).toBe("''");
    expect(shellQuote("a.b-c_d@e%f+g=h:i,j/k")).toBe("a.b-c_d@e%f+g=h:i,j/k");
    expect(shellQuote("it's")).toBe(`'it'"'"'s'`);
    expect(shellQuote("$HOME")).toBe("'$HOME'");
  });
});
