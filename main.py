import os
import json
import logging
import apprise
import httpx

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="Diun Webhook Dispatcher")


# ---------------------------------------------------------------------------
# Config from environment variables
# ---------------------------------------------------------------------------

def get_env(key: str, required: bool = True) -> str:
    val = os.getenv(key, "").strip()
    if required and not val:
        raise RuntimeError(f"Missing required environment variable: {key}")
    return val


def load_mappings() -> dict:
    """
    MAPPINGS is a JSON env var:
    {
      "my-server": {
        "ntfy": "coolify-uuid-aaa",
        "vaultwarden": "coolify-uuid-bbb"
      },
      "my-other-server": {
        "uptime-kuma": "coolify-uuid-ccc"
      }
    }
    """
    raw = os.getenv("MAPPINGS", "{}")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Invalid MAPPINGS JSON: {e}")


def load_apprise_urls() -> list[str]:
    """
    APPRISE_URLS is a comma-separated list of Apprise URLs.
    e.g. pover://userkey@apptoken,ntfy://ntfy.example.com/topic
    """
    raw = os.getenv("APPRISE_URLS", "")
    return [u.strip() for u in raw.split(",") if u.strip()]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_coolify_uuid(mappings: dict, hostname: str, container: str) -> str | None:
    return mappings.get(hostname, {}).get(container)


async def trigger_coolify(coolify_url: str, coolify_token: str, uuid: str) -> bool:
    url = f"{coolify_url.rstrip('/')}/api/v1/deploy?uuid={uuid}&force=false"
    headers = {"Authorization": f"Bearer {coolify_token}"}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            logger.info(f"Coolify deploy triggered uuid={uuid} status={resp.status_code}")
            return True
    except Exception as e:
        logger.error(f"Coolify deploy failed uuid={uuid}: {e}")
        return False


def send_notification(urls: list[str], title: str, body: str) -> None:
    if not urls:
        logger.warning("No APPRISE_URLS configured, skipping notification")
        return
    apobj = apprise.Apprise()
    for url in urls:
        apobj.add(url)
    if apobj.notify(title=title, body=body):
        logger.info("Notification sent")
    else:
        logger.error("Notification failed")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.post("/webhook")
async def diun_webhook(request: Request):
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    # Optional secret validation
    secret = os.getenv("WEBHOOK_SECRET", "").strip()
    if secret:
        provided = (
            request.headers.get("X-Diun-Secret")
            or request.query_params.get("secret")
        )
        if provided != secret:
            logger.warning("Invalid webhook secret")
            raise HTTPException(status_code=401, detail="Unauthorized")

    meta = data.get("meta", {})
    entry = data.get("entry", {})

    hostname = meta.get("hostname", "unknown")
    status = entry.get("status", "")
    image = entry.get("image", "unknown")
    container_name = (
        entry.get("container", {}).get("name")
        or entry.get("metadata", {}).get("container_name")
        or "unknown"
    )

    logger.info(f"Event: hostname={hostname} container={container_name} image={image} status={status}")

    if status not in ("new", "update"):
        logger.info(f"Ignoring status={status}")
        return JSONResponse({"ok": True, "action": "ignored"})

    mappings = load_mappings()
    apprise_urls = load_apprise_urls()

    coolify_url = os.getenv("COOLIFY_URL", "").strip()
    coolify_token = os.getenv("COOLIFY_TOKEN", "").strip()

    uuid = get_coolify_uuid(mappings, hostname, container_name)
    deployed = False

    if uuid and coolify_url and coolify_token:
        deployed = await trigger_coolify(coolify_url, coolify_token, uuid)
    else:
        logger.warning(f"No Coolify mapping for hostname={hostname} container={container_name}")

    status_emoji = "ðŸ†•" if status == "new" else "â¬†ï¸"
    deploy_line = "âœ… Coolify redeploy triggered" if deployed else "âš ï¸ No Coolify mapping configured"

    title = f"{status_emoji} {container_name} â€” new image available"
    body = (
        f"ðŸ–¥ï¸ Server: {hostname}\n"
        f"ðŸ³ Image: {image}\n"
        f"ðŸ“¦ Container: {container_name}\n"
        f"{deploy_line}"
    )

    send_notification(apprise_urls, title, body)

    return JSONResponse({"ok": True, "deployed": deployed})


@app.get("/health")
async def health():
    return {"status": "ok"}
