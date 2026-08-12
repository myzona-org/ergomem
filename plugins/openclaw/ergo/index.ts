/**
 * Ergo memory plugin for the OpenClaw harness.
 *
 * Design decision: wire automatic RECALL, never wire
 * automatic CAPTURE. OpenClaw's own built-in auto-capture (`agent_end`,
 * phrase-triggered, capped ~3/turn) is the wrong shape for Ergo: it would
 * write reason-less claims that `/why` deliberately can't surface, and feed
 * conversation into the contradiction gate, producing spurious conflicts
 * against real decisions -- degrading the one feature Ergo exists for.
 * `autoRecall` (default true) is a read-only `GET /recall` before every
 * prompt build. `autoCapture` (default false) is this plugin's OWN opt-in
 * capture path, and when enabled it posts to the gate-free `POST /ingest`
 * -- never `/remember` -- mirroring the sibling Hermes plugin's
 * `ERGO_HERMES_CAPTURE_TURNS` opt-in (`plugins/memory/ergo/__init__.py`).
 *
 * Hook choice: `before_prompt_build`, not `before_agent_start`. Verified
 * against the published `openclaw` package's own type declarations
 * (`dist/hook-types-*.d.ts`, openclaw@2026.7.1-2): `before_agent_start` is
 * explicitly `@deprecated` there -- "Legacy compatibility hook that
 * combines both phases... Use before_model_resolve and before_prompt_build."
 * The third-party reference plugin (openclaw-memory-mem0) uses the
 * deprecated hook; the first-party memory-lancedb plugin uses
 * `before_prompt_build`, which is also the current, non-deprecated hook per
 * the SDK's own types -- that is the tiebreaker this plugin follows.
 *
 * Runtime entry shape: this file does NOT `import { definePluginEntry }
 * from "openclaw/plugin-sdk/plugin-entry"` at the top level. It instead
 * exports a plain object matching `DefinedPluginEntry` directly -- verified
 * against the published .d.ts to be the exact normalized shape
 * `definePluginEntry` itself produces (`{id, name, description,
 * configSchema, register, kind?, ...}`). `definePluginEntry` and
 * `buildJsonPluginConfigSchema` are both VALUE imports from the `openclaw`
 * package, and this repo's tests must run with no OpenClaw installation on
 * disk -- a top-level value import would fail module resolution the moment
 * a test imports this file. `openclaw` stays a peerDependency for its type
 * declarations only; nothing here requires it to be installed at runtime.
 */

import { Type } from "@sinclair/typebox";
import { ErgoClient, ErgoError, RECALL_HOOK_TIMEOUT_MS, type JsonRecord } from "./client.ts";
import {
  DEFAULT_RECALL_LIMIT,
  forceScopeRead,
  forceScopeWrite,
  isEnabled,
  projectForContext,
  resolveConfig,
  type RawErgoConfig,
  type ResolvedErgoConfig,
} from "./config.ts";

// Bounds the opt-in autoCapture path: a slow/unreachable engine backs up a
// bounded set of in-flight `/ingest` calls instead of growing unboundedly.
export const MAX_INFLIGHT_CAPTURES = 20;
// Best-effort drain budget on teardown; an unreachable engine must not hang
// gateway shutdown.
const CAPTURE_FLUSH_TIMEOUT_MS = 5_000;
// Appears in the auto-recall hook's injected text; skipped when scanning
// messages for autoCapture so recalled context never gets re-ingested.
const RECALL_MARKER = "Relevant Ergo memory";
const MIN_CAPTURE_CHARS = 50;

// --- minimal structural types for the OpenClaw plugin surface this file
// touches, written independently from the published `openclaw` .d.ts
// (never imported at runtime -- see module docstring). Only the members
// actually used below are represented. ---------------------------------

interface PluginLogger {
  info?: (message: string) => void;
  warn?: (message: string) => void;
  debug?: (message: string) => void;
}

interface AgentContext {
  agentId?: string;
}

interface BeforePromptBuildEvent {
  prompt: string;
  messages: unknown[];
}

interface BeforePromptBuildResult {
  prependContext?: string;
}

interface AgentEndEvent {
  messages: unknown[];
}

interface RuntimeLifecycleRegistration {
  id: string;
  description?: string;
  cleanup?: (ctx: unknown) => void | Promise<void>;
}

