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
 poll status
 until it is back
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

With `AUTO_DEPLOY=true`, a Diun **`update`** event that matches a Coolify service
triggers the redeploy immediately — no click needed. `IGNORE_CONTAINERS` still wins: an
ignored container is never deployed automatically.

### `update` deploys, `new` only informs

A redeploy always pulls the tag the Coolify resource is **configured** with; it never
changes that tag. So the two Diun statuses are handled differently:

| Diun status | Meaning | What the dispatcher does |
|---|---|---|
| `update` | the tag in service was republished (new digest) | redeploy (`AUTO_DEPLOY`), or notify with a deploy link |
| `update`, another tag | a republished series other than the one in service (listed by `watch_repo`) | nothing — resources are matched by repository, this would restart them for nothing |
| `new`, same tag as the resource | Diun recording an image it had not seen (fresh database, new container) | nothing — this used to redeploy every resource at once after a Diun reset |
| `new`, inside the series in service | a `1.27.4` for a resource pinned on `1.27` | nothing — it arrives by itself as an `update` of `1.27` |
| `new`, older tag | an earlier series listed by `watch_repo` | nothing |
| `new`, resource on a moving tag (`latest`, `stable`, `trixie`…) | any release | nothing — it reaches the resource as an `update` of that tag |
| `new`, no Coolify resource runs the repository | Coolify's own database, buildkit… | nothing — it cannot be upgraded from here, and every tag would look new |
| `new`, newer tag | a newer series is out | notify only, **never deploy**: change the tag in Coolify when you are ready |

This gives *patches automatically, majors on request*: pin each resource to a series
tag (`gitea/gitea:1.27`, `postgres:18-alpine`, `lycheeorg/lychee:v6`) rather than
`latest`, and let Diun also list the repository's series tags:

```
DIUN_DEFAULTS_WATCHREPO=true
DIUN_DEFAULTS_INCLUDETAGS=^v?\d+(\.\d+){0,2}$
DIUN_DEFAULTS_SORTTAGS=semver
DIUN_DEFAULTS_MAXTAGS=5
```

A republished `1.27` (a patch release) arrives as `update` and is deployed; a `1.28`
appearing arrives as `new` and is only announced. Including full versions (`x.y.z`)
also announces new releases for resources pinned on an exact version (`n8n:2.40.5` →
`2.40.6`), while releases inside a pinned series (`1.27.4` for `1.27`) stay silent. Tags are compared by their leading
numbers (`v1.27` → 1.27, `11.8-noble` → 11.8); a tag that does not start with a number
(`latest`, `alpine`) cannot be ordered and is always announced.

Your compose files are left untouched: nothing is pinned or rewritten, and the image
pull is requested explicitly for that one deployment. An ordinary restart — from
Coolify's UI, or because the container came back up on its own — still deploys the
same content as before, so nothing updates behind your back.

To tell you when the deployment is **finished** — not merely started — the dispatcher
watches the resource: it notes the service's status before redeploying, then polls
`GET /api/v1/services/{uuid}` — every second for the first minute, where a restart
shows up as `starting`, then every 15 seconds — until the status leaves that baseline
and comes back to it. A service that restarts faster than that may never show a
different status; after 60 seconds at its baseline the dispatcher concludes the
deployment succeeded and says the restart was too quick to observe.

The baseline is also how the dispatcher knows what "back" means. A service that
reported `running:healthy` has a healthcheck, so it must report healthy again; one
that reported `running:unhealthy` has none, and returning to `running` is all that can
be asked of it. Add a healthcheck to a service and the dispatcher automatically becomes
stricter about it — no configuration to change.

Coolify's webhook notifications are **not** used, and there is nothing to configure on
that side. Its documented triggers are a *deployment* completing, or a container that
"stops unexpectedly, restarts automatically, or reaches its automatic restart limit" —
none of which covers the restart the dispatcher requests.

What you get:

| Situation | Notification |
|-----------|--------------|
| The service came back to its baseline status | `✅ <container> — deployed`, with the status it came back with |
| The status never moved (restart finished between two polls) | `✅ <container> — deployed`, 60 s after the redeploy, saying the restart was too quick to observe |
| Coolify refused the redeploy request | `❌ <container> — auto-deploy could not be triggered`, with a manual deploy link |
| The service never came back within 15 minutes | `⏱️ <container> — deployment status unknown`, with the last status seen and a manual deploy link |

A successful auto-deploy sends **one** notification, once it is over.

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
