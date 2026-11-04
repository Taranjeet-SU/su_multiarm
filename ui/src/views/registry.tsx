// The view registry: the single extension point for new screens (log analytics, patterns, incidents,
// dashboards). A view declares when it is available from the server's advertised features; an
// unavailable view stays in the navigation, marked, and renders a "not available" notice.
import { lazy, Suspense, type ReactNode } from "react";
import { hasRoute, type Features } from "../api/features";
import { AskView } from "./AskView";
import { ChartsView } from "./ChartsView";
import { CommunitiesView, RunsView } from "./OpsViews";
import { PatternsView } from "./PatternsView";
import { hasLogTools, LogsView } from "./LogsView";
import { DashboardView } from "./DashboardView";
import { SettingsView } from "./SettingsView";

// cytoscape is loaded only when the graph explorer opens (a separate hashed chunk).
const GraphView = lazy(() => import("./GraphView").then((m) => ({ default: m.GraphView })));

export interface ViewDef {
  id: string;
  label: string;
  render: (params: Record<string, string>) => ReactNode;
  /** ``null`` when available, else the reason shown to the user. */
  unavailable?: (features: Features) => string | null;
  /** Not listed in the navigation at all (rev 3: overview tiles stay hidden until the log tools exist). */
  hidden?: (features: Features) => boolean;
}

/** Available only when the server advertises the route (never a sample payload instead). */
function needs(method: string, path: string): (features: Features) => string | null {
  return (features) =>
    !features.routesKnown ? "the server's route list is not readable" : hasRoute(features, method, path) ? null : `this server has no ${method} ${path} route`;
}

export const VIEWS: ViewDef[] = [
  { id: "overview", label: "Overview", render: () => <DashboardView />, hidden: (f) => !hasLogTools(f), unavailable: (f) => (hasLogTools(f) ? null : "this server has no log_* tools") },
  { id: "ask", label: "Ask", render: (p) => <AskView initialQuestion={p.q ?? ""} initialAsOf={p.as_of ?? ""} /> },
  {
    id: "graph",
    label: "Graph explorer",
    render: () => (
      <Suspense fallback={<p>Loading graph explorer…</p>}>
        <GraphView />
      </Suspense>
    ),
  },
  { id: "charts", label: "Charts", render: () => <ChartsView /> },
  { id: "logs", label: "Log shortcuts", render: () => <LogsView />, hidden: (f) => !hasLogTools(f), unavailable: (f) => (hasLogTools(f) ? null : "this server has no log_* tools") },
  { id: "patterns", label: "Patterns", render: () => <PatternsView />, unavailable: needs("GET", "/v2/patterns/review") },
  { id: "communities", label: "Communities", render: () => <CommunitiesView />, unavailable: needs("GET", "/v2/communities") },
  { id: "runs", label: "Runs & spend", render: () => <RunsView />, unavailable: needs("GET", "/v2/runs") },
  { id: "settings", label: "Settings", render: () => <SettingsView /> },
];

export function registerView(view: ViewDef, before = "settings"): void {
  if (VIEWS.some((v) => v.id === view.id)) return;
  const at = VIEWS.findIndex((v) => v.id === before);
  VIEWS.splice(at < 0 ? VIEWS.length : at, 0, view);
}