interface PluginApi {
  pluginConfig?: RawErgoConfig;
  logger?: PluginLogger;
  registerTool: (tool: unknown, opts?: { optional?: boolean }) => void;
  on: (hookName: string, handler: (event: unknown, ctx: unknown) => unknown, opts?: Record<string, unknown>) => void;
  lifecycle?: { registerRuntimeLifecycle: (registration: RuntimeLifecycleRegistration) => void };
}

// --- small shared result helpers ---------------------------------------

function textContent(text: string) {
  return { type: "text" as const, text };
}

function toolResult(text: string, details: JsonRecord) {
  return { content: [textContent(text)], details };
}

/**
 * A 409 from a guarded write route is Ergo's product working, not an
 * error: surface the conflicting claim + reason as usable structured data
 * (both in the readable text AND in `details`) so the agent can decide to
 * supersede, retry with a different statement, or drop it. Swallowing this
 * -- or reporting it as a generic failure -- defeats the entire point of
 * wiring a contradiction-guarded backend behind `memory_store`.
 */
function conflictResult(payload: JsonRecord, hint: string) {
  const text = `Ergo declined this write because it conflicts with an existing active claim -- this is expected behavior, not a failure.\n${hint}\n\n${JSON.stringify(payload, null, 2)}`;
  return toolResult(text, payload);
}

function describeError(err: unknown): string {
  if (err instanceof ErgoError) return `HTTP ${err.statusCode}`;
  if (err instanceof Error) return err.message;
  return String(err);
}

function clampRecallLimit(value: unknown, fallback: number): number {
  if (typeof value !== "number" || !Number.isFinite(value)) return fallback;
  return Math.min(50, Math.max(1, Math.floor(value)));
}

function formatRecallRow(row: unknown, index: number): string {
  const rec = typeof row === "object" && row !== null ? (row as JsonRecord) : {};
  const statement = typeof rec.statement === "string" ? rec.statement : "(no statement)";
  const reason = typeof rec.reason === "string" && rec.reason ? ` (${rec.reason})` : "";
  return `${index + 1}. ${statement}${reason}`;
}

// --- opt-in autoCapture message extraction ------------------------------

function messageRole(message: unknown): string | undefined {
  if (typeof message !== "object" || message === null) return undefined;
  const role = (message as Record<string, unknown>).role;
  return typeof role === "string" ? role : undefined;
}

function messageText(message: unknown): string | undefined {
  if (typeof message !== "object" || message === null) return undefined;
  const content = (message as Record<string, unknown>).content;
  if (typeof content === "string") return content;
  if (Array.isArray(content)) {
    const parts = content
      .map((part) =>
        typeof part === "object" && part !== null && typeof (part as Record<string, unknown>).text === "string"
          ? ((part as Record<string, unknown>).text as string)
          : undefined,
      )
      .filter((part): part is string => typeof part === "string");
    return parts.length > 0 ? parts.join("\n") : undefined;
  }
  return undefined;
}

/**
 * Extracts the last user/assistant exchange from an `agent_end` transcript.
 * `messages` is typed `unknown[]` in the published SDK (harness-specific
 * shape), so this defensively duck-types `{role, content}` and returns
 * `undefined` for anything else rather than guessing further. Skips
 * recalled-memory context (feedback-loop guard) and short exchanges, same
 * substance filter as both reference memory plugins.
 */
export function extractTurnText(messages: unknown[]): string | undefined {
  if (!Array.isArray(messages)) return undefined;
  let lastUser: string | undefined;
  let lastAssistant: string | undefined;
  for (const message of messages) {
    const role = messageRole(message);
    const text = messageText(message);
    if (!role || !text || text.includes(RECALL_MARKER)) continue;
    if (role === "user") lastUser = text;
    else if (role === "assistant") lastAssistant = text;
  }
  if (!lastUser && !lastAssistant) return undefined;
  const combined = `User: ${lastUser ?? ""}\n\nAssistant: ${lastAssistant ?? ""}`.trim();
  return combined.length >= MIN_CAPTURE_CHARS ? combined : undefined;
}

// --- plugin registration -------------------------------------------------

export function register(api: PluginApi): void {
  const rawConfig = api.pluginConfig;
  if (!isEnabled(rawConfig)) {
    api.logger?.warn?.("ergo: baseUrl explicitly blanked, not activating");
    return;
  }

  const config: ResolvedErgoConfig = resolveConfig(rawConfig);
  const client = new ErgoClient({ baseUrl: config.baseUrl, apiToken: config.apiToken });

  registerMemorySlotTools(api, client, config);
  registerErgoTools(api, client, config);

  if (config.autoRecall) {
    registerAutoRecall(api, client, config);
  }
  if (config.autoCapture) {
    registerAutoCapture(api, client, config);
  }
}

