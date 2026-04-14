# diun-dispatcher

> Webhook bridge between [Diun](https://crazymax.dev/diun/) and [Coolify](https://coolify.io/) — automatically redeploy containers when a new image is available, with notifications via [Apprise](https://github.com/caronc/apprise).

[![Build & Push Docker image](https://github.com/YOUR_GITHUB_USERNAME/diun-dispatcher/actions/workflows/docker.yml/badge.svg)](https://github.com/YOUR_GITHUB_USERNAME/diun-dispatcher/actions/workflows/docker.yml)
[![GitHub Container Registry](https://img.shields.io/badge/ghcr.io-diun--dispatcher-blue?logo=docker)](https://ghcr.io/YOUR_GITHUB_USERNAME/diun-dispatcher)

## How it works

```
Diun (server A)  ──�
                   ├──► diun-dispatcher ──► Coolify redeploy
Diun (server B)  ──┘                   └──► Apprise notification
```

1. Diun detects a new image and sends a POST webhook
2. diun-dispatcher matches `hostname + container_name` to a Coolify deployment UUID
3. Coolify redeploys the container with the new image
4. A notification is sent via Apprise (Pushover, ntfy, Telegram, Gotify, Discord, etc.)

## Quick start

```bash
docker run -d \
  --name diun-dispatcher \
  -p 8000:8000 \
  -e COOLIFY_URL="https://coolify.example.com" \
  -e COOLIFY_TOKEN="your-token" \
  -e APPRISE_URLS="pover://USER_KEY@APP_TOKEN" \
  -e WEBHOOK_SECRET="change-me" \
  -e MAPPINGS='{"my-server":{"my-container":"coolify-uuid"}}' \
  ghcr.io/YOUR_GITHUB_USERNAME/diun-dispatcher:latest
```

Or with Docker Compose — see [`docker-compose.yml`](docker-compose.yml).

## Environment variables

| Variable         | Required | Description |
|------------------|----------|-------------|
| `COOLIFY_URL`    | Yes      | Base URL of your Coolify instance |
| `COOLIFY_TOKEN`  | Yes      | Coolify API token |
| `APPRISE_URLS`   | No       | Comma-separated list of Apprise notification URLs |
| `WEBHOOK_SECRET` | No       | Shared secret validated against `X-Diun-Secret` header |
| `MAPPINGS`       | No       | JSON mapping `hostname → container → Coolify UUID` |

### MAPPINGS format

```json
{
  "my-server": {
    "ntfy": "coolify-uuid-aaa",
    "vaultwarden": "coolify-uuid-bbb"
  },
  "my-other-server": {
    "uptime-kuma": "coolify-uuid-ccc"
  }
}
```

The hostname must match the `hostname:` field set in your Diun `docker-compose.yml`.

The Coolify UUID is visible in the deploy webhook URL of each resource:
`https://coolify.example.com/api/v1/deploy?uuid=THIS_IS_THE_UUID`

### APPRISE_URLS examples

| Service   | URL format                              |
|-----------|-----------------------------------------|
| Pushover  | `pover://USER_KEY@APP_TOKEN`            |
| ntfy      | `ntfy://ntfy.example.com/topic`         |
| Telegram  | `tgram://BOTTOKEN/CHATID`               |
| Gotify    | `gotify://hostname/token`               |
| Discord   | `discord://webhook_id/webhook_token`    |
| Email     | `mailto://user:pass@gmail.com`          |

Full list: https://github.com/caronc/apprise/wiki

## Diun configuration

In each Diun `docker-compose.yml`, set the `hostname` (used for mapping) and configure the webhook notifier:

```yaml
services:
  diun:
    image: crazymax/diun:latest
    hostname: my-server        # must match your MAPPINGS key
    command: serve
    volumes:
      - "./data:/data"
      - "/var/run/docker.sock:/var/run/docker.sock"
    environment:
      - TZ=Europe/Paris
      - DIUN_WATCH_SCHEDULE=0 */6 * * *
      - DIUN_PROVIDERS_DOCKER=true
      - DIUN_PROVIDERS_DOCKER_WATCHBYDEFAULT=true
      - DIUN_NOTIF_WEBHOOK_ENDPOINT=https://diun-dispatcher.example.com/webhook
      - DIUN_NOTIF_WEBHOOK_METHOD=POST
      - DIUN_NOTIF_WEBHOOK_HEADERS_X-Diun-Secret=change-me
    restart: unless-stopped
```

## Endpoints

| Method | Path       | Description              |
|--------|------------|--------------------------|
| POST   | `/webhook` | Receives Diun events     |
| GET    | `/health`  | Health check             |

## License

MIT
