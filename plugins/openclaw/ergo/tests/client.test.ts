import { test } from "node:test";
import assert from "node:assert/strict";
import { ErgoClient, ErgoError } from "../client.ts";

/** Records every call and returns a canned {status, body} per call, in order. */
function stubFetch(responses: Array<{ status: number; body: unknown }>) {
  const calls: Array<{ url: string; init: RequestInit }> = [];
  let i = 0;
  const fn = (async (url: string | URL, init: RequestInit = {}) => {
    calls.push({ url: String(url), init });
    const next = responses[Math.min(i, responses.length - 1)];
    i += 1;
    return new Response(JSON.stringify(next.body), { status: next.status });
  }) as unknown as typeof fetch;
  return { fn, calls };
}

test("health() sends no Authorization header even when a token is configured", async () => {
  const { fn, calls } = stubFetch([{ status: 200, body: { status: "ok" } }]);
  const client = new ErgoClient({ baseUrl: "http://x", apiToken: "secret-token", fetchImpl: fn });
  const result = await client.health();
  assert.equal(result.status, "ok");
  const headers = calls[0].init.headers as Record<string, string>;
  assert.equal(headers.Authorization, undefined);
});

test("remember() sends the bearer token when configured", async () => {
  const { fn, calls } = stubFetch([{ status: 200, body: { result: "stored", claim_id: "c1" } }]);
  const client = new ErgoClient({ baseUrl: "http://x", apiToken: "secret-token", fetchImpl: fn });
  await client.remember({ org_id: "o", project: "p", who: "w", statement: "s" });
  const headers = calls[0].init.headers as Record<string, string>;
  assert.equal(headers.Authorization, "Bearer secret-token");
});

test("remember() returns a 409 conflict body instead of throwing", async () => {
  const conflictBody = { result: "conflict", conflicts: [{ claim: { id: "c1", statement: "existing" } }] };
  const { fn } = stubFetch([{ status: 409, body: conflictBody }]);
  const client = new ErgoClient({ baseUrl: "http://x", fetchImpl: fn });
  const result = await client.remember({ org_id: "o", project: "p", who: "w", statement: "s" });
  assert.equal(result.result, "conflict");
  assert.deepEqual(result.conflicts, conflictBody.conflicts);
});

test("remember() throws ErgoError on a genuine failure status", async () => {
  const { fn } = stubFetch([{ status: 400, body: { error: "missing field: statement" } }]);
  const client = new ErgoClient({ baseUrl: "http://x", fetchImpl: fn });
  await assert.rejects(
    () => client.remember({ org_id: "o", project: "p", who: "w" }),
    (err: unknown) => {
      assert.ok(err instanceof ErgoError);
      assert.equal(err.statusCode, 400);
      assert.deepEqual(err.body, { error: "missing field: statement" });
      return true;
    },
  );
});

test("retract() throws ErgoError on 404 (no active claim in scope) -- retract is never a guarded-OK route", async () => {
  const { fn } = stubFetch([{ status: 404, body: { error: "no active claim c1 in scope" } }]);
  const client = new ErgoClient({ baseUrl: "http://x", fetchImpl: fn });
  await assert.rejects(() => client.retract({ org_id: "o", project: "p", who: "w", claim_id: "c1", reason: "r" }), ErgoError);
});

test("learn()/supersede() also treat 409 as data, not a throw", async () => {
  const { fn: learnFetch } = stubFetch([{ status: 409, body: { result: "conflict" } }]);
  const learnClient = new ErgoClient({ baseUrl: "http://x", fetchImpl: learnFetch });
  const learnResult = await learnClient.learn({ org_id: "o", project: "p", who: "w", statement: "s", source: "src" });
  assert.equal(learnResult.result, "conflict");

  const { fn: supersedeFetch } = stubFetch([{ status: 409, body: { result: "conflict" } }]);
  const supersedeClient = new ErgoClient({ baseUrl: "http://x", fetchImpl: supersedeFetch });
  const supersedeResult = await supersedeClient.supersede({
    org_id: "o",
    project: "p",
    who: "w",
    existing_id: "c1",
    statement: "s",
  });
  assert.equal(supersedeResult.result, "conflict");
});

test("recall() sends org_id/project/q/limit as query params, never in the body", async () => {
  const { fn, calls } = stubFetch([{ status: 200, body: { results: [] } }]);
  const client = new ErgoClient({ baseUrl: "http://x", fetchImpl: fn });
  await client.recall({ org_id: "o", project: "p", q: "deploy days", limit: 7 });
  const url = new URL(calls[0].url);
  assert.equal(url.pathname, "/recall");
  assert.equal(url.searchParams.get("org_id"), "o");
  assert.equal(url.searchParams.get("project"), "p");
  assert.equal(url.searchParams.get("q"), "deploy days");
  assert.equal(url.searchParams.get("limit"), "7");
  assert.equal(calls[0].init.body, undefined);
});

test("baseUrl trailing slashes are normalized so paths never double up", async () => {
  const { fn, calls } = stubFetch([{ status: 200, body: { status: "ok" } }]);
  const client = new ErgoClient({ baseUrl: "http://x///", fetchImpl: fn });
  await client.health();
  assert.equal(calls[0].url, "http://x/health");
});
