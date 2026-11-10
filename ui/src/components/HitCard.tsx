import type { Hit } from "../api/schema";
import { clip, displayText, formatNumber } from "../lib/text";
import { SafeLink, Text } from "./common";

/** Splits ``text`` around case-insensitive occurrences of ``terms``; every part stays a text node. */
export function highlightParts(text: string, terms: string[]): Array<{ text: string; match: boolean }> {
  const usable = [...new Set(terms.map((t) => t.toLowerCase()).filter((t) => t.length >= 3))];
  if (!usable.length) return [{ text, match: false }];
  const lower = text.toLowerCase();
  const parts: Array<{ text: string; match: boolean }> = [];
  let i = 0;
  while (i < text.length) {
    let best = -1;
    let bestLen = 0;
    for (const term of usable) {
      const at = lower.indexOf(term, i);
      if (at >= 0 && (best < 0 || at < best || (at === best && term.length > bestLen))) {
        best = at;
        bestLen = term.length;
      }
    }
    if (best < 0) {
      parts.push({ text: text.slice(i), match: false });
      break;
    }
    if (best > i) parts.push({ text: text.slice(i, best), match: false });
    parts.push({ text: text.slice(best, best + bestLen), match: true });
    i = best + bestLen;
  }
  return parts;
}

export function queryTerms(query: string): string[] {
  return query.split(/[^\p{L}\p{N}_.-]+/u).filter(Boolean);
}

export function HitCard({ hit, rank, terms = [], flash = false }: { hit: Hit; rank: number; terms?: string[]; flash?: boolean }) {
  const text = clip(hit.text, 3000);
  return (
    <article className={flash ? "hit flash" : "hit"} id={`ev-${hit.document_id}`} data-evidence-id={hit.document_id} tabIndex={-1}>
      <header>
        <span className="rank">#{rank}</span> <strong>{clip(hit.title || hit.document_id, 300)}</strong>
        <span className="muted"> · score {formatNumber(hit.score)} · arms {displayText(hit.arms.join(", "))}</span>
      </header>
      <div className="muted small">
        id <code>{displayText(hit.document_id)}</code> · source <Text value={hit.source} max={100} />
        {hit.url ? (
          <>
            {" · "}
            <SafeLink url={hit.url} />
          </>
        ) : null}
      </div>
      <p className="hit-text">
        {highlightParts(text, terms).map((p, i) => (p.match ? <mark key={i}>{p.text}</mark> : <span key={i}>{p.text}</span>))}
      </p>
      {hit.provenance.length ? (
        <details>
          <summary>provenance ({hit.provenance.length})</summary>
          <ul className="provenance">
            {hit.provenance.slice(0, 50).map((row, i) => (
              <li key={i}>
                {["field", "start", "end", "entity", "subject", "predicate", "object", "edge_kind", "support", "match_level", "depth"]
                  .filter((k) => row[k] !== undefined && row[k] !== null)
                  .map((k) => `${k}=${displayText(row[k])}`)
                  .join(" · ")}
                {typeof row.quote === "string" ? (
                  <blockquote>
                    <Text value={row.quote} max={600} />
                  </blockquote>
                ) : null}
              </li>
            ))}
          </ul>
        </details>
      ) : null}
    </article>
  );
}
