import { useState, type FormEvent } from "react";
import { KeyValues, Text } from "../components/common";
import { useApp } from "../context";
import { displayText } from "../lib/text";

export function TokenForm({ onSaved }: { onSaved?: () => void }) {
  const { api, settings } = useApp();
  const [value, setValue] = useState("");
  const submit = (e: FormEvent) => {
    e.preventDefault();
    api.tokens.set(value);
    setValue("");
    onSaved?.();
  };
  return (
    <form onSubmit={submit} className="row" aria-label="bearer token">
      <label htmlFor="token">Bearer token</label>
      <input id="token" type="password" autoComplete="off" value={value} onChange={(e) => setValue(e.target.value)} />
      <button type="submit" disabled={!value.trim()}>
        Use token
      </button>
      <button type="button" onClick={() => api.tokens.set(null)}>
        Forget token
      </button>
      <span className="muted small">{settings.session_storage_token ? "kept in this tab's session storage" : "kept in memory only (lost on reload)"}</span>
    </form>
  );
}

export function SettingsView() {
  const { capabilities, features, settings } = useApp();
  const dataset = capabilities?.datasets[0];
  return (
    <div className="view settings">
      <h2>Authentication</h2>
      <p className="muted">Server auth: {displayText(settings.auth)}</p>
      <TokenForm />
      <h2>Deployment</h2>
      {capabilities ? (
        <>
          <KeyValues
            rows={[
              ["dataset", dataset?.name],
              ["active generation", dataset?.generation ?? "none (no published generation)"],
              ["profile", dataset?.profile],
              ["default pipeline", capabilities.default_pipeline],
              ["token streaming", capabilities.answer_streaming ? String(capabilities.answer_streaming.tokens) : undefined],
            ]}
          />
          <h3>Pipelines</h3>
          <ul>
            {Object.entries(capabilities.pipelines).map(([name, p]) => (
              <li key={name}>
                <strong>{displayText(name)}</strong>: arms {displayText(p.arms.join(", "))}; modes {displayText(p.modes.join(", "))}
              </li>
            ))}
          </ul>
          <h3>Fields</h3>
          <table className="data">
            <thead>
              <tr>
                <th scope="col">name</th>
                <th scope="col">type</th>
                <th scope="col">filter</th>
                <th scope="col">list</th>
                <th scope="col">group</th>
                <th scope="col">sort</th>
              </tr>
            </thead>
            <tbody>
              {capabilities.fields.map((f) => (
                <tr key={f.name}>
                  <td>{displayText(f.name)}</td>
                  <td>{displayText(f.type)}</td>
                  <td>{f.filterable ? "yes" : ""}</td>
                  <td>{f.listable ? "yes" : ""}</td>
                  <td>{f.groupable ? "yes" : ""}</td>
                  <td>{f.sortable ? "yes" : ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      ) : (
        <p className="muted">Capabilities not loaded.</p>
      )}
      <h2>Server features</h2>
      <p className="muted small">Optional routes and tools are used only when the server advertises them.</p>
      <KeyValues
        rows={[
          ["route list", features.routesKnown ? `${features.routes.size} routes` : "not readable (optional views are disabled)"],
          ["tools", features.tools.size ? [...features.tools].sort().join(", ") : features.toolsError ?? "none"],
        ]}
      />
      <details>
        <summary>routes</summary>
        <ul className="small">
          {[...features.routes].sort().map((r) => (
            <li key={r}>
              <Text value={r} />
            </li>
          ))}
        </ul>
      </details>
    </div>
  );
}
