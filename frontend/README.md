# Clams v2 — Control room

Vite + React + TypeScript + Tailwind + TanStack Query SPA for the Clams v2
orchestrator. Talks to the backend API on the same origin under `/api/v1`.

## Develop

```sh
npm install
npm run dev      # Vite dev server on :5173, proxies /api + /healthz to :8200
```

## Build

```sh
npm run build    # tsc -b && vite build -> dist/
npm run preview
```

The built `dist/` is served by the Clams API process in production.

## Notes

- The API bearer token is entered via the gear (Settings) popover and stored in
  `localStorage` under `clams_api_token`; every request sends it as
  `Authorization: Bearer <token>`.
- Live updates come from the global `EventSource` on `/api/v1/events`; each
  frame invalidates all TanStack Query caches. `EventSource` cannot send
  headers, so the token is also passed as a `?token=` query parameter for the
  stream.
- Commands (`POST /api/v1/commands`) show a pending indicator and poll
  `GET /api/v1/commands/{id}` every 2 s until the status becomes
  `applied`/`rejected`, then a toast reports the outcome.
- Dark mode follows `prefers-color-scheme` until toggled manually (gear-free,
  header button; stored under `clams_theme`).