function registerMemorySlotTools(api: PluginApi, client: ErgoClient, config: ResolvedErgoConfig): void {
  api.registerTool(
    {
      name: "memory_recall",
      label: "Recall Memory",
      description:
        "Search Ergo's decision memory for relevant facts, decisions, and constraints (GET /recall). " +
        "Degrades to no results instead of failing when the engine is unreachable.",
      parameters: Type.Object(
        {
          query: Type.String({ description: "Search query." }),
          limit: Type.Optional(Type.Integer({ minimum: 1, maximum: 50, description: "Max results (clamped to [1,50])." })),
        },
        { additionalProperties: false },
      ),
      async execute(_toolCallId: string, params: { query: string; limit?: number }) {
        const limit = clampRecallLimit(params.limit, config.recallLimit);
        try {
          const resp = await client.recall(forceScopeRead(config, { q: params.query, limit }));
          const results = Array.isArray(resp.results) ? resp.results : [];
          if (results.length === 0) {
            return toolResult(`No relevant memories found for: "${params.query}"`, { results: [] });
          }
          const formatted = results.map(formatRecallRow).join("\n");
          return toolResult(
            `Found ${results.length} relevant ${results.length === 1 ? "memory" : "memories"}:\n\n${formatted}`,
            { results },
          );
        } catch (err) {
          // Recall is best-effort context, never a hard turn dependency --
          // degrade instead of throwing, same as the auto-recall hook below
          // and both reference memory plugins (memory-lancedb, memory-mem0).
          return toolResult(
            `Ergo memory is unreachable right now (${describeError(err)}); continuing without recall.`,
            { results: [], degraded: true },
          );
        }
      },
    },
    { optional: true },
  );

  api.registerTool(
    {
      name: "memory_store",
      label: "Store Memory",
      description:
        "Store a fact, decision, or constraint in Ergo's contradiction-guarded memory (POST /remember). " +
        "A conflict with an existing active claim comes back as structured data, not a failure -- resolve " +
        "it with ergo_supersede, or store a different statement.",
      parameters: Type.Object(
        {
          content: Type.String({ description: "The fact, decision, or constraint to remember." }),
          reason: Type.Optional(
            Type.String({ description: "Why -- recommended: a reason-less claim can never win an ergo_why query." }),
          ),
        },
        { additionalProperties: false },
      ),
      async execute(_toolCallId: string, params: { content: string; reason?: string }) {
        const extra: JsonRecord = { statement: params.content };
        if (params.reason) extra.reason = params.reason;
        const result = await client.remember(forceScopeWrite(config, extra));
        if (result.result === "conflict") {
          return conflictResult(
            result,
            "Resolve by calling ergo_supersede with the conflicting claim's id, or store different content.",
          );
        }
        return toolResult(`Stored in Ergo memory (project=${config.project}).`, result);
      },
    },
    { optional: true },
  );

  api.registerTool(
    {
      name: "memory_forget",
      label: "Forget Memory",
      description:
        "Remove a wrong-at-birth memory from Ergo (POST /retract) -- a typo, test junk, or a mis-scoped " +
        "write; never for a decision that genuinely changed (use ergo_supersede for that). Removal is " +
        "audit-loud, so `reason` is required.",
      parameters: Type.Object(
        {
          id: Type.String({ description: "Claim id to remove (from memory_recall/ergo_active/ergo_history)." }),
          reason: Type.String({ description: "Required -- why was this wrong at birth?" }),
        },
        { additionalProperties: false },
      ),
      async execute(_toolCallId: string, params: { id: string; reason: string }) {
        const result = await client.retract(forceScopeWrite(config, { claim_id: params.id, reason: params.reason }));
        return toolResult(`Removed claim ${params.id} from Ergo memory.`, result);
      },
    },
    { optional: true },
  );
}

/**
 * Decision-ledger verbs no other memory plugin has, namespaced `ergo_*` so
 * they can never collide with the slot's conventional `memory_*` names.
 * Mirrors `ergo/mcp/server.py`'s naming, arguments, and scoping exactly --
 * deliberately not a second vocabulary. `ergo_remember`/`ergo_recall`/
 * `ergo_ingest` are intentionally NOT duplicated here: `memory_store` and
 * `memory_recall` already cover `/remember` and `/recall`, and `/ingest`
 * is only ever reached through the opt-in `autoCapture` path below, never
 * as a manually-invoked tool.
 */
