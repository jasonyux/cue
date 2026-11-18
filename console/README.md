# Console

The Next.js console for Cue's Python backend. Launch it from the repository root:

```bash
pnpm cue web   # what `pnpm run build` made
pnpm cue dev   # the dev server: every theme and the debug pages too
```

It serves [http://127.0.0.1:3000](http://127.0.0.1:3000): `/` opens the landing page at `/v1`; Notes, History, Usage and Settings are pages under it.

## Configuration and startup

The root launcher sets `CUA_BACKEND=python` and one `RUNNER_BASE_URL`, defaulting to the Python backend on **4041**. It honors `HOST`/`PORT` and an explicit `RUNNER_BASE_URL` override. The root `.env` and launching shell configure model defaults; the console reads them through `/api/capabilities`. The console sends `X-CUA-Backend` on JSON requests; the backend rejects mismatches before performing actions.

The Next.js routes and global styles live in `app/`. The pages and their themes live in `components/v1/`, what the themes share (and the Debug page) in `components/live/`; `tests/` covers them.

Shared request and event types come from [`contracts`](../contracts/). The console contains no model-request loop or execution runtime.

## Checks and production

Development uses `.next-dev`; build, start, and type generation use `.next`, so a production build does not overwrite a running development server’s output.

From the repository root:

```bash
pnpm --filter @cue/console test
pnpm --filter @cue/console typecheck
pnpm run build
pnpm cue web
```

Ctrl+C requests graceful cleanup of the backend and console. See the [root guide](../README.md) for installation.
