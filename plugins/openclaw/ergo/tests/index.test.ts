import { test } from "node:test";
import assert from "node:assert/strict";
import { extractTurnText, MAX_INFLIGHT_CAPTURES, register } from "../index.ts";

interface FetchCall {
  url: string;
  init: RequestInit;
}

/** Queue of canned {status, body} responses, consumed in order (last one repeats). */
function stubFetch(responses: Array<{ status: number; body: unknown }>) {
  const calls: FetchCall[] = [];
  let i = 0;
  const fn = (async (url: string | URL, init: RequestInit = {}) => {
    calls.push({ url: String(url), init });
    const next = responses[Math.min(i, responses.length - 1)];
    i += 1;
    return new Response(JSON.stringify(next.body), { status: next.status });
  }) as unknown as typeof fetch;
  return { fn, calls };
}

/**
 * Resolves slowly rather than never: the bounded-concurrency test fires all
 * its calls synchronously (before any promise gets a chance to settle), so
 * a short delay is enough to prove the cap without leaving a real
 * `AbortController` timeout (30s in client.ts) as a dangling timer that
 * would otherwise keep the test process alive for the full timeout.
 */
function slowFetch(delayMs: number) {
  const calls: FetchCall[] = [];
  const fn = (async (url: string | URL, init: RequestInit = {}) => {
    calls.push({ url: String(url), init });
    await new Promise((resolve) => setTimeout(resolve, delayMs));
    return new Response(JSON.stringify({ result: "ingested" }), { status: 200 });
  }) as unknown as typeof fetch;
  return { fn, calls };
}

function delayedFetch(responses: Array<{ status: number; body: unknown }>, delayMs: number) {
  const calls: FetchCall[] = [];
  let resolvedAt = 0;
  const fn = (async (url: string | URL, init: RequestInit = {}) => {
    calls.push({ url: String(url), init });
    await new Promise((resolve) => setTimeout(resolve, delayMs));
    resolvedAt = Date.now();
    const next = responses[0];
    return new Response(JSON.stringify(next.body), { status: next.status });
  }) as unknown as typeof fetch;
  return { fn, calls, resolvedAt: () => resolvedAt };
}

async function withFetch<T>(fn: typeof fetch, body: () => Promise<T>): Promise<T> {
  const original = globalThis.fetch;
  globalThis.fetch = fn;
  try {
    return await body();
  } finally {
    globalThis.fetch = original;
  }
}

interface StubTool {
  name: string;
  parameters?: { required?: string[] };
  execute: (toolCallId: string, params: unknown) => Promise<{ content: Array<{ type: string; text: string }>; details: unknown }>;
}

function makeApi(pluginConfig?: Record<string, unknown>) {
  const tools = new Map<string, StubTool>();
  const hooks = new Map<string, (event: unknown, ctx: unknown) => unknown>();
  const cleanups: Array<{ id: string; cleanup?: (ctx: unknown) => unknown }> = [];
  const warnings: string[] = [];

  const api = {
    pluginConfig,
    logger: {
      warn: (message: string) => warnings.push(message),
      debug: () => {},
      info: () => {},
    },
    registerTool: (tool: StubTool) => {
      tools.set(tool.name, tool);
    },
    on: (hookName: string, handler: (event: unknown, ctx: unknown) => unknown) => {
      hooks.set(hookName, handler);
    },
    lifecycle: {
      registerRuntimeLifecycle: (registration: { id: string; cleanup?: (ctx: unknown) => unknown }) => {
        cleanups.push(registration);
      },
    },
  };

  return { api, tools, hooks, cleanups, warnings };
}

const ALL_TOOL_NAMES = [
  "memory_recall",
  "memory_store",
  "memory_forget",
  "ergo_why",
  "ergo_learn",
  "ergo_supersede",
  "ergo_retract",
  "ergo_active",
  "ergo_history",
  "ergo_diagnose",
  "ergo_health",
];

