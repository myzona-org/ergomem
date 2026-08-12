import { test } from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { createServer } from "node:http";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const SCRIPT = join(dirname(fileURLToPath(import.meta.url)), "..", "install.sh");

function runScript(env: Record<string, string>): Promise<{ code: number | null; stdout: string; stderr: string }> {
  return new Promise((resolve) => {
    const cleanEnv = { ...process.env };
    delete cleanEnv.ERGO_PROJECT;
    delete cleanEnv.ERGO_API_URL;
    const child = spawn("bash", [SCRIPT], { env: { ...cleanEnv, ...env } });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (d) => (stdout += d));
    child.stderr.on("data", (d) => (stderr += d));
    child.on("close", (code) => resolve({ code, stdout, stderr }));
  });
}

test("install.sh exits nonzero with a helpful message when ERGO_PROJECT is unset", async () => {
  const { code, stderr } = await runScript({});
  assert.notEqual(code, 0);
  assert.match(stderr, /ERGO_PROJECT is not set/);
});

test("install.sh exits nonzero with a helpful message when ERGO_PROJECT would break JSON", async () => {
  const { code, stderr } = await runScript({ ERGO_PROJECT: 'has"quote' });
  assert.notEqual(code, 0);
  assert.match(stderr, /quote, backslash, or control character/);
});

test("install.sh exits nonzero with a helpful message when ERGO_API_URL would break JSON", async () => {
  const { code, stderr } = await runScript({ ERGO_PROJECT: "test-project", ERGO_API_URL: 'http://127.0.0.1:8788"evil' });
  assert.notEqual(code, 0);
  assert.match(stderr, /ERGO_API_URL contains a quote, backslash, or control character/);
});

test("install.sh exits nonzero with a helpful message when the engine is unreachable", async () => {
  const { code, stderr } = await runScript({ ERGO_PROJECT: "test-project", ERGO_API_URL: "http://127.0.0.1:1" });
  assert.notEqual(code, 0);
  assert.match(stderr, /not reachable/i);
});

test("install.sh prints a config block that parses as JSON when the engine is reachable", async () => {
  const server = createServer((_req, res) => {
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify({ status: "ok" }));
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const { port } = server.address() as { port: number };

  try {
    const { code, stdout } = await runScript({ ERGO_API_URL: `http://127.0.0.1:${port}`, ERGO_PROJECT: "test-project" });
    assert.equal(code, 0);

    const jsonStart = stdout.indexOf("{");
    assert.notEqual(jsonStart, -1, "expected a JSON config block in the output");
    const parsed = JSON.parse(stdout.slice(jsonStart));
    assert.equal(parsed.plugins.slots.memory, "memory-ergo");
    assert.equal(parsed.plugins.entries["memory-ergo"].config.baseUrl, `http://127.0.0.1:${port}`);
    assert.equal(parsed.plugins.entries["memory-ergo"].config.project, "test-project");
  } finally {
    server.close();
  }
});
