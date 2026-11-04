// Untrusted text (log lines, templates, evidence, model output) is only ever rendered as React text
// nodes. These helpers make it *readable* and *honest* before that: ANSI escape sequences are removed,
// other C0/C1 control characters are dropped (newline and tab kept), and Unicode bidi controls
// (which can visually reorder a line, "trojan source") are replaced by a visible marker.

// CSI / OSC / single-character ANSI escapes.
const ANSI = /\u001b\[[0-?]*[ -/]*[@-~]|\u001b\][^\u0007\u001b]*(?:\u0007|\u001b\\)|\u001b[@-Z\\-_]/g;
// eslint-disable-next-line no-control-regex
const CONTROL = /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f-\u009f]/g;
// eslint-disable-next-line no-control-regex
const HAS_CONTROL = /[\u0000-\u001f\u007f-\u009f]/;
const BIDI =/[‪-‮⁦-⁩‎‏؜]/g;

export function displayText(value: unknown): string {
  if (value === null || value === undefined) return "";
  const text = typeof value === "string" ? value : typeof value === "object" ? JSON.stringify(value) : String(value);
  return text.replace(ANSI, "").replace(CONTROL, "").replace(BIDI, (ch) => `[U+${ch.codePointAt(0)!.toString(16).toUpperCase().padStart(4, "0")}]`);
}

/** At most ``max`` characters; the cut is visible, never silent. */
export function clip(value: unknown, max = 2000): string {
  const text = displayText(value);
  return text.length > max ? `${text.slice(0, max)}… [${text.length - max} more characters]` : text;
}

/** A short label (axis ticks, graph nodes): cut with an ellipsis; the full text stays in tables. */
export function short(value: unknown, max = 40): string {
  const text = displayText(value);
  return text.length > max ? `${text.slice(0, max - 1)}…` : text;
}

const SAFE_PROTOCOLS = new Set(["http:", "https:"]);

/** A URL that may become an ``href``: absolute http(s) only. Anything else (``javascript:``,
 * ``data:``, ``vbscript:``, relative or malformed) returns ``null`` and is rendered as text. */
export function safeHref(url: unknown): string | null {
  if (typeof url !== "string" || url.length > 4096) return null;
  const trimmed = url.trim();
  if (trimmed !== url || HAS_CONTROL.test(url) || /\s/.test(url)) return null;
  try {
    const parsed = new URL(trimmed);
    return SAFE_PROTOCOLS.has(parsed.protocol) ? parsed.href : null;
  } catch {
    return null;
  }
}

export function formatNumber(value: unknown): string {
  if (typeof value === "number") return Number.isInteger(value) ? value.toLocaleString("en-US") : value.toLocaleString("en-US", { maximumFractionDigits: 4 });
  return displayText(value);
}

export function formatSeconds(value: unknown): string {
  return typeof value === "number" ? `${(value * 1000).toFixed(value < 0.01 ? 2 : 0)} ms` : "n/a";
}

export function formatUsd(value: unknown): string {
  return typeof value === "number" ? `USD ${value.toFixed(value < 0.01 ? 5 : 4)}` : "not reported";
}
