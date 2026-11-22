# Bot Points v3.9.9 — standalone Render deployment

This is a **preparation branch**, not a production-ready deployment.

Source archive: `BOT_Point_v3.9.9_GitHub_Release_CLEAN(1).zip`. The archive contains Flask, Telethon, Google Sheets and SQLite logic; the source still needs to be committed to this branch before deploying.

## Mandatory gates
1. Keep StaffFlow's existing Render service and repository unchanged.
2. Do **not** deploy the unmodified Flask app publicly. The source currently has no StaffFlow-authenticated access boundary, and it has write endpoints.
3. Implement an authenticated StaffFlow-to-Bot-Points gateway, deny direct unauthenticated access to every write route, and configure explicit origin and CSRF protections.
4. Replace local `points.db` with persistent PostgreSQL, or provision and verify a persistent disk with backup/recovery before enabling any writes. A free Render web service has ephemeral storage.
5. Store Telegram bot token, Telethon session/API credentials, Google service account and Flask secret as Render secrets. Never commit sessions or .env files.
6. Bind to Render's `PORT` and `0.0.0.0` only after authentication has been implemented.
7. Start Telegram polling only once per deployment and handle restarts safely; verify no duplicate writes.
8. Add a health check and end-to-end tests; keep auto-deploy off until verified.
9. StaffFlow SSO should be the only user-facing login; Telegram credentials are a server integration, not a second user login.

## Target deployment
- Render workspace: StaffFlow
- Region: Frankfurt
- Separate web service and independent releases
- Repo: `Deline123/bot_points_update`
- Build: `pip install -r requirements.txt`
- Start: secure wrapper around Bot Points (to be implemented)
- Auto-deploy: disabled until security and persistence gates pass

No existing StaffFlow code or deployment should be modified for this preparation.
