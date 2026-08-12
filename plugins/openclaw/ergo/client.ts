/**
 * Thin HTTP client for the Ergo API, independently written for this plugin.
 *
 * DELIBERATELY not shared with `ergo/mcp/server.py` or the Hermes plugin's
 * `_client.py`: this ships into an OpenClaw installation on Node, a
 * different runtime than this repo's own Python venv, with no code-sharing
 * boundary between the two languages. Mirrors the shape and 409-handling of
 * both (same `_GUARDED_OK` treatment) so all three stay easy to eyeball-diff
 * if the HTTP API changes.
 *
 * 409 (conflict) is NOT an error for the guarded write routes
 * (`/remember`, `/learn`, `/supersede`) -- it is a documented outcome
 * returned as a normal body so the caller can surface the conflict to the
 * agent. Every other non-2xx throws `ErgoError`.
 */

export const DEFAULT_API_URL = "http://127.0.0.1:8788";
export const DEFAULT_TIMEOUT_MS = 30_000;
// Auto-recall runs synchronously before every prompt build -- fail fast
// rather than stalling the user's turn on an unreachable or slow engine.
export const RECALL_HOOK_TIMEOUT_MS = 4_000;

export type QueryValue = string | number | boolean | undefined;
export type JsonRecord = Record<string, unknown>;

export class ErgoError extends Error {
  readonly statusCode: number;
  readonly body: unknown;

  constructor(statusCode: number, body: unknown) {
    super(`ergo HTTP ${statusCode}: ${safeStringify(body)}`);
    this.name = "ErgoError";
    this.statusCode = statusCode;
    this.body = body;
  }
}

function safeStringify(value: unknown): string {
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

export interface ErgoClientOptions {
  baseUrl: string;
  apiToken?: string;
  timeoutMs?: number;
  /** Injectable for tests; defaults to the global `fetch`. */
  fetchImpl?: typeof fetch;
}

interface RequestOptions {
  query?: Record<string, QueryValue>;
  body?: JsonRecord;
  auth?: boolean;
  /** HTTP statuses treated as a normal (non-throwing) response body. */
  okStatuses?: readonly number[];
  timeoutMs?: number;
}

const OK = [200] as const;
// 409 is a documented outcome of the guarded write paths, not a failure.
const GUARDED_OK = [200, 409] as const;

export class ErgoClient {
  readonly baseUrl: string;
  private readonly apiToken?: string;
  private readonly timeoutMs: number;
  private readonly fetchImpl: typeof fetch;

  constructor(options: ErgoClientOptions) {
    this.baseUrl = options.baseUrl.replace(/\/+$/, "");
    this.apiToken = options.apiToken;
    this.timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
    this.fetchImpl = options.fetchImpl ?? fetch;
  }

  private headers(auth: boolean): Record<string, string> {
    const headers: Record<string, string> = { "Content-Type": "application/json" };
    if (auth && this.apiToken) headers.Authorization = `Bearer ${this.apiToken}`;
    return headers;
  }

  private async request(method: string, path: string, opts: RequestOptions = {}): Promise<JsonRecord> {
    const url = new URL(this.baseUrl + path);
    for (const [key, value] of Object.entries(opts.query ?? {})) {
      if (value !== undefined) url.searchParams.set(key, String(value));
    }
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), opts.timeoutMs ?? this.timeoutMs);
    let response: Response;
    try {
      response = await this.fetchImpl(url.toString(), {
        method,
        headers: this.headers(opts.auth ?? true),
        body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
        signal: controller.signal,
      });
    } finally {
      clearTimeout(timer);
    }

    const text = await response.text();
    let parsed: unknown;
    try {
      parsed = text.length > 0 ? JSON.parse(text) : {};
    } catch {
      parsed = { data: text };
    }

    const okStatuses = opts.okStatuses ?? OK;
    if (okStatuses.includes(response.status)) {
      return isRecord(parsed) ? parsed : { data: parsed };
    }
    throw new ErgoError(response.status, parsed);
  }

  health(): Promise<JsonRecord> {
    return this.request("GET", "/health", { auth: false });
  }

  remember(payload: JsonRecord): Promise<JsonRecord> {
    return this.request("POST", "/remember", { body: payload, okStatuses: GUARDED_OK });
  }

  learn(payload: JsonRecord): Promise<JsonRecord> {
    return this.request("POST", "/learn", { body: payload, okStatuses: GUARDED_OK });
  }

  ingest(payload: JsonRecord): Promise<JsonRecord> {
    return this.request("POST", "/ingest", { body: payload });
  }

  supersede(payload: JsonRecord): Promise<JsonRecord> {
    return this.request("POST", "/supersede", { body: payload, okStatuses: GUARDED_OK });
  }

  retract(payload: JsonRecord): Promise<JsonRecord> {
    return this.request("POST", "/retract", { body: payload });
  }

  recall(params: Record<string, QueryValue>, timeoutMs?: number): Promise<JsonRecord> {
    return this.request("GET", "/recall", { query: params, timeoutMs });
  }

  why(params: Record<string, QueryValue>): Promise<JsonRecord> {
    return this.request("GET", "/why", { query: params });
  }

  active(params: Record<string, QueryValue>): Promise<JsonRecord> {
    return this.request("GET", "/active", { query: params });
  }

  history(params: Record<string, QueryValue>): Promise<JsonRecord> {
    return this.request("GET", "/history", { query: params });
  }

  diagnose(): Promise<JsonRecord> {
    return this.request("GET", "/diagnose", {});
  }
}

function isRecord(value: unknown): value is JsonRecord {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
