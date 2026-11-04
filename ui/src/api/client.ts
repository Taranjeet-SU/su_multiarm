// The only module that talks to the server. Same origin: every path is ``/v2/...`` relative to the
// page's origin. The bearer token lives in memory (``TokenStore``); it is copied to
// ``sessionStorage`` only when the server's UI settings allow it, never to ``localStorage`` or a
// cookie. There is no retry on 4xx and no fallback data: an error is surfaced as an ``ApiError``.
import type { z } from "zod";

type Out<S extends z.ZodTypeAny> = z.output<S>;
import { SseParser } from "./sse";
import { StreamEvent } from "./schema";

export type ErrorKind =
  | "unauthenticated"
  | "forbidden"
  | "not_found"
  | "conflict"
  | "rejected"
  | "budget"
  | "cancelled"
  | "provider"
  | "unavailable"
  | "deadline"
  | "bad_response"
  | "network"
  | "server";

const KIND_BY_STATUS: Record<number, ErrorKind> = {
  401: "unauthenticated",
  403: "forbidden",
  404: "not_found",
  409: "conflict",
  422: "rejected",
  429: "budget",
  499: "cancelled",
  502: "provider",
  503: "unavailable",
  504: "deadline",
};

export const ERROR_TITLES: Record<ErrorKind, string> = {
  unauthenticated: "Authentication required (401): enter the bearer token",
  forbidden: "Not permitted (403): the principal lacks the scope for this request",
  not_found: "Not found (404)",
  conflict: "Conflict (409)",
  rejected: "Request rejected (422)",
  budget: "Budget exhausted (429)",
  cancelled: "Run cancelled (499)",
  provider: "Model provider failed (502)",
  unavailable: "Backend unavailable or no generation (503)",
  deadline: "Deadline exceeded (504)",
  bad_response: "Unexpected response shape from the server",
  network: "Network error: the server could not be reached",
  server: "Server error",
};

export class ApiError extends Error {
  readonly kind: ErrorKind;
  readonly status: number | null;
  readonly detail: string;

  constructor(kind: ErrorKind, status: number | null, detail: string) {
    super(`${ERROR_TITLES[kind]}${detail ? `: ${detail}` : ""}`);
    this.kind = kind;
    this.status = status;
    this.detail = detail;
  }
}

function detailText(body: unknown): string {
  if (body && typeof body === "object" && "detail" in body) {
    const detail = (body as { detail: unknown }).detail;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) return detail.map((d) => (d && typeof d === "object" && "msg" in d ? String((d as { msg: unknown }).msg) : JSON.stringify(d))).join("; ");
    return JSON.stringify(detail);
  }
  return "";
}

export async function errorFromResponse(response: Response): Promise<ApiError> {
  let body: unknown = null;
  try {
    body = await response.json();
  } catch {
    body = null;
  }
  const kind = KIND_BY_STATUS[response.status] ?? "server";
  return new ApiError(kind, response.status, detailText(body).slice(0, 2000));
}

const SESSION_KEY = "su_multi_arm.ui.token";

export class TokenStore {
  private token: string | null = null;
  private persist = false;
  private listeners = new Set<() => void>();

  configure(sessionStorageAllowed: boolean): void {
    this.persist = sessionStorageAllowed;
    if (this.persist && this.token === null) {
      try {
        this.token = window.sessionStorage.getItem(SESSION_KEY);
      } catch {
        this.token = null;
      }
    }
    if (!this.persist) this.clearSession();
  }

  get(): string | null {
    return this.token;
  }

  set(token: string | null): void {
    this.token = token && token.trim() ? token.trim() : null;
    if (this.persist && this.token) {
      try {
        window.sessionStorage.setItem(SESSION_KEY, this.token);
      } catch {
        /* storage unavailable: the token stays in memory only */
      }
    } else {
      this.clearSession();
    }
    this.listeners.forEach((l) => l());
  }

  subscribe(listener: () => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  private clearSession(): void {
    try {
      window.sessionStorage.removeItem(SESSION_KEY);
    } catch {
      /* nothing stored */
    }
  }
}

export interface StreamHandlers {
  onEvent: (event: StreamEvent) => void;
}

export class ApiClient {
  constructor(
    readonly tokens: TokenStore,
    private readonly base: string = "",
    private readonly fetchImpl: typeof fetch = (...args) => fetch(...args),
  ) {}

