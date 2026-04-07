# Scripts Directory

This directory contains helper scripts used to **provision and run the staging environment
on a fresh temporary server**.

The staging servers used for experiments are **ephemeral**. When the node reboots or
expires, the disk resets to the base OS image. Any local state (Docker volumes, database
files, Grafana data, etc.) is lost.
These scripts make it easy to recreate the environment quickly on a new machine.

# Overview

Typical workflow when starting a new staging server:

1.  SSH into the server
2.  Clone this repository
3.  Run the bootstrap script to install system dependencies
4.  Start the staging services
5.  Optionally restore staging data

Example:

```bash
git clone <repo-url>
cd hybridInference/scripts/staging

chmod +x *.sh

./bootstrap_server.sh
newgrp docker

./start_staging.sh
```

# Scripts

## bootstrap-server.sh

Prepares a fresh Ubuntu server for running the staging stack.

Responsibilities:
-   installs required system packages
-   installs Docker Engine
-   installs Docker Compose
-   installs `uv` (Python dependency tool)
-   adds the current user to the `docker` group
-   configures the shell so `uv` is available

This script only needs to run **once per server**.
After running it you must either:

```bash
newgrp docker
```
or log out and SSH back in.

## start-staging.sh

Starts the staging environment and refreshes the application service.

Responsibilities:
- changes to the repository root
- loads `uv` into the shell environment if available
- installs or syncs Python dependencies with `uv sync`
- pulls infrastructure images using the staging compose file
- starts infrastructure containers in detached mode
- prints container status
- restarts the `hybrid_inference.staging` systemd service
- shows the current service status

### Verify installation

```bash
docker --version
docker compose version
uv --version
```

## Security Note

The staging gateway binds to `0.0.0.0:8000`, making it reachable on the
server's public IP.  Before exposing the service, either:

- Set `USER_AUTH_ENABLED=1` in `.env` to require API-key authentication, or
- Restrict access with a firewall rule (e.g. `sudo ufw allow from <your-ip> to any port 8000`).

## Services Started by the Stack
After everything starts, the server runs several services:
- FastAPI Gateway: Main API server for LLM requests (8000)
- PostgreSQL: Stores logs, users, API keys (5433)
- Prometheus: Collects application metrics (9091)
- Grafana: Visual dashboard for metrics (3001)
