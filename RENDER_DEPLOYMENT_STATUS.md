# Bot Points — isolated Render deployment

This branch currently contains only the fail-closed gateway and a maintenance-mode entrypoint. The complete v3.9.9 application code has NOT been committed. Do not consider this a production-ready Bot Points deployment.

The separate Neon project is dark-poetry-61483506, database bot_points; schema has been initialized.

Before switching start command to the real application:
- Commit complete reviewed Bot Points source.
- Verify PostgreSQL queries and migrations with a real database.
- Store Neon DATABASE_URL, BOT_POINTS_GATEWAY_SECRET, Telegram and Google credentials as Render secrets.
- Integrate a server-side signed reverse proxy in StaffFlow.
- Confirm access control, backup/rollback and background worker behavior.
