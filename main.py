# -*- coding: utf-8 -*-
import asyncio
import os
import json
import logging
import apprise
import httpx
import time
from pathlib import Path

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="Diun Webhook Dispatcher")

# Templates
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

# Static assets
STATIC_DIR = Path(__file__).parent / "static"

# UUID cache configuration
CACHE_FILE = Path(os.getenv("CACHE_FILE", "/data/uuid_cache.json"))
CACHE_TTL_SECONDS = 7 * 24 * 60 * 60  # 7 days
CACHE_MAX_ENTRIES = 100
SHORT_UUID_LENGTH = 8

# In-memory cache (loaded from disk at startup)
_uuid_cache: dict = {}
_cache_dirty = False

# Recent deployments (in-memory only, no persistence)
_recent_deployments: list = []
MAX_RECENT_DEPLOYMENTS = 5

# Deployments we triggered ourselves and are still waiting on (in-memory only).
# Coolify's webhook notification resolves them; the sweeper gives up after a while.
_pending_deployments: list = []
PENDING_DEPLOYMENT_TIMEOUT_SECONDS = 15 * 60


# ---------------------------------------------------------------------------
# UUID Cache functions (in-memory with lazy disk persistence)
# ---------------------------------------------------------------------------

def _is_entry_expired(entry: dict, now: float = None) -> bool:
    """Check if a cache entry has expired"""
    if now is None:
        now = time.time()
    return (now - entry.get('timestamp', 0)) >= CACHE_TTL_SECONDS


def _clean_expired_entries() -> None:
    """Remove expired entries from in-memory cache"""
    global _uuid_cache
    now = time.time()
    original_size = len(_uuid_cache)
    _uuid_cache = {k: v for k, v in _uuid_cache.items() if not _is_entry_expired(v, now)}
    if len(_uuid_cache) < original_size:
        logger.info(f"Cleaned {original_size - len(_uuid_cache)} expired cache entries")


def _load_cache_from_disk() -> None:
    """Load UUID cache from disk into memory"""
    global _uuid_cache, _cache_dirty
    if not CACHE_FILE.exists():
        _uuid_cache = {}
        return
    try:
        with open(CACHE_FILE, 'r') as f:
            _uuid_cache = json.load(f)
        _clean_expired_entries()
        _cache_dirty = False
        logger.info(f"Loaded cache from disk: {len(_uuid_cache)} entries")
    except (FileNotFoundError, json.JSONDecodeError) as e:
        logger.warning(f"Failed to load cache from disk: {e}")
        _uuid_cache = {}


def _save_cache_to_disk() -> None:
    """Save in-memory cache to disk"""
    global _cache_dirty
    if not _cache_dirty or not _uuid_cache:
        return
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(CACHE_FILE, 'w') as f:
            json.dump(_uuid_cache, f, separators=(',', ':'))
        _cache_dirty = False
        logger.info(f"Saved cache to disk: {len(_uuid_cache)} entries")
    except (IOError, OSError) as e:
        logger.error(f"Failed to save cache to disk: {e}")


def cache_uuid(uuid_short: str, uuid_full: str) -> None:
    """Cache a UUID mapping (in-memory, lazy disk persistence)"""
    global _uuid_cache, _cache_dirty
    now = time.time()

    # If at capacity, remove oldest entry
    if len(_uuid_cache) >= CACHE_MAX_ENTRIES:
        oldest_key = min(_uuid_cache.keys(), key=lambda k: _uuid_cache[k].get('timestamp', 0))
        del _uuid_cache[oldest_key]

    # Add new entry
    _uuid_cache[uuid_short] = {
        'uuid_full': uuid_full,
        'timestamp': now
    }
    _cache_dirty = True
    logger.info(f"Cached UUID: {uuid_short} → {uuid_full}")


def get_uuid_from_cache(uuid_short: str) -> str | None:
    """Retrieve full UUID from cache (O(1) in-memory lookup)"""
    global _uuid_cache, _cache_dirty

    if uuid_short not in _uuid_cache:
        return None

    entry = _uuid_cache[uuid_short]

    if _is_entry_expired(entry):
        # Remove expired entry
        del _uuid_cache[uuid_short]
        _cache_dirty = True
        return None

    return entry.get('uuid_full')


