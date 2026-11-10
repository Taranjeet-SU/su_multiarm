// The checked answer (the ``done`` payload only): text with inline citation chips, the contract
// status and its violations, the numbers behind the answer with their exactness and the server's
// rendering sentence, and the history notes of the generation. Markdown uses safe React elements.
import { AnswerMarkdown } from "./AnswerMarkdown";
import { AnswerRows } from "./AnswerRows";
import type { AnswerResult, Violation } from "../api/schema";
import { displayText } from "../lib/text";
import { ContractBadge, ExactnessBadge, Text } from "./common";
import { UnsupportedList } from "./EvidenceTabs";

export { citationParts } from "../lib/citations";

export function AnswerCard({ result, onCite, history }: { result: AnswerResult; onCite: (id: string) => void; history: Record<string, unknown> | null }) {
  const { answer } = result;
  const order = new Map(answer.citations.map((c, i) => [c, i + 1]));
  const numbers = result.retrieval.aggregates.filter((a) => a.exactness || a.rendering);
  const issueTable = result.contract !== "abstained" && !answer.insufficient_evidence && numbers.length > 0 && numbers.every((a) => a.id && answer.citations.includes(a.id) && a.rows?.length && a.rows.every((r) => "error_type" in r && "error_count" in r));
  const libraries = Array.isArray(history?.library_identities) ? (history?.library_identities as unknown[]).length : 0;
  const archived = Array.isArray(history?.archived_days) ? (history?.archived_days as unknown[]).length : null;
  return (
    <section className="answer" aria-label="answer" data-testid="answer-card">
      <div className="card-head">
        <h2>Answer</h2>
        {answer.insufficient_evidence ? <span className="badge badge-unknown">insufficient evidence</span> : null}
      </div>
      {result.contract !== "passed" ? <ViolationList status={result.contract} violations={result.violations} /> : null}
      {issueTable ? <div data-testid="answer-text">
        {numbers.map((a, i) => <div key={a.id ?? i}>
          <AnswerRows aggregate={a} />
          <p><Text value={a.rendering} /></p>
          <details>
            <summary>Supporting log patterns and count limitations</summary>
            {(a.rows ?? []).map((row, j) => <p key={j}><strong>{[row.tenant, row.service].filter((v) => v != null).map(displayText).join(" · ")} · {displayText(row.error_type)}</strong><br /><Text value={row.supporting_template ?? "No supporting template available."} max={600} /></p>)}
            {Array.isArray(a.bounded_by) ? a.bounded_by.map((note, j) => <p key={j}><Text value={note} max={1000} /></p>) : null}
          </details>
        </div>)}
      </div> : <AnswerMarkdown text={answer.answer} citations={answer.citations} onCite={onCite} />}
      {answer.short_answer ? (
        <p>
          <span className="label">Short answer</span> <Text value={answer.short_answer} max={2000} />
        </p>
      ) : null}
      {numbers.length && !issueTable ? (
        <div data-testid="answer-numbers">
          {numbers.map((a, i) => (
            <div className="rendering" key={a.id ?? i}>
              <ExactnessBadge exactness={a.exactness} />{" "}
              {a.rendering ? <Text value={a.rendering} max={1000} /> : <span>{displayText(a.operation)}{a.field ? ` by ${displayText(a.field)}` : ""}: denominator {displayText(a.denominator)}</span>}
              {result.contract !== "abstained" && a.id && answer.citations.includes(a.id) ? <AnswerRows aggregate={a} /> : null}
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
      <details><summary>Answer diagnostics</summary><ContractBadge status={result.contract} /><p>Citation and count checks do not establish that the answer fully addresses the question.</p></details>
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
      <strong>{status === "abstained" ? "There is not enough verified evidence to answer." : "Content without supporting evidence was removed."}</strong>
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
