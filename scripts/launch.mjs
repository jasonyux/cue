import { spawn } from "node:child_process";
import { randomUUID } from "node:crypto";
import { existsSync } from "node:fs";
import { createServer } from "node:net";
import { fileURLToPath, pathToFileURL } from "node:url";
import { join } from "node:path";
import { setTimeout as delay } from "node:timers/promises";

const root = fileURLToPath(new URL("../", import.meta.url));

export function requestServiceShutdown(child, managed) {
  // Node's SIGTERM force-kills Windows children. EOF lets the backend
  // complete its own teardown before the launcher's deadline expires.
  if (managed && child.stdin) child.stdin.end();
  else child.kill("SIGTERM");
}

// ``env`` gives the launch settings (HOST, PORT, RUNNER_BASE_URL); the services get ``shell`` with them.
export function launchConfiguration(production = false, env = process.env, shell = env) {
  const port = Number(env.PORT ?? 4041);
  if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error("PORT must be an integer between 1 and 65535.");
  const host = env.HOST ?? "127.0.0.1";
  const connectHost = host === "0.0.0.0" ? "127.0.0.1" : host === "::" ? "[::1]" : host.includes(":") ? `[${host.replace(/^\[|\]$/g, "")}]` : host;
  const runnerBaseUrl = env.RUNNER_BASE_URL ?? `http://${connectHost}:${port}`;
  const python = join(root, "python-app", ".venv", process.platform === "win32" ? "Scripts/python.exe" : "bin/python");
  return {
    command: python,
    args: ["-m", "app"],
    runnerBaseUrl,
    env: { ...shell, HOST: host, PORT: String(port), CUA_BACKEND: "python", RUNNER_BASE_URL: runnerBaseUrl },
    consoleCommand: production ? "start" : "dev",
  };
}

export async function assertPortAvailable(host, port, label) {
  const listener = createServer(socket => socket.resetAndDestroy());
  try {
    await new Promise((resolve, reject) => {
      listener.once("error", reject);
      listener.listen({ host, port, exclusive: true }, resolve);
    });
  } catch (error) {
    if (error.code === "EADDRINUSE") {
      const address = `${host.includes(":") ? `[${host}]` : host}:${port}`;
      throw new Error(`${label} address ${address} is already in use. Stop the original launch with Ctrl+C in its terminal, wait for shutdown, then retry. If another application owns this port, stop it before launching the sample app.`);
    }
    throw error;
  }
  await new Promise((resolve, reject) => listener.close(error => error ? reject(error) : resolve()));
}

async function preflightLaunchPorts(config) {
  // These probes improve startup errors; the backend lease and instance check
  // still protect against another launch winning a race after the probes close.
  await assertPortAvailable("127.0.0.1", 4050, "Backend coordination");
  await assertPortAvailable(config.env.HOST, Number(config.env.PORT), "Backend");
  await assertPortAvailable("127.0.0.1", 3000, "Console");
}

export async function waitForBackend(baseUrl, { signal, isAlive = () => true, timeoutMs = 30_000, instanceId } = {}) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    signal?.throwIfAborted();
    if (!isAlive()) throw new Error("The backend exited before becoming ready.");
    try {
      const timeout = AbortSignal.timeout(Math.min(2_000, Math.max(1, deadline - Date.now())));
      const response = await fetch(`${baseUrl}/api/capabilities`, { signal: signal ? AbortSignal.any([signal, timeout]) : timeout });
      if (response.ok) {
        const capabilities = await response.json();
        if (capabilities.backendId !== "python") throw new Error("Backend identity mismatch. Stop the existing backend before launching this console.");
        if (instanceId && capabilities.instanceId !== instanceId) throw new Error("Backend identity mismatch: this address belongs to an earlier launch.");
        // Recheck child status: an existing service must not satisfy a failed launch.
        if (!isAlive()) throw new Error("The backend exited before becoming ready.");
        return capabilities;
      }
    } catch (error) {
      if (signal?.aborted || /identity mismatch|backend exited/.test(String(error))) throw error;
    }
    await delay(Math.min(100, Math.max(1, deadline - Date.now())), undefined, { signal });
  }
  throw new Error(`The backend did not become ready at ${baseUrl}.`);
}

