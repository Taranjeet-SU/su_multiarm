// The checked answer (the ``done`` payload only): text with inline citation chips, the contract
// status and its violations, the numbers behind the answer with their exactness and the server's
// rendering sentence, and the history notes of the generation. Text nodes only.
import type { AnswerResult, Violation } from "../api/schema";
import { displayText } from "../lib/text";
import { ContractBadge, ExactnessBadge, Text } from "./common";
import { UnsupportedList } from "./EvidenceTabs";

/** Splits ``text`` into plain parts and the citation ids it mentions verbatim (longest id first). */
export function citationParts(text: string, ids: string[]): Array<{ text: string; cite: string | null }> {
  const sorted = [...new Set(ids.filter((id) => id.length >= 3))].sort((a, b) => b.length - a.length);
  const parts: Array<{ text: string; cite: string | null }> = [];
  let rest = text;
  while (rest.length) {
    let best = -1;
    let id = "";
    for (const candidate of sorted) {
      const at = rest.indexOf(candidate);
      if (at >= 0 && (best < 0 || at < best)) {
        best = at;
        id = candidate;
      }
    }
    if (best < 0) {
      parts.push({ text: rest, cite: null });
      break;
    }
    if (best > 0) parts.push({ text: rest.slice(0, best), cite: null });
    parts.push({ text: id, cite: id });
    rest = rest.slice(best + id.length);
  }
  return parts;
}

export function AnswerCard({ result, onCite, history }: { result: AnswerResult; onCite: (id: string) => void; history: Record<string, unknown> | null }) {
  const { answer } = result;
  const order = new Map(answer.citations.map((c, i) => [c, i + 1]));
  const parts = citationParts(displayText(answer.answer), answer.citations);
  const numbers = result.retrieval.aggregates.filter((a) => a.exactness || a.rendering);
  const libraries = Array.isArray(history?.library_identities) ? (history?.library_identities as unknown[]).length : 0;
  const archived = Array.isArray(history?.archived_days) ? (history?.archived_days as unknown[]).length : null;
  return (
    <section className="answer" aria-label="answer" data-testid="answer-card">
      <div className="card-head">
        <h2>Answer</h2>
        <ContractBadge status={result.contract} />
        {answer.insufficient_evidence ? <span className="badge badge-unknown">insufficient evidence</span> : null}
      </div>
      {result.contract !== "passed" ? <ViolationList status={result.contract} violations={result.violations} /> : null}
      <p className="answer-text" data-testid="answer-text">
        {parts.map((p, i) =>
          p.cite ? (
            <button type="button" key={i} className="chip cite cite-inline" onClick={() => onCite(p.cite!)} title={p.cite}>
              [{order.get(p.cite) ?? "·"}] {displayText(p.cite)}
            </button>
          ) : (
            <span key={i}>{p.text}</span>
          ),
        )}
      </p>
      {answer.short_answer ? (
        <p>
          <span className="label">Short answer</span> <Text value={answer.short_answer} max={2000} />
        </p>
      ) : null}
      {numbers.length ? (
        <div data-testid="answer-numbers">
          {numbers.map((a, i) => (
            <div className="rendering" key={a.id ?? i}>
              <ExactnessBadge exactness={a.exactness} />{" "}
              {a.rendering ? <Text value={a.rendering} max={1000} /> : <span>{displayText(a.operation)}{a.field ? ` by ${displayText(a.field)}` : ""}: denominator {displayText(a.denominator)}</span>}
              {a.id ? (
                <button type="button" className="chip cite cite-inline" onClick={() => onCite(a.id!)}>
                  {displayText(a.id)}
                </button>
              ) : null}
            </div>
          ))}
        </div>
      ) : null}
      {answer.citations.length ? (
        <div className="chips" aria-label="citations">
          <span className="label">Sources</span>
          {answer.citations.map((id, i) => (
            <button type="button" className="chip cite" key={`${id}-${i}`} onClick={() => onCite(id)}>
              [{i + 1}] {displayText(id)}
            </button>
          ))}
        </div>
      ) : null}
      {answer.claims.length ? (
        <details>
          <summary>{answer.claims.length} claim(s) and their evidence</summary>
          <ul className="claims">
            {answer.claims.map((c, i) => (
              <li key={i}>
                <span className="badge">{displayText(c.kind)}</span> <Text value={c.text} max={1000} />{" "}
                {c.evidence_ids.map((id) => (
                  <button type="button" className="chip cite cite-inline" key={id} onClick={() => onCite(id)}>
                    [{order.get(id) ?? "·"}]
                  </button>
                ))}
              </li>
            ))}
          </ul>
        </details>
      ) : null}
      {result.unsupported_constraints.length ? <UnsupportedList items={result.unsupported_constraints} /> : null}
      {result.coverage ? (
        <p className="banner banner-warn">
          Coverage ({displayText(result.coverage.policy)}): {result.coverage.documents_covered} of {result.coverage.documents_total} documents covered
          {result.coverage.note ? `. ${displayText(result.coverage.note)}` : ""}
        </p>
      ) : null}
      {libraries > 1 ? (
        <p className="banner banner-warn" data-testid="answer-multi-library">
          The served days span {libraries} pattern-library identities: a change between days can come from the library, not only the logs.
        </p>
      ) : null}
      {archived ? <p className="muted small">History: {archived} archived day(s) are kept outside the served window.</p> : null}
    </section>
  );
}

function ViolationList({ status, violations }: { status: string; violations: Violation[] }) {
  return (
    <div className="banner banner-warn" role="note" data-testid="violations">
      <strong>{status === "abstained" ? "The answer contract abstained." : "The answer contract removed unbacked content."}</strong>
      <ul>
        {violations.map((v, i) => (
          <li key={i}>
            <code>{displayText(v.rule)}</code> ({displayText(v.action)}): <Text value={v.detail} max={600} />
          </li>
        ))}
      </ul>
    </div>
  );
}
