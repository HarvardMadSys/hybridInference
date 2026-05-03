# PostgreSQL Admin Playbook

## Environment Variables
- Update `.env` with `PGADMIN_EMAIL`, `PGADMIN_PASSWORD`, and `PGADMIN_CONFIG_SERVER_MODE=True`.
- Master password support is optional; skip `PGADMIN_CONFIG_MASTER_PASSWORD_REQUIRED` and `PGADMIN_CONFIG_MASTER_PASSWORD` if the team does not use it.
- Keep `.env` out of version control and rotate the secrets periodically.

## Restart Admin Stack
```bash
# Stop and restart pgAdmin (data is in hybridinference_pgadmin_data volume)
make down
make up

# Or to fully reset pgAdmin (destroys saved connections):
docker compose -f infrastructure/docker/docker-compose.yml --env-file .env --profile admin down
docker volume rm hybridinference_pgadmin_data
docker compose -f infrastructure/docker/docker-compose.yml --env-file .env --profile admin up -d
```

## Access pgAdmin Securely
- SSH into the host with local port forwarding: `ssh -L 5050:127.0.0.1:5050 user@freeinference.org`.
- Open `http://localhost:5050`, sign in with the configured email/password. If a master password is configured, unlock with it; otherwise continue directly.

## Register Primary Database
1. In pgAdmin, right-click `Servers` → `Register` → `Server`.
2. General tab: name the connection (e.g., `Primary Postgres`).
3. Connection tab:
   - `Host name/address`: `postgres`
   - `Port`: `5432`
   - `Maintenance database`: value of `DB_NAME`
   - `Username`: value of `DB_USER`
   - `Password`: value of `DB_PASSWORD`
4. Save and expand the new server to inspect `freeinference_db`; leave the default `postgres` database for maintenance tasks only.

## Post-Restart Checks
- `make ps` to confirm bindings on `127.0.0.1`.
- Run a smoke query via pgAdmin (e.g., `SELECT 1;`).
- Log findings in the ops channel and schedule password rotation reminders.