function registerErgoTools(api: PluginApi, client: ErgoClient, config: ResolvedErgoConfig): void {
  api.registerTool(
    {
      name: "ergo_why",
      label: "Ergo: Why",
      description: "Best REASONED belief for a query plus its supersede history. A reason-less fact can never win this query.",
      parameters: Type.Object({ query: Type.String() }, { additionalProperties: false }),
      async execute(_toolCallId: string, params: { query: string }) {
        const result = await client.why(forceScopeRead(config, { q: params.query }));
        return toolResult(JSON.stringify(result, null, 2), result);
      },
    },
    { optional: true },
  );

  api.registerTool(
    {
      name: "ergo_learn",
      label: "Ergo: Learn",
      description:
        "Record a belief formed FROM a source (file:line, URL, doc id), contradiction-guarded with " +
        "provenance folded into the stored reason. Same conflict handling as memory_store.",
      parameters: Type.Object(
        {
          statement: Type.String(),
          source: Type.String({ description: "Where this belief came from." }),
          reason: Type.Optional(Type.String()),
          force_exception: Type.Optional(Type.String({ description: "Store despite a conflict, with this justification." })),
        },
        { additionalProperties: false },
      ),
      async execute(_toolCallId: string, params: { statement: string; source: string; reason?: string; force_exception?: string }) {
        const extra: JsonRecord = { statement: params.statement, source: params.source };
        if (params.reason) extra.reason = params.reason;
        if (params.force_exception) extra.force_exception = params.force_exception;
        const result = await client.learn(forceScopeWrite(config, extra));
        if (result.result === "conflict") {
          return conflictResult(result, "Resolve by calling ergo_supersede, or retry ergo_learn with force_exception.");
        }
        return toolResult(`Learned and stored in Ergo memory (project=${config.project}).`, result);
      },
    },
    { optional: true },
  );

  api.registerTool(
    {
      name: "ergo_supersede",
      label: "Ergo: Supersede",
      description: 'Replace an existing active claim with a revised one ("I changed my mind"). Get existing_id from memory_recall/ergo_why/ergo_active.',
      parameters: Type.Object(
        {
          existing_id: Type.String(),
          statement: Type.String(),
          reason: Type.Optional(Type.String()),
        },
        { additionalProperties: false },
      ),
      async execute(_toolCallId: string, params: { existing_id: string; statement: string; reason?: string }) {
        const extra: JsonRecord = { existing_id: params.existing_id, statement: params.statement };
        if (params.reason) extra.reason = params.reason;
        const result = await client.supersede(forceScopeWrite(config, extra));
        if (result.result === "conflict") {
          return conflictResult(result, "The revised statement itself conflicts with another active claim; resolve that first.");
        }
        return toolResult(`Superseded claim ${params.existing_id} in Ergo memory.`, result);
      },
    },
    { optional: true },
  );

  api.registerTool(
    {
      name: "ergo_retract",
      label: "Ergo: Retract",
      description: "Retract a wrong-at-birth claim (typo, test junk, mis-scoped write) -- never for a decision that genuinely changed, use ergo_supersede for that.",
      parameters: Type.Object(
        {
          claim_id: Type.String(),
          reason: Type.String({ description: "Required -- removal is audit-loud." }),
        },
        { additionalProperties: false },
      ),
      async execute(_toolCallId: string, params: { claim_id: string; reason: string }) {
        const result = await client.retract(forceScopeWrite(config, { claim_id: params.claim_id, reason: params.reason }));
        return toolResult(`Retracted claim ${params.claim_id}.`, result);
      },
    },
    { optional: true },
  );

  api.registerTool(
    {
      name: "ergo_active",
      label: "Ergo: Active Claims",
      description: "Enumerate active claims in scope.",
      parameters: Type.Object(
        {
          limit: Type.Optional(Type.Integer({ minimum: 1, maximum: 500 })),
          offset: Type.Optional(Type.Integer({ minimum: 0 })),
        },
        { additionalProperties: false },
      ),
      async execute(_toolCallId: string, params: { limit?: number; offset?: number }) {
        const extra: JsonRecord = {};
        if (params.limit !== undefined) extra.limit = params.limit;
        if (params.offset !== undefined) extra.offset = params.offset;
        const result = await client.active(forceScopeRead(config, extra));
        return toolResult(JSON.stringify(result, null, 2), result);
      },
    },
    { optional: true },
  );

  api.registerTool(
    {
      name: "ergo_history",
      label: "Ergo: History",
      description: "Full claim history for scope -- active + superseded + retracted.",
      parameters: Type.Object({}, { additionalProperties: false }),
      async execute() {
        const result = await client.history(forceScopeRead(config, {}));
        return toolResult(JSON.stringify(result, null, 2), result);
      },
    },
    { optional: true },
  );

  api.registerTool(
    {
      name: "ergo_diagnose",
      label: "Ergo: Diagnose",
      description: "Ops view of the Ergo engine (anti-zombie): counts, conflict stats, version drift warnings.",
      parameters: Type.Object({}, { additionalProperties: false }),
      async execute() {
        const result = await client.diagnose();
        return toolResult(JSON.stringify(result, null, 2), result);
      },
    },
    { optional: true },
  );

  api.registerTool(
    {
      name: "ergo_health",
      label: "Ergo: Health",
      description: "Liveness probe for the Ergo engine.",
      parameters: Type.Object({}, { additionalProperties: false }),
      async execute() {
        const result = await client.health();
        return toolResult(JSON.stringify(result, null, 2), result);
      },
    },
    { optional: true },
  );
}