# ---------------------------------------------------------------------------
# Log environment configuration on startup
# ---------------------------------------------------------------------------

def log_environment_config():
    """Log which environment variables are configured (without exposing secrets)"""
    logger.info("=== Environment Configuration ===")

    # Check Coolify config
    coolify_url = os.getenv("COOLIFY_API_URL", "").strip()
    coolify_token = os.getenv("COOLIFY_TOKEN", "").strip()
    logger.info(f"COOLIFY_API_URL: {'✓ configured' if coolify_url else '✗ not configured'}")
    logger.info(f"COOLIFY_TOKEN: {'✓ configured' if coolify_token else '✗ not configured'}")

    # Check Cloudflare Access config
    cf_id = os.getenv("CF_ACCESS_CLIENT_ID", "").strip()
    cf_secret = os.getenv("CF_ACCESS_CLIENT_SECRET", "").strip()
    cf_configured = "✓ configured" if (cf_id and cf_secret) else "✗ not configured"
    logger.info(f"Cloudflare Access headers: {cf_configured}")

    # Check Apprise config
    apprise_urls = os.getenv("APPRISE_URLS", "").strip()
    apprise_count = len([u.strip() for u in apprise_urls.split(",") if u.strip()]) if apprise_urls else 0
    logger.info(f"APPRISE_URLS: {apprise_count} URL(s) configured")

    # Check webhook secret
    secret = os.getenv("WEBHOOK_SECRET", "").strip()
    logger.info(f"WEBHOOK_SECRET: {'✓ configured' if secret else '✗ not configured'}")

    # Check auto-deploy
    logger.info(f"AUTO_DEPLOY: {'✓ enabled' if is_auto_deploy_enabled() else '✗ disabled (manual link only)'}")

    # Check dispatcher URL
    dispatcher_url = os.getenv("DISPATCHER_URL", "").strip()
    logger.info(f"DISPATCHER_URL: {'✓ configured' if dispatcher_url else '✗ not configured'}")

    # Check ignore list
    ignore_containers_raw = os.getenv("IGNORE_CONTAINERS", "").strip()
    ignore_count = len([c.strip() for c in ignore_containers_raw.split(",") if c.strip()]) if ignore_containers_raw else 0
    logger.info(f"IGNORE_CONTAINERS: {ignore_count} container(s) to ignore")

    logger.info("=== End Configuration ===\n")


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
    cf_headers = get_cloudflare_headers()
    headers = {
        "Authorization": f"Bearer {coolify_token}",
        **cf_headers
    }

    # Log request details
    header_names = list(headers.keys())
    cf_enabled = "CF-Access-Client-Id" in headers
    logger.info(f"GET {url} | Headers: {header_names} | Cloudflare Access: {'enabled' if cf_enabled else 'disabled'}")

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            logger.info(f"✓ Coolify services fetched: {len(data)} service(s)")
            return data
    except Exception as e:
        logger.error(f"✗ Failed to fetch Coolify services: {e}")
        return []


def load_apprise_urls() -> list[str]:
    """
    APPRISE_URLS is a comma-separated list of Apprise URLs.
    e.g. pover://userkey@apptoken,ntfy://ntfy.example.com/topic
    """
    raw = os.getenv("APPRISE_URLS", "")
    return [u.strip() for u in raw.split(",") if u.strip()]


AUTO_DEPLOY_TRUTHY = ("true", "1", "yes", "on")


def is_auto_deploy_enabled() -> bool:
    """AUTO_DEPLOY makes the dispatcher redeploy matched images by itself."""
    return os.getenv("AUTO_DEPLOY", "").strip().lower() in AUTO_DEPLOY_TRUTHY


