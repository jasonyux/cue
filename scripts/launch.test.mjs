import { test } from "node:test";
import assert from "node:assert/strict";
import { createServer } from "node:http";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { fileURLToPath } from "node:url";
import { setTimeout as delay } from "node:timers/promises";
import { assertPortAvailable, launchConfiguration, requestServiceShutdown, waitForBackend } from "./launch.mjs";

test("launch configuration names the Python runner and one console identity", () => {
  const py = launchConfiguration(true, {});
  assert.equal(py.env.CUA_BACKEND, "python");
  assert.equal(py.runnerBaseUrl, "http://127.0.0.1:4041");
  assert.deepEqual(py.args, ["-m", "app"]);
  assert.equal(py.consoleCommand, "start");
  assert.equal(launchConfiguration(false, {}).consoleCommand, "dev");
  assert.throws(() => launchConfiguration(false, { PORT: "0" }), /PORT/);
  const files = launchConfiguration(false, { PORT: "4100", CUA_SUMMARY_MODEL: "m" }, { HOME: "/h" });  // the env files' and the shell's
  assert.deepEqual([files.env.PORT, files.env.HOME, files.env.CUA_SUMMARY_MODEL], ["4100", "/h", undefined]);
});

test("port preflight reports the occupied address without disturbing its listener", async () => {
  const server = createServer((_request, response) => response.end("existing service"));
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  const port = server.address().port;
  try {
    await assert.rejects(assertPortAvailable("127.0.0.1", port, "Backend"), error => {
      assert.match(error.message, new RegExp(`127\\.0\\.0\\.1:${port} is already in use`));
      assert.match(error.message, /Ctrl\+C.*wait for shutdown.*retry/);
      return true;
    });
    assert.equal(await (await fetch(`http://127.0.0.1:${port}`)).text(), "existing service");
  } finally { await new Promise(resolve => server.close(resolve)); }
});

test("port preflight releases an available port", async () => {
  const server = createServer();
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  const port = server.address().port;
  await new Promise(resolve => server.close(resolve));
  await assertPortAvailable("127.0.0.1", port, "Console");
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(port, "127.0.0.1", resolve);
  });
  await new Promise(resolve => server.close(resolve));
});

test("occupied coordination port rejects a launch before its runner starts", async () => {
  const coordination = createServer();
  await new Promise((resolve, reject) => {
    coordination.once("error", reject);
    coordination.listen(4050, "127.0.0.1", resolve);
  });
  const child = spawn(process.execPath, [fileURLToPath(new URL("./launch.mjs", import.meta.url)), "dev"], {
    env: { ...process.env, OPENAI_API_KEY: "" }, stdio: ["ignore", "pipe", "pipe"],
  });
  const exited = once(child, "exit");
  let stdout = "";
  let stderr = "";
  child.stdout.setEncoding("utf8").on("data", chunk => { stdout += chunk; });
  child.stderr.setEncoding("utf8").on("data", chunk => { stderr += chunk; });
  try {
    const status = await Promise.race([exited, delay(5_000, "timeout", { ref: false })]);
    assert.deepEqual(status, [1, null], stderr);
    assert.match(stderr, /Backend coordination address 127\.0\.0\.1:4050 is already in use/);
    assert.doesNotMatch(stderr, /Runner failed to start|identity mismatch/);
    assert.equal(stdout, "");
    assert.equal(coordination.listening, true);
  } finally {
    if (child.exitCode === null && child.signalCode === null) { child.kill("SIGKILL"); await exited; }
    await new Promise(resolve => coordination.close(resolve));
  }
});

test("a launch that names neither web nor dev says how to launch and starts nothing", async () => {
  for (const args of [[], ["start"], ["web", "extra"]]) {
    const child = spawn(process.execPath, [fileURLToPath(new URL("./launch.mjs", import.meta.url)), ...args], { stdio: ["ignore", "pipe", "pipe"] });
    let stderr = "";
    child.stderr.setEncoding("utf8").on("data", chunk => { stderr += chunk; });
    assert.deepEqual(await once(child, "exit"), [1, null]);
    assert.match(stderr, /Use pnpm cue web, or pnpm cue dev/);
  }
});

test("managed shutdown closes the pipe instead of sending a forceful Windows signal", () => {
  const calls = [];
  const child = { stdin: { end: () => calls.push("eof") }, kill: signal => calls.push(signal) };
  requestServiceShutdown(child, true);
  assert.deepEqual(calls, ["eof"]);
});

test("readiness refuses a different running backend or dead child", async () => {
  let backendId = "other";
  const server = createServer((_request, response) => {
    response.setHeader("content-type", "application/json");
    response.end(JSON.stringify({ backendId }));
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  const url = `http://127.0.0.1:${server.address().port}`;
  try {
    await assert.rejects(waitForBackend(url), /identity mismatch/);
    backendId = "python";
    await assert.rejects(waitForBackend(url, { instanceId: "new-launch" }), /earlier launch/);
    await assert.rejects(waitForBackend(url, { isAlive: () => false }), /exited/);
    assert.equal((await waitForBackend(url)).backendId, "python");
  } finally { await new Promise(resolve => server.close(resolve)); }
});