function registerAutoRecall(api: PluginApi, client: ErgoClient, config: ResolvedErgoConfig): void {
  api.on("before_prompt_build", async (rawEvent: unknown, rawCtx: unknown) => {
    const event = rawEvent as BeforePromptBuildEvent;
    const ctx = (rawCtx as AgentContext) ?? {};
    const query = (event?.prompt ?? "").trim();
    if (!query) return;

    const project = projectForContext(config, ctx.agentId);
    let results: unknown[];
    try {
      const resp = await client.recall(
        forceScopeRead(config, { q: query, limit: config.recallLimit }, project),
        RECALL_HOOK_TIMEOUT_MS,
      );
      results = Array.isArray(resp.results) ? resp.results : [];
    } catch {
      // Best-effort context injection must never block or fail a turn.
      return;
    }
    if (results.length === 0) return;

    const lines = results.map(formatRecallRow).join("\n");
    const result: BeforePromptBuildResult = {
      prependContext: `${RECALL_MARKER} (project=${project}):\n${lines}`,
    };
    return result;
  });
}

function registerAutoCapture(api: PluginApi, client: ErgoClient, config: ResolvedErgoConfig): void {
  const inflight = new Set<Promise<void>>();

  api.on("agent_end", (rawEvent: unknown) => {
    const event = rawEvent as AgentEndEvent;
    if (inflight.size >= MAX_INFLIGHT_CAPTURES) {
      api.logger?.warn?.("ergo: autoCapture dropped -- too many in-flight /ingest calls (engine too slow)");
      return;
    }
    const text = extractTurnText(event?.messages ?? []);
    if (!text) return;

    const payload = forceScopeWrite(config, { text, source: "openclaw-agent_end" });
    const job = client.ingest(payload).then(
      () => undefined,
      (err) => {
        api.logger?.debug?.(`ergo: autoCapture ingest failed: ${describeError(err)}`);
      },
    );
    inflight.add(job);
    void job.finally(() => inflight.delete(job));
  });

  api.lifecycle?.registerRuntimeLifecycle({
    id: "ergo-capture-flush",
    description: "Drain in-flight Ergo /ingest autoCapture calls before teardown.",
    cleanup: async () => {
      if (inflight.size === 0) return;
      const pending = Array.from(inflight);
      const timeout = new Promise<void>((resolve) => setTimeout(resolve, CAPTURE_FLUSH_TIMEOUT_MS));
      await Promise.race([Promise.allSettled(pending).then(() => undefined), timeout]);
      if (inflight.size > 0) {
        api.logger?.warn?.(`ergo: shutdown timed out draining autoCapture -- ${inflight.size} call(s) still in flight`);
      }
    },
  });
}

export { DEFAULT_RECALL_LIMIT };

const definition = {
  id: "memory-ergo",
  name: "Ergo Memory",
  description: "Decision memory that remembers WHY, and refuses to silently contradict itself -- backed by a self-hosted Ergo engine.",
  kind: "memory" as const,
  register,
};

export default definition;
