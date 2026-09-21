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
      ↓
POST /coolify-webhook   (deployment finished)
           ↓
        Apprise
```

1. **Diun** detects a new container image and sends a POST webhook
2. **diun-dispatcher** queries Coolify API to find the service using that image
3. **Coolify** redeploys the service with the new image
4. **Apprise** sends a notification with deployment status and manual deploy link

By default the dispatcher only *notifies*, with a one-click deploy link. Set
[`AUTO_DEPLOY=true`](#automatic-deployments) and it redeploys by itself, then tells
you once Coolify reports the deployment as finished.

## Quick start

### Docker

```bash
docker run -d \
  --name diun-dispatcher \
  -p 8000:8000 \
  -e COOLIFY_API_URL="https://coolify.example.com" \
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
| `COOLIFY_API_URL` | Base URL of your Coolify instance (e.g., `https://coolify.example.com`). **Not** `COOLIFY_URL` — Coolify reserves the `COOLIFY_*` namespace and would override it with the app's own FQDN. |
| `COOLIFY_TOKEN` | Coolify API token (generate in Settings → API) |

### Optional environment variables

| Variable              | Default | Description |
|-----------------------|---------|-------------|
| `AUTO_DEPLOY`         | `false` | Redeploy matched images automatically instead of only sending a deploy link (`true`/`1`/`yes`/`on`). See [Automatic deployments](#automatic-deployments) |
| `WEBHOOK_SECRET`      | (none)  | Shared secret for webhook validation (validates `X-Diun-Secret` header, and the `secret` query param of `/coolify-webhook`) |
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
    hostname: production-server      # Fallback server name in notifications
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

- **`hostname`** is a *fallback* server name for notifications. When the image matches a Coolify resource, the dispatcher uses the real server name Coolify reports instead; `hostname` is only shown when there is no match (otherwise Diun defaults it to the container id). Still worth setting a meaningful value (e.g., `production-server`).
- **`X-Diun-Secret`** header must match `WEBHOOK_SECRET` in dispatcher if validation is enabled
- **`DIUN_WATCH_SCHEDULE`** controls how often Diun checks for new images (cron format)

## How image matching works

When Diun sends a webhook with a container image (e.g., `ghcr.io/music-assistant/server:latest`):

1. **dispatcher queries** Coolify API to list all services and databases
2. **Compares** the image name against all deployed containers
3. **Finds matching service** by normalized image name
4. **Triggers redeploy** if found, pulling the image now published under the tag

### Image normalization

- `docker.io/my-app:latest` → `my-app`
- `ghcr.io/user/app:v1.0.0` → `user/app`
- `registry.example.com/app:tag` → `registry.example.com/app`

The dispatcher handles these automatically — no manual mapping needed.

## Automatic deployments

With `AUTO_DEPLOY=true`, a Diun event that matches a Coolify service triggers the
redeploy immediately — no click needed. `IGNORE_CONTAINERS` still wins: an ignored
container is never deployed automatically.

Your compose files are left untouched: nothing is pinned or rewritten, and the image
pull is requested explicitly for that one deployment. An ordinary restart — from
Coolify's UI, or because the container came back up on its own — still deploys the
same content as before, so nothing updates behind your back.

To be told when the deployment is **finished** (and not merely started), let Coolify
call back. In Coolify → **Notifications → Webhook**:

1. Set the **Webhook URL** to `https://dispatcher.example.com/coolify-webhook?secret=<WEBHOOK_SECRET>`
   — Coolify's webhook channel sends no custom headers, so the secret goes in the query string.
2. Under **Notification events**, enable **Deployment success** and **Deployment failure**
   (also enable **Resource status changes** if your resources are Coolify *services*:
   the dispatcher logs every payload it receives, so its logs will tell you which events
   your instance actually emits).
3. Click **Enable**.

What you get:

| Situation | Notification |
|-----------|--------------|
| Deploy triggered, Coolify reports success | `✅ <container> — deployed`, with a link to the deployment logs |
| Deploy triggered, Coolify reports failure | `❌ <container> — deployment failed`, with the logs and a manual deploy link |
| Coolify refused the deploy request | `❌ <container> — auto-deploy could not be triggered`, with a manual deploy link |
| No callback within 15 minutes | `⏱️ <container> — deployment status unknown`, with a manual deploy link |

A successful auto-deploy sends **one** notification, once it is over. Deployments you
start by hand in the Coolify UI are ignored by the callback — only deployments the
dispatcher triggered are reported.

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

Deployments go through Coolify's `POST /api/v1/services/{uuid}/restart?latest=true` —
the API equivalent of *advanced → pull latest images and restart*. `POST /api/v1/deploy`
is **not** used: for compose-based services Coolify reuses the image it already has
locally, so it would redeploy exactly the content Diun just reported as outdated.

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

### POST `/coolify-webhook`

Receives Coolify deployment notifications (see [Automatic deployments](#automatic-deployments)).

**Parameters:**
- `secret` (query string): Must match `WEBHOOK_SECRET` if set

**Body:** Coolify's notification payload (`event`, `deployment_uuid`, `application_uuid`, `deployment_url`, …)

**Response:**
```json
{
  "ok": true,
  "deployed": true
}
```

An event that matches no deployment triggered by the dispatcher returns
`{"ok": true, "action": "ignored"}`.

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
  -e COOLIFY_API_URL="https://coolify.example.com" \
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
