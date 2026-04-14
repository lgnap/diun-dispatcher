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


async def get_coolify_applications(coolify_url: str, coolify_token: str) -> list[dict]:
    """Fetch all services/applications from Coolify API"""
    url = f"{coolify_url.rstrip('/')}/api/v1/services"
    headers = {"Authorization": f"Bearer {coolify_token}"}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        logger.error(f"Failed to fetch Coolify services: {e}")
        return []


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

def find_service_uuid_by_image(services: list[dict], image: str) -> str | None:
    """Find service UUID by matching Docker image name within applications/databases"""
    # Extract base image name (e.g., "ghcr.io/music-assistant/server" from "ghcr.io/music-assistant/server:latest")
    image_base = image.split(":")[0] if ":" in image else image

    for service in services:
        # Check applications within the service
        for app in service.get("applications", []):
            app_image = app.get("image", "")
            if not app_image:
                continue

            app_image_base = app_image.split(":")[0] if ":" in app_image else app_image

            if app_image_base == image_base:
                service_uuid = service.get("uuid")
                logger.info(f"Found matching service uuid={service_uuid} for image={image}")
                return service_uuid

        # Also check databases within the service
        for db in service.get("databases", []):
            db_image = db.get("image", "")
            if not db_image:
                continue

            db_image_base = db_image.split(":")[0] if ":" in db_image else db_image

            if db_image_base == image_base:
                service_uuid = service.get("uuid")
                logger.info(f"Found matching service uuid={service_uuid} for image={image}")
                return service_uuid

    logger.warning(f"No application found for image={image}")
    return None


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

    logger.info(f"Received webhook payload: {json.dumps(data, indent=2)}")

    hostname = data.get("hostname", "unknown")
    status = data.get("status", "")
    image = data.get("image", "unknown")
    metadata = data.get("metadata", {})
    container_name = metadata.get("ctn_names", "unknown")

    logger.info(f"Event: hostname={hostname} container={container_name} image={image} status={status}")

    if status not in ("new", "update"):
        logger.info(f"Ignoring status={status}")
        return JSONResponse({"ok": True, "action": "ignored"})

    apprise_urls = load_apprise_urls()

    coolify_url = os.getenv("COOLIFY_URL", "").strip()
    coolify_token = os.getenv("COOLIFY_TOKEN", "").strip()

    uuid = None
    deploy_link = ""
    if coolify_url and coolify_token:
        services = await get_coolify_applications(coolify_url, coolify_token)
        uuid = find_service_uuid_by_image(services, image)
        if uuid:
            dispatcher_url = os.getenv("DISPATCHER_URL", "").strip()
            webhook_secret = os.getenv("WEBHOOK_SECRET", "").strip()
            if dispatcher_url:
                deploy_link = f"\n\n🚀 Déployer: {dispatcher_url}/deploy?uuid={uuid}&secret={webhook_secret}"
    else:
        logger.warning("COOLIFY_URL or COOLIFY_TOKEN not configured")

    status_emoji = "🆕" if status == "new" else "⬆�"
    available_text = "new image available" if uuid else "new image (no deploy available)"

    title = f"{status_emoji} {container_name} — {available_text}"
    body = (
        f"🖥� Server: {hostname}\n"
        f"� Image: {image}\n"
        f"📦 Container: {container_name}"
        f"{deploy_link}"
    )

    send_notification(apprise_urls, title, body)

    return JSONResponse({"ok": True, "uuid": uuid})


@app.get("/deploy")
async def manual_deploy(uuid: str, secret: str = ""):
    """Manually trigger a Coolify deployment"""
    # Validate secret
    expected_secret = os.getenv("WEBHOOK_SECRET", "").strip()
    if expected_secret and secret != expected_secret:
        logger.warning(f"Invalid deploy secret")
        raise HTTPException(status_code=401, detail="Unauthorized")

    coolify_url = os.getenv("COOLIFY_URL", "").strip()
    coolify_token = os.getenv("COOLIFY_TOKEN", "").strip()

    if not coolify_url or not coolify_token:
        raise HTTPException(status_code=500, detail="Coolify not configured")

    deployed = await trigger_coolify(coolify_url, coolify_token, uuid)
    return JSONResponse({"ok": True, "deployed": deployed})


@app.get("/health")
async def health():
    return {"status": "ok"}
