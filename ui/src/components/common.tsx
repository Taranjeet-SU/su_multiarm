// Shared presentational components. Every untrusted string goes through ``displayText`` into a React
// text node; the UI never uses a raw-HTML API (a vitest guard greps the sources).
import type { ReactNode } from "react";
import { ApiError, ERROR_TITLES } from "../api/client";
import { clip, displayText, safeHref } from "../lib/text";

export function Text({ value, max, className }: { value: unknown; max?: number; className?: string }) {
  return <span className={className}>{max ? clip(value, max) : displayText(value)}</span>;
}

/** Pre-formatted untrusted text (log lines, quotes): whitespace kept, still a text node. */
export function Pre({ value, max = 4000 }: { value: unknown; max?: number }) {
  return <pre className="pre">{clip(value, max)}</pre>;
}

/** An ``<a>`` only for absolute http(s) URLs; anything else is shown as inert text. */
export function SafeLink({ url, children }: { url: unknown; children?: ReactNode }) {
  const href = safeHref(url);
  if (!href) return <span className="inert-url" title="not a http(s) link; shown as text">{displayText(url)}</span>;
  return (
    <a href={href} target="_blank" rel="noopener noreferrer nofollow">
      {children ?? displayText(url)}
    </a>
  );
}

export function ErrorBanner({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  if (!error) return null;
  const api = error instanceof ApiError ? error : null;
  const title = api ? ERROR_TITLES[api.kind] : "Error";
  const detail = api ? api.detail : error instanceof Error ? error.message : String(error);
  return (
    <div className={`banner banner-error kind-${api?.kind ?? "unknown"}`} role="alert">
      <strong>{title}</strong>
      {detail ? (
        <div>
          <Text value={detail} max={2000} />
        </div>
      ) : null}
      {onRetry ? (
        <button type="button" onClick={onRetry}>
          Try again
        </button>
      ) : null}
    </div>
  );
}

export function NotAvailable({ what, why }: { what: string; why: string }) {
  return (
    <div className="banner banner-muted" role="status">
      <strong>{what}: not available on this server.</strong> <span>{why}</span>
    </div>
  );
}

const EXACTNESS_MARK: Record<string, string> = { exact: "=", bounded: "≤", approximate: "≈" };

/** Exactness never relies on colour alone: the badge carries a symbol and the word. */
export function ExactnessBadge({ exactness }: { exactness: string | undefined }) {
  if (!exactness) return <span className="badge badge-unknown">exactness not reported</span>;
  return (
    <span className={`badge badge-${displayText(exactness)}`} data-testid="exactness-badge">
      {EXACTNESS_MARK[exactness] ?? "?"} {displayText(exactness)}
    </span>
  );
}

const CONTRACT_MARK: Record<string, string> = { passed: "✓", stripped: "✂", abstained: "∅" };

export function ContractBadge({ status }: { status: string }) {
  return (
    <span className={`badge contract-${displayText(status)}`} data-testid="contract-badge">
      {CONTRACT_MARK[status] ?? "?"} contract {displayText(status)}
    </span>
  );
}

export function KeyValues({ rows }: { rows: Array<[string, unknown]> }) {
  return (
    <dl className="kv">
      {rows.map(([k, v]) => (
        <div key={k}>
          <dt>{k}</dt>
          <dd>
            <Text value={v === undefined || v === null || v === "" ? "—" : v} max={400} />
          </dd>
        </div>
      ))}
    </dl>
  );
}

export function Tabs<T extends string>({ tabs, active, onChange, label }: { tabs: Array<{ id: T; label: string; count?: number }>; active: T; onChange: (id: T) => void; label: string }) {
  return (
    <div className="tabs" role="tablist" aria-label={label}>
      {tabs.map((t) => (
        <button key={t.id} type="button" role="tab" aria-selected={t.id === active} className={t.id === active ? "tab active" : "tab"} onClick={() => onChange(t.id)}>
          {t.label}
          {t.count !== undefined ? <span className="count">{t.count}</span> : null}
        </button>
      ))}
    </div>
  );
}

export function JsonBlock({ value }: { value: unknown }) {
  return <Pre value={JSON.stringify(value, null, 2)} max={20000} />;
}
