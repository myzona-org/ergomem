/**
 * Config resolution + scoping for the Ergo OpenClaw plugin.
 *
 * Everything a write/read call sends to Ergo is scoped to
 * `(org_id, project)`, plus a `who` actor label on writes. This module is
 * the single place that resolves those values so every call site agrees --
 * the Hermes sibling plugin (`plugins/memory/ergo/__init__.py`) shipped a
 * `setdefault()`-based scope merge that let a tool-call arg silently win
 * over the configured scope (a confused-deputy scope escape, caught in
 * review). `forceScope*` below always overwrites, never defaults.
 */

export const DEFAULT_BASE_URL = "http://127.0.0.1:8788";
export const DEFAULT_PROJECT = "openclaw";
export const DEFAULT_ORG_ID = "default";
export const DEFAULT_WHO = "openclaw-agent";
export const DEFAULT_RECALL_LIMIT = 5;

/** Shape of `api.pluginConfig` -- an untrusted, user-edited JSON object. */
export interface RawErgoConfig {
  baseUrl?: unknown;
  apiToken?: unknown;
  project?: unknown;
  autoRecall?: unknown;
  autoCapture?: unknown;
  recallLimit?: unknown;
}

export interface ResolvedErgoConfig {
  baseUrl: string;
  apiToken?: string;
  /** The configured or literal-default project. See `projectForContext`. */
  project: string;
  /** True iff the operator explicitly set a non-empty `project` in config. */
  projectExplicit: boolean;
  orgId: string;
  who: string;
  autoRecall: boolean;
  autoCapture: boolean;
  recallLimit: number;
}

function nonEmptyString(value: unknown): string | undefined {
  if (typeof value !== "string") return undefined;
  const trimmed = value.trim();
  return trimmed.length > 0 ? trimmed : undefined;
}

/**
 * Resolves the effective base URL with ONE precedence, used by every caller
 * that needs to know whether the plugin is enabled -- so "explicitly
 * disabled" can never mean two different things in two different places
 * (the Hermes sibling plugin's own review caught exactly that split: an
 * availability check and an init path disagreeing on what a blanked value
 * meant).
 *
 * An explicit empty string (`"baseUrl": ""` in `openclaw.json`) is the
 * documented kill switch and always wins over the schema default -- this
 * reads the raw config object directly rather than trusting a downstream
 * JSON-Schema-default layer to preserve that distinction.
 */
export function resolveBaseUrl(raw: RawErgoConfig | undefined | null): string {
  const value = raw?.baseUrl;
  if (typeof value === "string") return value.trim();
  return DEFAULT_BASE_URL;
}

/** Config-presence check only -- no network call. */
export function isEnabled(raw: RawErgoConfig | undefined | null): boolean {
  return resolveBaseUrl(raw) !== "";
}

export function resolveConfig(raw: RawErgoConfig | undefined | null): ResolvedErgoConfig {
  const baseUrl = resolveBaseUrl(raw);
  const project = nonEmptyString(raw?.project);
  const recallLimitRaw = raw?.recallLimit;
  // Clamped here, once, to [1, 50] -- 50 is Ergo's own GET /recall clamp
  // (docs/HTTP-API.md), not a number this plugin invented, so a configured
  // value above it would be misleading even though the engine would itself
  // ignore the excess. This is also the value the auto-recall hook sends
  // straight through with no clamp of its own (PR review finding), so
  // resolveConfig must be the single place this is enforced.
  const recallLimit =
    typeof recallLimitRaw === "number" && Number.isFinite(recallLimitRaw) && recallLimitRaw > 0
      ? Math.min(50, Math.floor(recallLimitRaw))
      : DEFAULT_RECALL_LIMIT;

  return {
    baseUrl,
    apiToken: nonEmptyString(raw?.apiToken),
    project: project ?? DEFAULT_PROJECT,
    projectExplicit: project !== undefined,
    orgId: DEFAULT_ORG_ID,
    who: DEFAULT_WHO,
    // Both default true/false respectively; only an explicit `false`/`true`
    // flips them -- matches the JSON Schema defaults in openclaw.plugin.json.
    autoRecall: raw?.autoRecall !== false,
    autoCapture: raw?.autoCapture === true,
    recallLimit,
  };
}

/**
 * Project for a single call. Tool `execute()` handlers in the OpenClaw
 * plugin SDK receive no per-call agent/session context (verified against
 * `AgentTool.execute(this: void, toolCallId, params, signal?, onUpdate?)` in
 * the published `openclaw` type declarations) -- only the `before_prompt_build`
 * hook does, via its `PluginHookAgentContext` second argument. So dynamic,
 * per-agent scoping is only possible from that hook; every tool call uses
 * the plugin's statically configured project.
 *
 * When the operator has NOT set `project` explicitly, the auto-recall hook
 * prefers the harness's own `agentId` over the literal default, mirroring
 * the Hermes plugin's config > harness-identity > literal-default
 * precedence -- but an operator who explicitly configured `project` (even
 * to the same string as the literal default) always wins; `projectExplicit`
 * makes that distinction instead of a fragile string comparison.
 */
export function projectForContext(config: ResolvedErgoConfig, agentId?: string): string {
  if (config.projectExplicit) return config.project;
  return nonEmptyString(agentId) ?? DEFAULT_PROJECT;
}

/**
 * Merges a tool call's own fields with the plugin's resolved scope.
 * `org_id`/`project`/`who` are forced AFTER the spread so they always win,
 * regardless of what a caller/model put in `extra` -- never a
 * `extra.org_id ??= ...`-style default, which a model could beat by
 * supplying its own (every tool's own parameter schema also sets
 * `additionalProperties: false` so a model can't even attempt it, but this
 * is the second, independent line of defense).
 */
export function forceScopeWrite(config: ResolvedErgoConfig, extra: JsonRecord, project?: string): JsonRecord {
  return { ...extra, org_id: config.orgId, project: project ?? config.project, who: config.who };
}

export function forceScopeRead(config: ResolvedErgoConfig, extra: JsonRecord, project?: string): JsonRecord {
  return { ...extra, org_id: config.orgId, project: project ?? config.project };
}

export type JsonRecord = Record<string, unknown>;