  private headers(json: boolean): Headers {
    const headers = new Headers({ Accept: json ? "application/json" : "text/event-stream" });
    if (json) headers.set("Content-Type", "application/json");
    const token = this.tokens.get();
    if (token) headers.set("Authorization", `Bearer ${token}`);
    return headers;
  }

  private async send(method: string, path: string, body?: unknown, signal?: AbortSignal): Promise<Response> {
    let response: Response;
    try {
      response = await this.fetchImpl(`${this.base}${path}`, {
        method,
        headers: this.headers(true),
        body: body === undefined ? undefined : JSON.stringify(body),
        signal,
        credentials: "omit",
        cache: "no-store",
        redirect: "error",
      });
    } catch (exc) {
      if (exc instanceof DOMException && exc.name === "AbortError") throw new ApiError("cancelled", null, "stopped by the user");
      throw new ApiError("network", null, exc instanceof Error ? exc.message : "");
    }
    if (!response.ok) throw await errorFromResponse(response);
    return response;
  }

  async json<S extends z.ZodTypeAny>(method: string, path: string, schema: S, body?: unknown, signal?: AbortSignal): Promise<Out<S>> {
    const response = await this.send(method, path, body, signal);
    let payload: unknown;
    try {
      payload = await response.json();
    } catch {
      throw new ApiError("bad_response", response.status, "the body is not JSON");
    }
    const parsed = schema.safeParse(payload);
    if (!parsed.success) throw new ApiError("bad_response", response.status, parsed.error.issues.slice(0, 3).map((i) => `${i.path.join(".")}: ${i.message}`).join("; "));
    return parsed.data as Out<S>;
  }

  get<S extends z.ZodTypeAny>(path: string, schema: S, signal?: AbortSignal): Promise<Out<S>> {
    return this.json("GET", path, schema, undefined, signal);
  }

  post<S extends z.ZodTypeAny>(path: string, body: unknown, schema: S, signal?: AbortSignal): Promise<Out<S>> {
    return this.json("POST", path, schema, body, signal);
  }

  /** ``GET`` that maps a 404 to ``null``: feature detection of optional inspect routes. */
  async optional<S extends z.ZodTypeAny>(path: string, schema: S, signal?: AbortSignal): Promise<Out<S> | null> {
    try {
      return await this.get(path, schema, signal);
    } catch (exc) {
      if (exc instanceof ApiError && exc.kind === "not_found") return null;
      throw exc;
    }
  }

  /** ``POST /v2/answer/stream``: events in order until ``done`` or ``error``. Aborting ``signal``
   * closes the connection, which cancels the run and its budget on the server. */
  async stream(body: unknown, handlers: StreamHandlers, signal: AbortSignal): Promise<void> {
    let response: Response;
    try {
      response = await this.fetchImpl(`${this.base}/v2/answer/stream`, {
        method: "POST",
        headers: (() => {
          const h = this.headers(true);
          h.set("Accept", "text/event-stream");
          return h;
        })(),
        body: JSON.stringify(body),
        signal,
        credentials: "omit",
        cache: "no-store",
        redirect: "error",
      });
    } catch (exc) {
      if (exc instanceof DOMException && exc.name === "AbortError") throw new ApiError("cancelled", null, "stopped by the user");
      throw new ApiError("network", null, exc instanceof Error ? exc.message : "");
    }
    if (!response.ok) throw await errorFromResponse(response);
    if (!response.body) throw new ApiError("bad_response", response.status, "no stream body");
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    const parser = new SseParser();
    let terminal = false;
    try {
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        for (const message of parser.push(decoder.decode(value, { stream: true }))) {
          let raw: unknown;
          try {
            raw = JSON.parse(message.data);
          } catch {
            throw new ApiError("bad_response", response.status, `event ${message.event} is not JSON`);
          }
          const parsed = StreamEvent.safeParse(raw);
          if (!parsed.success) throw new ApiError("bad_response", response.status, `event ${message.event}: ${parsed.error.issues[0]?.message ?? "invalid"}`);
          handlers.onEvent(parsed.data);
          if (parsed.data.event === "done" || parsed.data.event === "error") terminal = true;
        }
      }
    } catch (exc) {
      if (exc instanceof DOMException && exc.name === "AbortError") throw new ApiError("cancelled", null, "stopped by the user");
      throw exc;
    } finally {
      reader.releaseLock();
    }
    if (!terminal) throw new ApiError("bad_response", response.status, "the stream ended without a terminal event");
  }
}
