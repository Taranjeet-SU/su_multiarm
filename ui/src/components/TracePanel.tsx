import { summarise } from "../lib/trace";
import { displayText, formatNumber, formatSeconds, formatUsd } from "../lib/text";
import { JsonBlock, KeyValues } from "./common";

export function TracePanel({ answerTrace, retrievalTrace }: { answerTrace?: unknown; retrievalTrace: unknown }) {
  const t = summarise(answerTrace, retrievalTrace);
  const d = t.decision;
  return (
    <section className="trace" aria-label="trace">
      <h3>Decision</h3>
      <KeyValues
        rows={[
          ["pipeline", t.pipeline],
          ["mode", t.mode],
          ["agent", d.agent],
          ["outcome", d.outcome],
          ["policy applied", d.policy_applied],
          ["decision fallback", d.decision_fallback === undefined ? undefined : String(d.decision_fallback)],
          ["failure", d.failure],
          ["intents", Array.isArray(d.intents) ? (d.intents.length ? d.intents.join(", ") : "none") : undefined],
          ["stop reason", t.stopReason],
          ["trace id", t.traceId],
        ]}
      />
      <h3>Timings</h3>
      <KeyValues
        rows={[
          ["total", formatSeconds(t.totalSeconds)],
          ["retrieval", formatSeconds(t.retrievalSeconds)],
          ["decision", formatSeconds(t.decisionSeconds)],
        ]}
      />
      <h3>Arms fired ({t.armsFired.length ? t.armsFired.join(", ") : "none"})</h3>
      <table className="data">
        <thead>
          <tr>
            <th scope="col">round</th>
            <th scope="col">arm</th>
            <th scope="col" className="num">documents</th>
            <th scope="col" className="num">time</th>
            <th scope="col">aggregate</th>
            <th scope="col">incomplete</th>
          </tr>
        </thead>
        <tbody>
          {t.steps.map((s, i) => (
            <tr key={i}>
              <td>{s.round}</td>
              <td>{displayText(s.arm)}</td>
              <td className="num">{s.documents === null ? "—" : formatNumber(s.documents)}</td>
              <td className="num">{formatSeconds(s.seconds)}</td>
              <td>{s.aggregate ? "yes" : "no"}</td>
              <td>{s.incomplete ? "yes" : "no"}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <h3>Model usage and spend</h3>
      {t.calls.length ? (
        <>
          <table className="data" data-testid="model-calls">
            <thead>
              <tr>
                <th scope="col">stage</th>
                <th scope="col">provider / model</th>
                <th scope="col">ok</th>
                <th scope="col" className="num">in tokens</th>
                <th scope="col" className="num">out tokens</th>
                <th scope="col" className="num">time</th>
                <th scope="col" className="num">USD</th>
              </tr>
            </thead>
            <tbody>
              {t.calls.map((c, i) => (
                <tr key={i}>
                  <td>{displayText(c.stage)}</td>
                  <td>
                    {displayText(c.provider)} / {displayText(c.model)}
                  </td>
                  <td>{c.success === null ? "—" : c.success ? "yes" : `no${c.errorCategory ? ` (${displayText(c.errorCategory)})` : ""}`}</td>
                  <td className="num">{c.inputTokens === null ? "not reported" : formatNumber(c.inputTokens)}</td>
                  <td className="num">{c.outputTokens === null ? "not reported" : formatNumber(c.outputTokens)}</td>
                  <td className="num">{formatSeconds(c.seconds)}</td>
                  <td className="num">{formatUsd(c.usd)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p>
            Spend: <strong>{formatUsd(t.usdTotal)}</strong>
            {t.callsWithoutPrice ? <span className="muted"> (plus {t.callsWithoutPrice} call(s) without a reported price: not included)</span> : null}
          </p>
        </>
      ) : (
        <p className="muted">No model calls in this run (spend USD 0).</p>
      )}
      <details>
        <summary>raw trace</summary>
        <JsonBlock value={{ answer: answerTrace ?? null, retrieval: retrievalTrace }} />
      </details>
    </section>
  );
}
