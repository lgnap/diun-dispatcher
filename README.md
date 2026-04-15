# diun-dispatcher

> Webhook bridge between [Diun](https://crazymax.dev/diun/) and [Coolify](https://coolify.io/) — automatically redeploy containers when a new image is available, with notifications via [Apprise](https://github.com/caronc/apprise).

Receives image update notifications from Diun, automatically triggers redeployment in Coolify by matching container images, and sends notifications to your preferred channels (Pushover, ntfy, Telegram, Discord, Slack, etc.).

## How it works

```
Diun (multiple servers)
           ↓
      POST /webhook
           ↓
  diun-dispatcher
      ↙         ↘
Coolify API   Apprise
  (deploy)   (notify)
```

1. **Diun** detects a new container image and sends a POST webhook
2. **diun-dispatcher** queries Coolify API to find the service using that image
3. **Coolify** redeploys the service with the new image
4. **Apprise** sends a notification with deployment status and manual deploy link

## Quick start

### Docker

```bash
docker run -d \
  --name diun-dispatcher \
  -p 8000:8000 \
  -e COOLIFY_URL="https://coolify.example.com" \
  -e COOLIFY_TOKEN="your-coolify-api-token" \
  -e APPRISE_URLS="ntfy://ntfy.example.com/topic" \
  -e WEBHOOK_SECRET="your-secret-key" \
  ghcr.io/lgnap/diun-dispatcher:latest
```

### Docker Compose

See [`docker-compose.yml`](docker-compose.yml) for a complete example.

## Configuration

### Required environment variables

| Variable        | Description |
|-----------------|-------------|
| `COOLIFY_URL`   | Base URL of your Coolify instance (e.g., `https://coolify.example.com`) |
| `COOLIFY_TOKEN` | Coolify API token (generate in Settings → API) |

### Optional environment variables

| Variable              | Default | Description |
|-----------------------|---------|-------------|
| `WEBHOOK_SECRET`      | (none)  | Shared secret for webhook validation (validates `X-Diun-Secret` header) |
| `DISPATCHER_URL`      | (none)  | Your dispatcher URL for manual deploy links in notifications (e.g., `https://dispatcher.example.com`) |
| `APPRISE_URLS`        | (none)  | Comma-separated Apprise notification URLs (see examples below) |
| `IGNORE_CONTAINERS`   | (none)  | Comma-separated container names to skip (e.g., `test-app,staging-db`) |
| `CACHE_FILE`          | `/data/uuid_cache.json` | Path for UUID cache file |
| `CF_ACCESS_CLIENT_ID` | (none)  | Cloudflare Access client ID (if behind Cloudflare Access) |
| `CF_ACCESS_CLIENT_SECRET` | (none) | Cloudflare Access client secret |

### Apprise notification URLs

The dispatcher supports any Apprise notification service:

| Service   | URL format                           | Notes |
|-----------|--------------------------------------|-------|
| ntfy      | `ntfy://ntfy.example.com/topic`      | Self-hosted or ntfy.sh |
| Pushover  | `pover://USER_KEY@APP_TOKEN`         | Mobile notifications |
| Telegram  | `tgram://BOT_TOKEN/CHAT_ID`          | Bot must be in chat |
| Discord   | `discord://webhook_id/webhook_token` | Use webhook URL |
| Gotify    | `gotify://host/token`                | Self-hosted |
| Slack     | `slack://token-a/token-b/token-c`   | Using webhook |
| Email     | `mailto://user:pass@gmail.com`       | SMTP credentials |

**Full list:** https://github.com/caronc/apprise/wiki

Example with multiple services:
```bash
APPRISE_URLS="ntfy://ntfy.example.com/deployments,discord://webhook_id/token,slack://webhook"
```

## Setting up Diun

In each Diun instance, configure the webhook to point to your dispatcher:

```yaml
services:
  diun:
    image: crazymax/diun:latest
    hostname: production-server      # Used in notifications
    command: serve
    volumes:
      - "./data:/data"
      - "/var/run/docker.sock:/var/run/docker.sock"
    environment:
      - TZ=Europe/Paris
      - DIUN_WATCH_SCHEDULE=0 */6 * * *
      - DIUN_PROVIDERS_DOCKER=true
      - DIUN_PROVIDERS_DOCKER_WATCHBYDEFAULT=true
      - DIUN_NOTIF_WEBHOOK_ENDPOINT=https://dispatcher.example.com/webhook
      - DIUN_NOTIF_WEBHOOK_METHOD=POST
      - DIUN_NOTIF_WEBHOOK_HEADERS_X-Diun-Secret=your-secret-key
    restart: unless-stopped
```

### Key points

- **`hostname`** is displayed in notifications — use a meaningful name (e.g., `production-server`, `staging-app`)
- **`X-Diun-Secret`** header must match `WEBHOOK_SECRET` in dispatcher if validation is enabled
- **`DIUN_WATCH_SCHEDULE`** controls how often Diun checks for new images (cron format)

## How image matching works

When Diun sends a webhook with a container image (e.g., `ghcr.io/music-assistant/server:latest`):

1. **dispatcher queries** Coolify API to list all services and databases
2. **Compares** the image name against all deployed containers
3. **Finds matching service** by normalized image name
4. **Triggers redeploy** if found

### Image normalization

- `docker.io/my-app:latest` → `my-app`
- `ghcr.io/user/app:v1.0.0` → `user/app`
- `registry.example.com/app:tag` → `registry.example.com/app`

The dispatcher handles these automatically — no manual mapping needed.

## API endpoints

### POST `/webhook`

Receives Diun webhook events.

**Headers:**
- `X-Diun-Secret` (optional): Must match `WEBHOOK_SECRET` if set
- `Content-Type: application/json`

**Body:**
```json
{
  "hostname": "production-server",
  "status": "new",
  "image": "ghcr.io/music-assistant/server:latest",
  "metadata": {
    "ctn_names": "music-assistant"
  }
}
```

**Response:**
```json
{
  "ok": true,
  "uuid": "a1b2c3d4"
}
```

### GET `/deploy`

Manually trigger a redeployment. Used in notification links.

**Parameters:**
- `uuid` (string): Service UUID (short form cached, or full UUID)
- `secret` (string): Must match `WEBHOOK_SECRET`

**Response:**
```json
{
  "ok": true,
  "deployed": true
}
```

### GET `/health`

Health check endpoint.

**Response:**
```json
{
  "status": "ok"
}
```

## Notification format

Notifications include:

- **Server name** (from Diun hostname)
- **Container name**
- **Image name**
- **Deploy link** (if `DISPATCHER_URL` configured and `WEBHOOK_SECRET` set)

Example:
```
🆕 music-assistant — new image available

🖥️ Server: production-server
🖼️ Image: ghcr.io/music-assistant/server:latest
📦 Container: music-assistant

🚀 Déployer [a1b2c3d4]: https://dispatcher.example.com/deploy?uuid=...
```

## Cloudflare Access

If your Coolify instance is protected by [Cloudflare Access](https://www.cloudflare.com/zero-trust/products/access/):

```bash
docker run -d \
  --name diun-dispatcher \
  -p 8000:8000 \
  -e COOLIFY_URL="https://coolify.example.com" \
  -e COOLIFY_TOKEN="your-token" \
  -e CF_ACCESS_CLIENT_ID="your-client-id" \
  -e CF_ACCESS_CLIENT_SECRET="your-client-secret" \
  -e APPRISE_URLS="ntfy://..." \
  ghcr.io/lgnap/diun-dispatcher:latest
```

The dispatcher automatically adds required Cloudflare Access headers to API requests.

## Architecture

### Caching

- **UUID mappings** are cached in-memory with disk persistence
- Cache entries expire after 7 days
- Max 100 concurrent cache entries
- Zero per-request disk I/O — lookups are O(1)
- Automatically loaded at startup, saved at shutdown

### Performance

- Image matching is O(n) where n = total containers in Coolify
- Cache lookups are O(1)
- No persistent database needed

## Troubleshooting

### Webhook not received

- Check firewall rules and port forwarding (default: `8000`)
- Verify DNS resolution: `curl https://dispatcher.example.com/health`
- Check dispatcher logs: `docker logs diun-dispatcher`

### "No application found for image"

- Verify image name matches exactly (case-sensitive)
- Check Coolify API token has correct permissions
- Run `docker logs` to see Coolify API response

### Notification not sent

- Verify `APPRISE_URLS` format is correct
- Test Apprise URL: `docker run caronc/apprise apprise -b "test" "your-url"`
- Check logs for Apprise errors

### Cloudflare Access errors

- Verify `CF_ACCESS_CLIENT_ID` and `CF_ACCESS_CLIENT_SECRET` are correct
- Ensure tokens have access to Coolify API endpoint

## License

MIT
