import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import {
  DEFAULT_BASE_URL,
  DEFAULT_ORG_ID,
  DEFAULT_PROJECT,
  DEFAULT_RECALL_LIMIT,
  DEFAULT_WHO,
  forceScopeRead,
  forceScopeWrite,
  isEnabled,
  projectForContext,
  resolveBaseUrl,
  resolveConfig,
} from "../config.ts";

test("resolveBaseUrl defaults when unset", () => {
  assert.equal(resolveBaseUrl(undefined), DEFAULT_BASE_URL);
  assert.equal(resolveBaseUrl({}), DEFAULT_BASE_URL);
});

test("resolveBaseUrl trims and returns an explicit value", () => {
  assert.equal(resolveBaseUrl({ baseUrl: "  https://ergo.example.com  " }), "https://ergo.example.com");
});

test("resolveBaseUrl treats an explicit empty string as the kill switch, not the default", () => {
  assert.equal(resolveBaseUrl({ baseUrl: "" }), "");
});

test("isEnabled and resolveConfig agree on the kill switch (single precedence)", () => {
  const raw = { baseUrl: "" };
  assert.equal(isEnabled(raw), false);
  assert.equal(resolveConfig(raw).baseUrl, "");
});

test("isEnabled is true by default (zero-config local engine)", () => {
  assert.equal(isEnabled(undefined), true);
  assert.equal(isEnabled({}), true);
});

test("resolveConfig applies documented defaults", () => {
  const config = resolveConfig(undefined);
  assert.equal(config.baseUrl, DEFAULT_BASE_URL);
  assert.equal(config.project, DEFAULT_PROJECT);
  assert.equal(config.projectExplicit, false);
  assert.equal(config.orgId, DEFAULT_ORG_ID);
  assert.equal(config.who, DEFAULT_WHO);
  assert.equal(config.autoRecall, true);
  assert.equal(config.autoCapture, false);
  assert.equal(config.recallLimit, DEFAULT_RECALL_LIMIT);
  assert.equal(config.apiToken, undefined);
});

test("resolveConfig only flips autoRecall/autoCapture on explicit booleans", () => {
  assert.equal(resolveConfig({ autoRecall: false }).autoRecall, false);
  assert.equal(resolveConfig({ autoRecall: "false" }).autoRecall, true, "a non-boolean must not disable auto-recall");
  assert.equal(resolveConfig({ autoCapture: true }).autoCapture, true);
  assert.equal(resolveConfig({ autoCapture: "true" }).autoCapture, false, "a non-boolean must not enable auto-capture");
});

test("resolveConfig falls back to the default recallLimit on invalid input", () => {
  assert.equal(resolveConfig({ recallLimit: -5 }).recallLimit, DEFAULT_RECALL_LIMIT);
  assert.equal(resolveConfig({ recallLimit: "10" }).recallLimit, DEFAULT_RECALL_LIMIT);
  assert.equal(resolveConfig({ recallLimit: 12.9 }).recallLimit, 12);
});

test("resolveConfig clamps recallLimit to the documented [1, 50] range", () => {
  // openclaw.plugin.json and the README both document recallLimit as
  // clamped to [1, 50] -- the auto-recall hook sends config.recallLimit
  // straight through to GET /recall with no clamping of its own (unlike
  // memory_recall's per-call clampRecallLimit), so resolveConfig must be
  // the single place this is enforced, or an oversized configured value
  // reaches the engine unclamped on every auto-recall call.
  assert.equal(resolveConfig({ recallLimit: 10000 }).recallLimit, 50);
  assert.equal(resolveConfig({ recallLimit: 50 }).recallLimit, 50);
  assert.equal(resolveConfig({ recallLimit: 51 }).recallLimit, 50);
});

test("openclaw.plugin.json's recallLimit schema bound matches the code's clamp, not just documents it", () => {
  // A schema bound that drifted from resolveConfig's actual clamp would be
  // worse than no bound at all: it would tell an operator "500 is fine" via
  // the config UI while the code silently clamps to 50. Parsing the real
  // manifest file (not a hand-copied literal) keeps the two from silently
  // diverging.
  const manifest = JSON.parse(readFileSync(new URL("../openclaw.plugin.json", import.meta.url), "utf8"));
  const schema = manifest.configSchema.properties.recallLimit;
  assert.equal(schema.minimum, 1);
  assert.equal(schema.maximum, 50);
  assert.equal(resolveConfig({ recallLimit: schema.maximum + 1 }).recallLimit, schema.maximum);
});

test("resolveConfig marks project explicit only for a real non-empty string", () => {
  assert.equal(resolveConfig({ project: "acme" }).projectExplicit, true);
  assert.equal(resolveConfig({ project: "" }).projectExplicit, false);
  assert.equal(resolveConfig({ project: "   " }).projectExplicit, false);
});

test("projectForContext prefers an explicitly configured project over the harness agentId", () => {
  const config = resolveConfig({ project: "acme" });
  assert.equal(projectForContext(config, "agent-123"), "acme");
});

test("projectForContext falls back to the harness agentId when project was never configured", () => {
  const config = resolveConfig(undefined);
  assert.equal(projectForContext(config, "agent-123"), "agent-123");
});

test("projectForContext falls back to the literal default when neither is available", () => {
  const config = resolveConfig(undefined);
  assert.equal(projectForContext(config, undefined), DEFAULT_PROJECT);
  assert.equal(projectForContext(config, "   "), DEFAULT_PROJECT);
});

test("forceScopeWrite overwrites an attacker-supplied org_id/project/who instead of defaulting", () => {
  const config = resolveConfig({ project: "real-project" });
  const smuggled = {
    statement: "malicious statement",
    org_id: "attacker-org",
    project: "attacker-project",
    who: "attacker",
  };
  const scoped = forceScopeWrite(config, smuggled);
  assert.equal(scoped.org_id, config.orgId);
  assert.equal(scoped.project, "real-project");
  assert.equal(scoped.who, config.who);
  assert.equal(scoped.statement, "malicious statement", "non-scope fields must survive the merge");
});

test("forceScopeRead overwrites an attacker-supplied org_id/project instead of defaulting", () => {
  const config = resolveConfig({ project: "real-project" });
  const smuggled = { q: "query", org_id: "attacker-org", project: "attacker-project" };
  const scoped = forceScopeRead(config, smuggled);
  assert.equal(scoped.org_id, config.orgId);
  assert.equal(scoped.project, "real-project");
  assert.equal(scoped.q, "query");
});

test("forceScopeRead/Write accept an explicit project override for the auto-recall hook's per-call scoping", () => {
  const config = resolveConfig(undefined);
  const scoped = forceScopeRead(config, { q: "x" }, "agent-123");
  assert.equal(scoped.project, "agent-123");
});
