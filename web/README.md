# Xun web

Vue 3 and TypeScript frontend for `xun.WebDisplay`.

```bash
npm install
npm run dev
```

Open `http://127.0.0.1:5173`. This one command starts a local `xuns` backend for the repository root and Vite with Vue DevTools. Vite proxies `/api/sessions` and `/session` to FastAPI at `http://127.0.0.1:18960` and authenticates them with a development-only token.

To use a backend you manage separately, run only the UI and configure its proxy:

```bash
VITE_XUN_BACKEND=http://127.0.0.1:18960 \
VITE_XUN_TOKEN=your-token \
npm run dev:ui
```

There is no backend-to-Vite redirect in development. The browser loads the UI directly from Vite, while API and WebSocket requests are proxied to the backend.

To develop with a non-root service path, start the backend and Vite with the same prefix in separate terminals:

```bash
uv run --project .. xuns .. --host 127.0.0.1 --token xun-dev --base-path /alpha

VITE_XUN_BASE_PATH=/alpha \
VITE_XUN_BACKEND=http://127.0.0.1:18960 \
VITE_XUN_TOKEN=xun-dev \
npm run dev:ui
```

Open `http://127.0.0.1:5173/`; the UI normalizes the URL to `/alpha/chat/` and Vite proxies `/alpha/api/sessions` and `/alpha/session`.

```bash
npm run build
```

The production build is written to `../src/xun/assets/web`, where `WebDisplayService` serves it at `<base_path>/chat/` and the Python wheel includes it as package data.