test("kill switch: an explicitly blanked baseUrl registers no tools and no hooks", () => {
  const { api, tools, hooks, warnings } = makeApi({ baseUrl: "" });
  register(api);
  assert.equal(tools.size, 0);
  assert.equal(hooks.size, 0);
  assert.ok(warnings.some((w) => w.includes("not activating")));
});

test("default config registers every slot + ergo_* tool, and the auto-recall hook only", () => {
  const { api, tools, hooks } = makeApi(undefined);
  register(api);
  for (const name of ALL_TOOL_NAMES) {
    assert.ok(tools.has(name), `expected tool ${name} to be registered`);
  }
  assert.ok(hooks.has("before_prompt_build"));
  assert.equal(hooks.has("agent_end"), false, "autoCapture is off by default -- no agent_end hook, no write path");
});

test("autoCapture is off by default and no write happens on agent_end", async () => {
  const { fn, calls } = stubFetch([{ status: 200, body: { result: "ingested" } }]);
  await withFetch(fn, async () => {
    const { api, hooks } = makeApi(undefined); // autoCapture unset -> false
    register(api);
    assert.equal(hooks.has("agent_end"), false);
    assert.equal(calls.length, 0, "no request must ever be sent when autoCapture is disabled");
  });
});

test("memory_store sends the plugin's configured scope end-to-end, never a caller-supplied one", async () => {
  // Two independent layers guard scope here: memory_store's own `parameters`
  // schema never declares org_id/project/who (so a well-behaved caller can't
  // even offer them), and `execute()` below builds its `extra` payload from
  // an explicit allowlist (`content`/`reason`) rather than forwarding
  // `params` wholesale, so a param object with extra keys never reaches
  // `forceScopeWrite` in the first place. This test proves the end-to-end
  // output is correctly scoped; `forceScopeWrite`'s own override-not-default
  // contract (the thing that would matter if either layer above regressed)
  // is unit-tested and mutation-proven directly in config.test.ts.
  const { fn, calls } = stubFetch([{ status: 200, body: { result: "stored", claim_id: "c1" } }]);
  await withFetch(fn, async () => {
    const { api, tools } = makeApi({ project: "real-project" });
    register(api);
    const tool = tools.get("memory_store")!;
    const extraneousParams = {
      content: "attacker statement",
      org_id: "attacker-org",
      project: "attacker-project",
      who: "attacker",
    };
    const result = await tool.execute("call-1", extraneousParams);
    assert.equal(result.details.result, "stored");
    const body = JSON.parse(calls[0].init.body as string);
    assert.equal(body.project, "real-project");
    assert.notEqual(body.project, "attacker-project");
    assert.equal(body.who, "openclaw-agent");
    assert.equal(body.statement, "attacker statement");
  });
});

test("memory_store surfaces a 409 as structured conflict data instead of throwing", async () => {
  const conflictBody = {
    result: "conflict",
    conflicts: [{ claim: { id: "c1", statement: "Deploy on Tuesdays" }, reason: "same subject + opposing modality" }],
  };
  const { fn } = stubFetch([{ status: 409, body: conflictBody }]);
  await withFetch(fn, async () => {
    const { api, tools } = makeApi(undefined);
    register(api);
    const tool = tools.get("memory_store")!;
    const result = await tool.execute("call-1", { content: "Deploy on Wednesdays" });
    assert.equal(result.details.result, "conflict");
    assert.deepEqual(result.details.conflicts, conflictBody.conflicts);
    assert.match(result.content[0].text, /conflict/i);
    assert.match(result.content[0].text, /Deploy on Tuesdays/);
  });
});

test("memory_forget maps id -> claim_id, requires reason in its schema, and forces scope", async () => {
  const { fn, calls } = stubFetch([{ status: 200, body: { result: "retracted", claim_id: "c1", retracted: true } }]);
  await withFetch(fn, async () => {
    const { api, tools } = makeApi({ project: "real-project" });
    register(api);
    const tool = tools.get("memory_forget")!;
    assert.ok(tool.parameters?.required?.includes("reason"), "Ergo's /retract requires a reason -- the tool schema must too");
    await tool.execute("call-1", { id: "c1", reason: "test junk", org_id: "attacker" });
    const body = JSON.parse(calls[0].init.body as string);
    assert.equal(body.claim_id, "c1");
    assert.equal(body.reason, "test junk");
    assert.equal(body.project, "real-project");
  });
});

