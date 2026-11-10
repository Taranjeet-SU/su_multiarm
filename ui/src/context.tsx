import { createContext, useContext } from "react";
import type { ApiClient } from "./api/client";
import type { Features } from "./api/features";
import type { AnswerResult, Capabilities, SearchResult, UiSettings } from "./api/schema";

export interface AppState {
  api: ApiClient;
  settings: UiSettings;
  capabilities: Capabilities | null;
  features: Features;
  /** The last authoritative result of the Ask view (``done`` payload), shared with Graph/Charts/Trace. */
  lastAnswer: AnswerResult | null;
  setLastAnswer: (result: AnswerResult | null) => void;
  /** The last search result run by another view (charts, graph), for the trace panel. */
  lastSearch: SearchResult | null;
  setLastSearch: (result: SearchResult | null) => void;
  navigate: (route: string, params?: Record<string, string>) => void;
}

export const AppContext = createContext<AppState | null>(null);

export function useApp(): AppState {
  const state = useContext(AppContext);
  if (!state) throw new Error("AppContext missing");
  return state;
}
