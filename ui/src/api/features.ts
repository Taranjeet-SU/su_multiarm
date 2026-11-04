// Feature detection. Optional routes (the A2 inspect API) and tools (the L5 log tools) are used only
// when the server advertises them: routes through FastAPI's ``/openapi.json`` path list, tools through
// ``GET /v2/tools``. Absent means "not available", shown as such; never replaced by sample data.
import { z } from "zod";
import { ApiError, type ApiClient } from "./client";

export interface Features {
  routes: ReadonlySet<string>; // "GET /v2/info", "POST /v2/graph/neighbourhood", ...
  routesKnown: boolean; // false when the route list could not be read
  tools: ReadonlySet<string>;
  toolsError: string | null; // e.g. the principal lacks the tools scope
  toolSchemas: ReadonlyMap<string, Record<string, unknown>>; // input JSON Schema per tool (GET /v2/tools)
}

const OpenApi = z.object({ paths: z.record(z.string(), z.record(z.string(), z.unknown())) }).passthrough();
const ToolList = z.object({ tools: z.array(z.object({ name: z.string(), input_schema: z.record(z.string(), z.unknown()).optional() }).passthrough()) }).passthrough();

export const EMPTY_FEATURES: Features = { routes: new Set(), routesKnown: false, tools: new Set(), toolsError: null, toolSchemas: new Map() };

export async function loadFeatures(api: ApiClient, signal?: AbortSignal): Promise<Features> {
  const routes = new Set<string>();
  let routesKnown = false;
  try {
    const spec = await api.get("/openapi.json", OpenApi, signal);
    for (const [path, methods] of Object.entries(spec.paths)) for (const method of Object.keys(methods)) routes.add(`${method.toUpperCase()} ${path}`);
    routesKnown = true;
  } catch (exc) {
    if (!(exc instanceof ApiError) || exc.kind !== "not_found") throw exc;
  }
  let tools = new Set<string>();
  let toolsError: string | null = null;
  const toolSchemas = new Map<string, Record<string, unknown>>();
  try {
    const listed = (await api.get("/v2/tools", ToolList, signal)).tools;
    tools = new Set(listed.map((t) => t.name));
    for (const t of listed) if (t.input_schema) toolSchemas.set(t.name, t.input_schema);
  } catch (exc) {
    if (exc instanceof ApiError && (exc.kind === "forbidden" || exc.kind === "not_found")) toolsError = exc.message;
    else throw exc;
  }
  return { routes, routesKnown, tools, toolsError, toolSchemas };
}

export function hasRoute(features: Features, method: string, path: string): boolean {
  return features.routes.has(`${method.toUpperCase()} ${path}`);
}