test("ergo_retract's schema also requires a reason", () => {
  const { api, tools } = makeApi(undefined);
  register(api);
  const tool = tools.get("ergo_retract")!;
  assert.ok(tool.parameters?.required?.includes("reason"));
  assert.ok(tool.parameters?.required?.includes("claim_id"));
});

test("memory_recall degrades to an empty result instead of throwing when the engine is unreachable", async () => {
  const failingFetch = (async () => {
    throw new Error("connect ECONNREFUSED");
  }) as unknown as typeof fetch;
  await withFetch(failingFetch, async () => {
    const { api, tools } = makeApi(undefined);
    register(api);
    const tool = tools.get("memory_recall")!;
    const result = await tool.execute("call-1", { query: "deploy days" });
    assert.deepEqual(result.details.results, []);
    assert.equal(result.details.degraded, true);
    assert.doesNotMatch(result.content[0].text, /error|exception/i);
  });
});

test("memory_recall reports real results when the engine responds", async () => {
  const { fn } = stubFetch([{ status: 200, body: { results: [{ statement: "Deploy on Tuesdays", reason: "on-call" }] } }]);
  await withFetch(fn, async () => {
    const { api, tools } = makeApi(undefined);
    register(api);
    const tool = tools.get("memory_recall")!;
    const result = await tool.execute("call-1", { query: "deploy days" });
    assert.equal(result.details.results.length, 1);
    assert.match(result.content[0].text, /Deploy on Tuesdays/);
  });
});

test("before_prompt_build auto-recall injects prependContext from real results", async () => {
  const { fn, calls } = stubFetch([{ status: 200, body: { results: [{ statement: "Never deploy on Fridays", reason: "incident risk" } ] } }]);
  await withFetch(fn, async () => {
    const { api, hooks } = makeApi(undefined);
    register(api);
    const handler = hooks.get("before_prompt_build")!;
    const result = await handler({ prompt: "can we deploy tomorrow?", messages: [] }, { agentId: "agent-123" });
    assert.match((result as { prependContext: string }).prependContext, /Never deploy on Fridays/);
    const url = new URL(calls[0].url);
    assert.equal(url.searchParams.get("q"), "can we deploy tomorrow?");
  });
});

test("before_prompt_build auto-recall degrades silently (no throw, no result) when the engine is down", async () => {
  const failingFetch = (async () => {
    throw new Error("timeout");
  }) as unknown as typeof fetch;
  await withFetch(failingFetch, async () => {
    const { api, hooks } = makeApi(undefined);
    register(api);
    const handler = hooks.get("before_prompt_build")!;
    const result = await handler({ prompt: "can we deploy tomorrow?", messages: [] }, {});
    assert.equal(result, undefined);
  });
});

test("before_prompt_build scopes to the configured project when set, ignoring ctx.agentId", async () => {
  const { fn, calls } = stubFetch([{ status: 200, body: { results: [] } }]);
  await withFetch(fn, async () => {
    const { api, hooks } = makeApi({ project: "configured-project" });
    register(api);
    const handler = hooks.get("before_prompt_build")!;
    await handler({ prompt: "q", messages: [] }, { agentId: "agent-123" });
    const url = new URL(calls[0].url);
    assert.equal(url.searchParams.get("project"), "configured-project");
  });
});

test("before_prompt_build falls back to ctx.agentId for scoping when project was never configured", async () => {
  const { fn, calls } = stubFetch([{ status: 200, body: { results: [] } }]);
  await withFetch(fn, async () => {
    const { api, hooks } = makeApi(undefined);
    register(api);
    const handler = hooks.get("before_prompt_build")!;
    await handler({ prompt: "q", messages: [] }, { agentId: "agent-123" });
    const url = new URL(calls[0].url);
    assert.equal(url.searchParams.get("project"), "agent-123");
  });
});

