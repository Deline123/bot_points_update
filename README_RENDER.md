# Standalone Bot Points on Render

This branch is a **safe deployment scaffold**, not a working Bot Points release.

The uploaded v3.9.9 ZIP must be added as reviewed source code before enabling
the Telegram worker, Google Sheets writes or browser UI. The original app has
a localhost-only write guard and uses SQLite on ephemeral disk, so publishing
it unchanged would be insecure and lose state on Render.

Current entrypoint: `python render_entry.py`. It exposes `/health` with
`ready:false`, and all application operations return HTTP 503.

To finish: import reviewed v3.9.9 source, add StaffFlow-authenticated API
gateway, move state to durable PostgreSQL, configure service-level Telethon
session, add concurrency and security tests, then enable real entrypoint.
Auto-deploy is disabled pending explicit approval.