def get_cloudflare_headers() -> dict:
    """
    Returns a dict with Cloudflare Access headers if both credentials are configured.
    If either is missing or empty, returns an empty dict (headers are optional).
    """
    cf_id = os.getenv("CF_ACCESS_CLIENT_ID", "").strip()
    cf_secret = os.getenv("CF_ACCESS_CLIENT_SECRET", "").strip()

    if cf_id and cf_secret:
        return {
            "CF-Access-Client-Id": cf_id,
            "CF-Access-Client-Secret": cf_secret
        }
    return {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

DOCKER_HUB_REGISTRIES = ("docker.io", "index.docker.io", "registry-1.docker.io")
DOCKER_HUB_IMPLICIT_NAMESPACE = "library/"


def normalize_image(image: str) -> str:
    """Normalize an image reference to a canonical repository name.

    Diun sends fully normalized references (docker.io/library/postgres:17-alpine)
    while Coolify stores them compose-style (postgres:17-alpine). Both forms must
    collapse to the same value so they can be compared.
    """
    ref = image.strip()

    # Drop a digest, if any (redis@sha256:...)
    ref = ref.split("@", 1)[0]

    # Split off the registry: the first component is a registry only when it
    # looks like a host, otherwise it is part of the repository path.
    head, separator, remainder = ref.partition("/")
    if separator and ("." in head or ":" in head or head == "localhost"):
        registry, repository = head, remainder
    else:
        registry, repository = "", ref

    # Drop the tag from the repository only — the registry may carry a port
    if ":" in repository:
        repository = repository.rsplit(":", 1)[0]

    # docker.io is implicit, and so is its "library/" namespace
    if registry in DOCKER_HUB_REGISTRIES:
        registry = ""
    if not registry and repository.startswith(DOCKER_HUB_IMPLICIT_NAMESPACE):
        repository = repository[len(DOCKER_HUB_IMPLICIT_NAMESPACE):]

    return f"{registry}/{repository}" if registry else repository


def find_service_by_image(services: list[dict], image: str) -> dict | None:
    """Find the Coolify service whose application/database matches the image.

    Returns the full service dict so callers can read both its uuid and its
    server name (Coolify knows which server each resource runs on).
    """
    # Normalize the incoming image
    image_normalized = normalize_image(image)

    for service in services:
        # Check applications within the service
        for app in service.get("applications", []):
            app_image = app.get("image", "")
            if not app_image:
                continue

            if normalize_image(app_image) == image_normalized:
                logger.info(f"Found matching service uuid={service.get('uuid')} for image={image}")
                return service

        # Also check databases within the service
        for db in service.get("databases", []):
            db_image = db.get("image", "")
            if not db_image:
                continue

            if normalize_image(db_image) == image_normalized:
                logger.info(f"Found matching service uuid={service.get('uuid')} for image={image}")
                return service

    logger.warning(f"No application found for image={image}")
    return None


def find_service_uuid_by_image(services: list[dict], image: str) -> str | None:
    """Find service UUID by matching Docker image name within applications/databases"""
    service = find_service_by_image(services, image)
    return service.get("uuid") if service else None


def _extract_deployment_uuid(resp) -> str | None:
    """Read the queued deployment id out of Coolify's deploy response.

    Coolify answers {"deployments": [{"resource_uuid": ..., "deployment_uuid": ...}]},
    but a deploy that succeeds with an unexpected body must not count as a failure.
    """
    try:
        payload = resp.json()
    except Exception:
        return None
    if isinstance(payload, dict):
        deployments = payload.get("deployments") or []
        if deployments and isinstance(deployments[0], dict):
            return deployments[0].get("deployment_uuid")
        return payload.get("deployment_uuid")
    return None


async def trigger_coolify(coolify_url: str, coolify_token: str, uuid: str) -> dict:
    url = f"{coolify_url.rstrip('/')}/api/v1/deploy?uuid={uuid}&force=false"
    cf_headers = get_cloudflare_headers()
    headers = {
        "Authorization": f"Bearer {coolify_token}",
        **cf_headers
    }

    # Log request details
    header_names = list(headers.keys())
    cf_enabled = "CF-Access-Client-Id" in headers
    logger.info(f"POST {url} | Headers: {header_names} | Cloudflare Access: {'enabled' if cf_enabled else 'disabled'} | UUID: {uuid}")

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            # Coolify's /api/v1/deploy is POST-only; a GET returns 405.
            resp = await client.post(url, headers=headers)
            resp.raise_for_status()
            deployment_uuid = _extract_deployment_uuid(resp)
            logger.info(
                f"✓ Coolify deploy triggered: uuid={uuid} status={resp.status_code} "
                f"deployment_uuid={deployment_uuid}"
            )
            return {"ok": True, "deployment_uuid": deployment_uuid}
    except Exception as e:
        logger.error(f"✗ Coolify deploy failed: uuid={uuid} error={e}")
        return {"ok": False, "deployment_uuid": None}


def build_deploy_link(uuid: str) -> str:
    """Build the manual deploy link shown in notifications (empty if not configurable)."""
    dispatcher_url = os.getenv("DISPATCHER_URL", "").strip()
    if not dispatcher_url:
        logger.warning("DISPATCHER_URL not configured, no deploy link generated")
        return ""

    webhook_secret = os.getenv("WEBHOOK_SECRET", "").strip()
    uuid_short = uuid[:SHORT_UUID_LENGTH]
    cache_uuid(uuid_short, uuid)
    secret_param = f"&secret={webhook_secret}" if webhook_secret else ""
    link = f"\n\n🚀 Déployer [{uuid_short}]: {dispatcher_url}/deploy?uuid={uuid_short}{secret_param}"
    logger.info(f"Generated deploy link: {link}")
    return link


def build_notification_body(server: str, image: str, container_name: str, extra: str = "") -> str:
    """The Server/Image/Container block shared by every notification."""
    return (
        f"🖥️ Server: {server}\n"
        f"🖼️ Image: {image}\n"
        f"📦 Container: {container_name}"
        f"{extra}"
    )


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


def find_deployment_by_uuid(services: list[dict], uuid: str) -> dict | None:
    """Find deployment details by service UUID"""
    for service in services:
        if service.get("uuid") == uuid:
            hostname = service.get("server", {}).get("name", "unknown")

            # Try to find any app or database in the service
            for app in service.get("applications", []):
                return {
                    "container_name": app.get("name", "unknown"),
                    "image": app.get("image", "unknown"),
                    "hostname": hostname,
                    "uuid": uuid,
                    "type": "application"
                }

            for db in service.get("databases", []):
                return {
                    "container_name": db.get("name", "unknown"),
                    "image": db.get("image", "unknown"),
                    "hostname": hostname,
                    "uuid": uuid,
                    "type": "database"
                }

    return None


def extract_deployments_from_services(services: list[dict]) -> list[dict]:
    """Extract deployable applications from Coolify services"""
    deployments = []

    for service in services:
        hostname = service.get("server", {}).get("name", "unknown")
        service_uuid = service.get("uuid", "")

        # Extract applications
        for app in service.get("applications", []):
            deployment = {
                "container_name": app.get("name", "unknown"),
                "image": app.get("image", "unknown"),
                "hostname": hostname,
                "uuid": service_uuid,
                "type": "application"
            }
            deployments.append(deployment)

        # Extract databases
        for db in service.get("databases", []):
            deployment = {
                "container_name": db.get("name", "unknown"),
                "image": db.get("image", "unknown"),
                "hostname": hostname,
                "uuid": service_uuid,
                "type": "database"
            }
            deployments.append(deployment)

    return deployments


# ---------------------------------------------------------------------------
# Startup/Shutdown events
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def startup_event():
    """Initialize cache and log configuration on application startup"""
    _load_cache_from_disk()
    log_environment_config()
    asyncio.create_task(_pending_deployment_sweeper())


@app.on_event("shutdown")
async def shutdown_event():
    """Save cache to disk on application shutdown"""
    _save_cache_to_disk()


def log_recent_deployment(container_name: str, image: str, hostname: str) -> None:
    """Log a deployment to recent deployments history (in-memory only)"""
    global _recent_deployments
    from datetime import datetime
    deployment = {
        "container_name": container_name,
        "image": image,
        "hostname": hostname,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }
    _recent_deployments.insert(0, deployment)
    # Keep only recent deployments
    _recent_deployments[:] = _recent_deployments[:MAX_RECENT_DEPLOYMENTS]


# ---------------------------------------------------------------------------
# Pending deployments (auto-deploy → Coolify callback)
# ---------------------------------------------------------------------------

def register_pending_deployment(deployment_uuid: str | None, service_uuid: str,
                                container_name: str, image: str, server: str) -> None:
    """Remember a deployment we triggered, so we can report its outcome later."""
    _pending_deployments.append({
        "deployment_uuid": deployment_uuid,
        "service_uuid": service_uuid,
        "container_name": container_name,
        "image": image,
        "server": server,
        "timestamp": time.time(),
    })
    logger.info(
        f"Waiting for deployment {deployment_uuid or '(no id)'} of {container_name} "
        f"(service {service_uuid})"
    )


def pop_pending_deployment(deployment_uuid: str | None = None,
                           service_uuid: str | None = None) -> dict | None:
    """Take the pending deployment matching either id, preferring the deployment id.

    Coolify reports application deployments with a deployment_uuid; service
    deployments may only carry the resource uuid, hence the second key.
    """
    for key, value in (("deployment_uuid", deployment_uuid), ("service_uuid", service_uuid)):
        if not value:
            continue
        for entry in _pending_deployments:
            if entry.get(key) == value:
                _pending_deployments.remove(entry)
                return entry
    return None


def notify_expired_deployments(now: float = None) -> int:
    """Report deployments Coolify never reported back on. Returns how many."""
    if now is None:
        now = time.time()

    expired = [e for e in _pending_deployments
               if now - e["timestamp"] >= PENDING_DEPLOYMENT_TIMEOUT_SECONDS]
    for entry in expired:
        _pending_deployments.remove(entry)
        container_name = entry["container_name"]
        logger.warning(f"No Coolify callback for {container_name} after the timeout")
        send_notification(
            load_apprise_urls(),
            f"⏱️ {container_name} — deployment status unknown",
            build_notification_body(
                entry["server"], entry["image"], container_name,
                "\n\n⚠️ Deploy triggered but Coolify never reported back."
                + build_deploy_link(entry["service_uuid"]),
            ),
        )
    return len(expired)


async def _pending_deployment_sweeper() -> None:
    """Check once a minute whether a pending deployment has gone quiet for too long."""
    while True:
        await asyncio.sleep(60)
        try:
            notify_expired_deployments()
        except Exception as e:  # a sweeper crash must not take the loop down
            logger.error(f"Pending deployment sweeper failed: {e}")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

# Diun sends webhooks with GET by default (JSON in the body); accept both.
@app.api_route("/webhook", methods=["GET", "POST"])
async def diun_webhook(request: Request):
    # Debug: log request details
    content_type = request.headers.get('Content-Type', 'not set')
    content_length = request.headers.get('Content-Length', 'not set')
    logger.info(f"Webhook received | Content-Type: {content_type} | Content-Length: {content_length}")

    # Try to get raw body first
    try:
        raw_body = await request.body()
        logger.info(f"Raw body (first 300 chars): {raw_body[:300]}")

        # Parse JSON manually from raw body
        data = json.loads(raw_body)
        logger.info(f"✓ Parsed JSON successfully")
    except json.JSONDecodeError as e:
        logger.error(f"✗ Failed to parse JSON from body: {e}")
        raise HTTPException(status_code=400, detail="Invalid JSON")
    except Exception as e:
        logger.error(f"✗ Error reading body: {e}")
        raise HTTPException(status_code=400, detail="Error reading request")

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

    # Check if container is in ignore list
    ignore_containers_raw = os.getenv("IGNORE_CONTAINERS", "").strip()
    ignore_containers = [c.strip() for c in ignore_containers_raw.split(",") if c.strip()]
    if container_name in ignore_containers:
        logger.info(f"Container {container_name} is in ignore list, skipping notification")
        return JSONResponse({"ok": True, "action": "ignored"})

    apprise_urls = load_apprise_urls()

    coolify_url = os.getenv("COOLIFY_API_URL", "").strip()
    coolify_token = os.getenv("COOLIFY_TOKEN", "").strip()

    uuid = None
    matched_service = None
    deploy_link = ""
    if coolify_url and coolify_token:
        services = await get_coolify_applications(coolify_url, coolify_token)
        matched_service = find_service_by_image(services, image)
        uuid = matched_service.get("uuid") if matched_service else None
        if uuid:
            deploy_link = build_deploy_link(uuid)
    else:
        logger.warning("COOLIFY_API_URL or COOLIFY_TOKEN not configured")

    status_emoji = "🆕" if status == "new" else "⬆️"
    available_text = "new image available" if uuid else "new image (no deploy available)"

    # Prefer the real server name Coolify knows for the matched resource;
    # fall back to the hostname Diun reported (often just the container id).
    server_display = hostname.split('.')[0] if hostname != "unknown" else hostname
    if matched_service:
        coolify_server = matched_service.get("server", {}).get("name")
        if coolify_server:
            server_display = coolify_server

    # AUTO_DEPLOY: redeploy right away and stay silent until Coolify reports back.
    if uuid and is_auto_deploy_enabled():
        result = await trigger_coolify(coolify_url, coolify_token, uuid)
        if result["ok"]:
            register_pending_deployment(
                deployment_uuid=result["deployment_uuid"],
                service_uuid=uuid,
                container_name=container_name,
                image=image,
                server=server_display,
            )
            logger.info(f"Auto-deploy triggered for {container_name} (service {uuid})")
            return JSONResponse({"ok": True, "uuid": uuid, "action": "auto-deploy"})

        send_notification(
            apprise_urls,
            f"❌ {container_name} — auto-deploy could not be triggered",
            build_notification_body(server_display, image, container_name, deploy_link),
        )
        return JSONResponse({"ok": True, "uuid": uuid, "action": "auto-deploy-failed"})

    title = f"{status_emoji} {container_name} — {available_text}"
    body = build_notification_body(server_display, image, container_name, deploy_link)

    send_notification(apprise_urls, title, body)

    return JSONResponse({"ok": True, "uuid": uuid})


@app.get("/deploy")
async def manual_deploy(request: Request, uuid: str, secret: str = ""):
    """Manually trigger a Coolify deployment and show confirmation page"""
    # Validate secret
    expected_secret = os.getenv("WEBHOOK_SECRET", "").strip()
    if expected_secret and secret != expected_secret:
        logger.warning(f"Invalid deploy secret")
        raise HTTPException(status_code=401, detail="Unauthorized")

    # Resolve UUID (short → full if cached)
    resolved_uuid = uuid
    if len(uuid) <= SHORT_UUID_LENGTH:
        full_uuid = get_uuid_from_cache(uuid)
        if full_uuid:
            logger.info(f"Resolved short UUID {uuid} → {full_uuid}")
            resolved_uuid = full_uuid
        else:
            logger.warning(f"Short UUID {uuid} not found in cache, may be expired")
            raise HTTPException(status_code=404, detail="UUID not found in cache (may be expired)")

    coolify_url = os.getenv("COOLIFY_API_URL", "").strip()
    coolify_token = os.getenv("COOLIFY_TOKEN", "").strip()

    if not coolify_url or not coolify_token:
        raise HTTPException(status_code=500, detail="Coolify not configured")

    # Enrich logs with deployment details
    deployment_info = "unknown service"
    container_name = "unknown"
    image = "unknown"
    hostname = "unknown"
    deployed = False

    services = await get_coolify_applications(coolify_url, coolify_token)
    deployment = find_deployment_by_uuid(services, resolved_uuid)
    if deployment:
        container_name = deployment['container_name']
        image = deployment['image']
        hostname = deployment['hostname']
        deployment_info = f"{container_name} ({deployment['type']}) @ {hostname}"
        logger.info(f"🚀 Deploying: {deployment_info} | Image: {image}")

        # Trigger deployment
        deployed = (await trigger_coolify(coolify_url, coolify_token, resolved_uuid))["ok"]

        if deployed:
            logger.info(f"✓ Deployment triggered successfully: {deployment_info}")
            log_recent_deployment(container_name, image, hostname)
        else:
            logger.warning(f"✗ Deployment failed: {deployment_info}")

    return templates.TemplateResponse("deploy_confirmation.html", {
        "request": request,
        "deployed": deployed,
        "container_name": container_name,
        "image": image,
        "hostname": hostname,
        "uuid": resolved_uuid,
        "recent_deployments": _recent_deployments
    })


@app.get("/api/deployments")
async def get_deployments_api(secret: str = "", status: str = None, container: str = None, hostname: str = None):
    """Get all deployable applications/databases from Coolify services (requires secret)"""
    # Authenticate
    expected_secret = os.getenv("WEBHOOK_SECRET", "").strip()
    if not expected_secret or secret != expected_secret:
        logger.warning("Unauthorized API access to /api/deployments")
        raise HTTPException(status_code=401, detail="Unauthorized")

    coolify_url = os.getenv("COOLIFY_API_URL", "").strip()
    coolify_token = os.getenv("COOLIFY_TOKEN", "").strip()

    if not coolify_url or not coolify_token:
        logger.warning("Coolify not configured, returning empty deployments")
        return JSONResponse({"deployments": []})

    services = await get_coolify_applications(coolify_url, coolify_token)
    deployments = extract_deployments_from_services(services)

    # Apply filters
    if container:
        deployments = [d for d in deployments if container.lower() in d.get("container_name", "").lower()]
    if hostname:
        deployments = [d for d in deployments if d.get("hostname") == hostname]

    return JSONResponse({"deployments": deployments})


@app.post("/coolify-webhook")
async def coolify_webhook(request: Request, secret: str = ""):
    """Receive Coolify deployment notifications and report the outcome.

    Configure it in Coolify → Notifications → Webhook with the secret in the
    query string: Coolify's webhook channel offers no custom headers.
    """
    expected_secret = os.getenv("WEBHOOK_SECRET", "").strip()
    if expected_secret and secret != expected_secret:
        logger.warning("Invalid Coolify webhook secret")
        raise HTTPException(status_code=401, detail="Unauthorized")

    try:
        data = json.loads(await request.body())
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        logger.error(f"✗ Failed to parse Coolify webhook body: {e}")
        raise HTTPException(status_code=400, detail="Invalid JSON")

    # Logged in full: this is how we learn what Coolify sends for service deployments.
    logger.info(f"Coolify webhook payload: {json.dumps(data, indent=2)}")

    event = str(data.get("event", ""))
    deployment_uuid = data.get("deployment_uuid")
    resource_uuid = data.get("application_uuid") or data.get("resource_uuid")

    pending = pop_pending_deployment(deployment_uuid, resource_uuid)
    if not pending:
        logger.info(f"Ignoring Coolify event={event}: no deployment of ours is waiting on it")
        return JSONResponse({"ok": True, "action": "ignored"})

    success = data.get("success")
    if not isinstance(success, bool):
        success = "success" in event

    container_name = pending["container_name"]
    image = pending["image"]

    if success:
        title = f"✅ {container_name} — deployed"
        extra = ""
        log_recent_deployment(container_name, image, pending["server"])
    else:
        title = f"❌ {container_name} — deployment failed"
        extra = build_deploy_link(pending["service_uuid"])

    deployment_url = data.get("deployment_url")
    if deployment_url:
        extra = f"\n\n🔎 Logs: {deployment_url}{extra}"

    send_notification(
        load_apprise_urls(),
        title,
        build_notification_body(pending["server"], image, container_name, extra),
    )
    return JSONResponse({"ok": True, "deployed": success})


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return FileResponse(STATIC_DIR / "favicon.svg", media_type="image/svg+xml")
