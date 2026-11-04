// "Copy CLI command" text for human pattern decisions (addendum A2 §4.4.3, N13). The UI never writes:
// a decision is made by running this command where the library lives. Quoting is POSIX ``shlex.quote``
// (same safe-character set), so the copied text is one argument per value whatever the id or reason
// contains; ``tests/unit/test_ui_cli_commands.py`` round-trips the shared cases through
// ``shlex.split`` and the real CLI parser.

const SAFE = /^[A-Za-z0-9_@%+=:,./-]+$/;

export function shellQuote(value: string): string {
  if (value === "") return "''";
  if (SAFE.test(value)) return value;
  return `'${value.replace(/'/g, `'"'"'`)}'`;
}

export type DecisionOp = "promote" | "deprecate" | "disable";

export interface DecisionCommand {
  config: string;
  op: DecisionOp;
  id: string;
  by: string;
  reason: string;
}

export function decisionCommand(c: DecisionCommand): string {
  // ``--opt=value`` keeps a value that starts with "-" a value; the id comes after "--", so it can
  // never become an option either.
  const argv = ["su-multi-arm", `--config=${c.config}`, "patterns", c.op, `--by=${c.by}`, `--reason=${c.reason}`, "--", c.id];
  return argv.map(shellQuote).join(" ");
}