test("extractTurnText skips recalled-memory context to avoid a feedback loop, and short exchanges", () => {
  assert.equal(extractTurnText([{ role: "user", content: "hi" }]), undefined, "too short to be substantial");
  assert.equal(
    extractTurnText([
      { role: "user", content: "Relevant Ergo memory (project=x):\n- some recalled fact that is long enough to pass the length filter" },
    ]),
    undefined,
    "recalled context must never be re-ingested",
  );
  const text = extractTurnText([
    { role: "user", content: "What day should we deploy given our on-call rotation this sprint?" },
    { role: "assistant", content: "Given the on-call rotation, Tuesdays are the safest deploy day this sprint." },
  ]);
  assert.match(text ?? "", /Tuesdays are the safest deploy day/);
});

test("autoCapture, when enabled, posts a completed turn to /ingest -- never /remember -- with forced scope", async () => {
  const { fn, calls } = stubFetch([{ status: 200, body: { result: "ingested" } }]);
  await withFetch(fn, async () => {
    const { api, hooks } = makeApi({ autoCapture: true, project: "real-project" });
    register(api);
    const handler = hooks.get("agent_end")!;
    handler(
      {
        messages: [
          { role: "user", content: "What day should we deploy given our on-call rotation this sprint?", org_id: "attacker" },
          { role: "assistant", content: "Given the on-call rotation, Tuesdays are the safest deploy day this sprint." },
        ],
      },
      {},
    );
    await new Promise((resolve) => setTimeout(resolve, 10)); // let the fire-and-forget promise settle
    assert.equal(calls.length, 1);
    const url = new URL(calls[0].url);
    assert.equal(url.pathname, "/ingest");
    const body = JSON.parse(calls[0].init.body as string);
    assert.equal(body.project, "real-project");
    assert.match(body.text, /Tuesdays are the safest deploy day/);
  });
});

test("autoCapture drops beyond MAX_INFLIGHT_CAPTURES instead of queueing unboundedly, and logs the drop", async () => {
  const { fn, calls } = slowFetch(20);
  await withFetch(fn, async () => {
    const { api, hooks, warnings } = makeApi({ autoCapture: true });
    register(api);
    const handler = hooks.get("agent_end")!;
    const longEnoughMessages = (n: number) => [
      { role: "user", content: `Question number ${n} about our on-call deploy rotation this sprint, please.` },
      { role: "assistant", content: `Answer number ${n} about the on-call deploy rotation for this sprint.` },
    ];
    for (let i = 0; i < MAX_INFLIGHT_CAPTURES + 5; i += 1) {
      handler({ messages: longEnoughMessages(i) }, {});
    }
    assert.equal(calls.length, MAX_INFLIGHT_CAPTURES, "in-flight capture calls must be bounded, never unbounded");
    assert.ok(warnings.some((w) => w.includes("dropped")));
    await new Promise((resolve) => setTimeout(resolve, 30)); // let the in-flight sends settle before the stub is restored
  });
});

test("autoCapture's teardown flush waits for the in-flight /ingest call before resolving", async () => {
  const { fn, resolvedAt } = delayedFetch([{ status: 200, body: { result: "ingested" } }], 15);
  await withFetch(fn, async () => {
    const { api, hooks, cleanups } = makeApi({ autoCapture: true });
    register(api);
    const handler = hooks.get("agent_end")!;
    handler(
      {
        messages: [
          { role: "user", content: "What day should we deploy given our on-call rotation this sprint?" },
          { role: "assistant", content: "Given the on-call rotation, Tuesdays are the safest deploy day this sprint." },
        ],
      },
      {},
    );
    const flush = cleanups.find((c) => c.id === "ergo-capture-flush");
    assert.ok(flush?.cleanup, "expected a registered teardown flush for the autoCapture path");
    await flush!.cleanup!({});
    assert.ok(resolvedAt() > 0, "cleanup must not resolve before the in-flight ingest call settles");
  });
});