// ``pnpm cue web`` serves what ``pnpm run build`` made, without rebuilding; ``pnpm cue dev`` runs the console's dev
// server, which keeps every theme and the debug pages.
async function main() {
  const [mode, ...rest] = process.argv.slice(2);
  if (rest.length || (mode !== "web" && mode !== "dev")) throw new Error("Use pnpm cue web, or pnpm cue dev for the dev server.");
  // The backend reads .env itself, and again when the settings page saves it, so it gets only the shell's variables and
  // knows those win over the file. The launcher reads it for its own settings (HOST, PORT).
  const shell = { ...process.env };
  const envPath = join(root, ".env");
  if (existsSync(envPath)) process.loadEnvFile(envPath);
  const config = launchConfiguration(mode === "web", process.env, shell);
  config.env.CUA_INSTANCE_ID = randomUUID();
  if (!existsSync(config.command)) throw new Error("The Python environment is missing. Run pnpm run build.");
  if (mode === "web" && !existsSync(join(root, "console/.next/BUILD_ID"))) throw new Error("The console is not built. Run pnpm run build.");
  await preflightLaunchPorts(config);
  const controller = new AbortController();
  const children = [];
  let closing;
  function stop() {
    return closing ??= (async () => {
      controller.abort();
      const running = children.filter(item => item.child.exitCode === null && item.child.signalCode === null);
      for (const item of running) requestServiceShutdown(item.child, item.managed);
      for (const item of running) {
        let timer;
        const exited = await Promise.race([
          item.done.then(() => true),
          new Promise(resolve => { timer = setTimeout(() => resolve(false), 25_000); }),
        ]);
        clearTimeout(timer);
        if (!exited) {
          console.error("A service did not stop within 25 seconds; forcing termination. Check desktop input before restarting.");
          item.child.kill("SIGKILL");
          await item.done;
        }
      }
    })();
  }
  const onSignal = () => { void stop(); };
  process.once("SIGINT", onSignal);
  process.once("SIGTERM", onSignal);
  function start(command, args, cwd, env, managed = false) {
    const child = spawn(command, args, {
      cwd, env: { ...env, CUA_MANAGED_LAUNCH: managed ? "1" : "0" },
      stdio: [managed ? "pipe" : "inherit", "inherit", "inherit"],
    });
    child.stdin?.on("error", () => { /* A backend may exit before its shutdown request. */ });
    let startupError;
    const done = new Promise(resolve => {
      child.once("error", error => { startupError = error; resolve({ code: 1, error }); });
      child.once("exit", (code, signal) => resolve({ code, signal }));
    });
    const item = { child, done, managed, isAlive: () => !startupError && child.exitCode === null && child.signalCode === null };
    children.push(item);
    return item;
  }
  try {
    const runner = start(config.command, config.args, root, config.env, true);
    await waitForBackend(config.runnerBaseUrl, {
      signal: controller.signal, isAlive: runner.isAlive, instanceId: config.env.CUA_INSTANCE_ID,
    });
    const consoleEnv = { ...config.env };
    delete consoleEnv.OPENAI_API_KEY;
    const consoleApp = start(process.execPath,
      [join(root, "console/node_modules/next/dist/bin/next"), config.consoleCommand, "--hostname", "127.0.0.1", "--port", "3000"],
      join(root, "console"), consoleEnv);
    const result = await Promise.race([runner.done, consoleApp.done]);
    if (!controller.signal.aborted && result.code !== 0) throw result.error ?? new Error("A service exited unexpectedly; stopping the app.");
  } catch (error) {
    if (!controller.signal.aborted) throw error;
  } finally {
    await stop();
    process.removeListener("SIGINT", onSignal);
    process.removeListener("SIGTERM", onSignal);
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  await main().catch(error => { console.error(error.message); process.exitCode = 1; });
}
