# -*- coding: utf-8 -*-
import asyncio
import base64
import os
import json
import logging
import re
import apprise
import httpx
import time
import yaml
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


class SecretQueryFilter(logging.Filter):
    """Mask the secret query parameter of /deploy and /upgrade in uvicorn's access log."""

    PATTERN = re.compile(r"([?&]secret=)[^&\s]*")

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(self.PATTERN.sub(r"\1***", a) if isinstance(a, str) else a
                                for a in record.args)
        return True


logging.getLogger("uvicorn.access").addFilter(SecretQueryFilter())

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

# Watching a redeploy we triggered: how often to ask Coolify for the resource
# status, how long to wait for it to come back, and how long to wait for the
# status to move at all before concluding the restart happened between two polls.
# Measured on a real service: restarted 7 s after the trigger, "starting" for
# ~6 s, then healthy. Polling every second during the grace window gives several
# samples of that transition; afterwards we only wait for a slow service, and
# hammering Coolify (which asks Docker on the server) every second would not help.
WATCH_FAST_INTERVAL_SECONDS = 1
WATCH_SLOW_INTERVAL_SECONDS = 15
WATCH_TIMEOUT_SECONDS = 15 * 60
WATCH_TRANSITION_GRACE_SECONDS = 60


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
    logger.info(f"IGNORE_CONTAINERS: {len(ignored_containers())} container(s) to ignore")

    # Check the series follow-up (diun-dispatcher.follow labels)
    write_token = os.getenv("COOLIFY_WRITE_TOKEN", "").strip()
    logger.info(f"COOLIFY_WRITE_TOKEN: {'✓ configured' if write_token else '✗ not configured (COOLIFY_TOKEN used)'}")
    logger.info(f"SERIES_CHECK_HOUR: series check every day at {series_check_hour()}:00")

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


def image_tag(image: str) -> str:
    """The tag of an image reference ("latest" when none is given)."""
    ref = image.strip().split("@", 1)[0]
    last = ref.rsplit("/", 1)[-1]
    return last.rsplit(":", 1)[1] if ":" in last else "latest"


def version_key(tag: str) -> tuple[int, ...] | None:
    """The leading version numbers of a tag, for ordering: "v1.27" -> (1, 27),
    "11.8-noble" -> (11, 8). None when the tag does not start with a number
    ("latest", "alpine"), so it cannot be ordered."""
    match = re.match(r"v?(\d+(?:\.\d+)*)", tag.strip())
    return tuple(int(part) for part in match.group(1).split(".")) if match else None


def find_configured_image(service: dict, image: str) -> str | None:
    """The image reference (with its tag) the matched Coolify resource runs."""
    image_normalized = normalize_image(image)
    for resource in service.get("applications", []) + service.get("databases", []):
        configured = resource.get("image", "")
        if configured and normalize_image(configured) == image_normalized:
            return configured
    return None


def classify_new_tag(configured_image: str | None, image: str) -> str:
    """What a Diun "new" event means for the resource that runs this repository.

    Diun sends "new" both when it discovers a tag it had never seen (watch_repo)
    and when it first records the image already in service (a fresh database, a
    new container). Neither must redeploy: redeploying pulls the tag the resource
    is configured with, so a new tag can only be adopted by changing it in
    Coolify. Returns:
      "in-service" -- the tag the resource already runs: nothing to report
      "in-series"  -- a release inside the series in service (1.27.4 for a
                      resource on 1.27): it arrives by itself as an "update"
      "older"      -- a tag of an earlier series than the one in service
      "rolling"    -- the resource follows a moving tag (latest, stable,
                      trixie...): every release reaches it as an "update"
      "unmanaged"  -- no Coolify resource runs this repository (Coolify's own
                      database, buildkit...): nothing can be done from here,
                      and without a reference every tag would look new
      "newer"      -- a tag worth telling the user about
    """
    if configured_image is None:
        return "unmanaged"
    new_tag, current_tag = image_tag(image), image_tag(configured_image)
    if new_tag == current_tag:
        return "in-service"
    new_key, current_key = version_key(new_tag), version_key(current_tag)
    if current_key is None:
        return "rolling"
    if new_key is not None:
        if len(new_key) > len(current_key) and new_key[:len(current_key)] == current_key:
            return "in-series"
        if new_key <= current_key:
            return "older"
    return "newer"


def find_service_uuid_by_image(services: list[dict], image: str) -> str | None:
    """Find service UUID by matching Docker image name within applications/databases"""
    service = find_service_by_image(services, image)
    return service.get("uuid") if service else None


def _extract_deployment_uuid(resp) -> str | None:
    """Read the queued deployment id out of Coolify's response, if it carries one.

    The restart endpoint answers with a bare message, so this usually yields None.
    It is kept for the Coolify versions that do answer with a deployment id, which
    the logs then carry.
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
    """Redeploy a Coolify service, pulling the image published under its tag.

    Not /api/v1/deploy: for compose-based services Coolify reuses the image it
    already has locally, so a deploy would redeploy the very content Diun just
    told us is outdated. The restart endpoint's latest=true is what the UI calls
    "pull latest images and restart" — it leaves the compose file untouched, so
    an ordinary restart still deploys the same content as before.
    """
    url = f"{coolify_url.rstrip('/')}/api/v1/services/{uuid}/restart?latest=true"
    cf_headers = get_cloudflare_headers()
    headers = {
        "Authorization": f"Bearer {coolify_token}",
        **cf_headers
    }

    # Log request details
    header_names = list(headers.keys())
    cf_enabled = "CF-Access-Client-Id" in headers
    logger.info(f"POST {url} | Headers: {header_names} | Cloudflare Access: {'enabled' if cf_enabled else 'disabled'} | UUID: {uuid} | pull latest images: yes")

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            # Coolify's deploy/restart endpoints are POST-only; a GET returns 405.
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
    link = f"\n\n🚀 Deploy [{uuid_short}]: {dispatcher_url}/deploy?uuid={uuid_short}{secret_param}"
    logger.info(f"Generated deploy link for {uuid_short}")
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
    global _series_task
    _load_cache_from_disk()
    log_environment_config()
    _series_task = asyncio.create_task(series_check_loop())


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
# Watching a deployment through Coolify's resource status
# ---------------------------------------------------------------------------

def _split_status(status: str) -> tuple[str, str]:
    """Split Coolify's "running:healthy" into its state and its health."""
    state, _, health = str(status).partition(":")
    return state.strip().lower(), health.strip().lower()


def status_is_at_least(current: str, baseline: str) -> bool:
    """Is the resource back to the state it was in before we redeployed it?

    The baseline tells us whether the resource reports health at all: one that
    was running:healthy has a healthcheck and must be healthy again, while one
    that was running:unhealthy has none and would never qualify otherwise.
    """
    current_state, current_health = _split_status(current)
    _, baseline_health = _split_status(baseline)

    if current_state != "running":
        return False
    if baseline_health == "healthy":
        return current_health == "healthy"
    return True


async def get_service_status(coolify_url: str, coolify_token: str, uuid: str) -> str | None:
    """Read a service's current status ("running:healthy"), or None if unreachable."""
    url = f"{coolify_url.rstrip('/')}/api/v1/services/{uuid}"
    headers = {"Authorization": f"Bearer {coolify_token}", **get_cloudflare_headers()}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            return resp.json().get("status")
    except Exception as e:
        logger.warning(f"Could not read status of service {uuid}: {e}")
        return None


def watch_interval(elapsed: float, grace: float = WATCH_TRANSITION_GRACE_SECONDS) -> float:
    """Poll fast while the restart can still be observed, slowly afterwards."""
    return WATCH_FAST_INTERVAL_SECONDS if elapsed < grace else WATCH_SLOW_INTERVAL_SECONDS


async def wait_for_service(coolify_url: str, coolify_token: str, uuid: str,
                           baseline_status: str, container_name: str,
                           timeout: float = WATCH_TIMEOUT_SECONDS,
                           grace: float = WATCH_TRANSITION_GRACE_SECONDS) -> tuple[bool, str | None, str]:
    """Follow a redeploy until the resource is back to its baseline status.

    Returns (back, last status, note): back is False when the resource did not
    come back before the timeout; the note says when the restart was too quick
    to be observed.
    """
    deadline = time.time() + timeout
    started = time.time()
    transition_seen = False
    status = None
    last_logged = object()  # anything the first status cannot equal

    while True:
        status = await get_service_status(coolify_url, coolify_token, uuid)
        if status != last_logged:
            logger.info(f"Watching {container_name} (service {uuid}): status={status}")
            last_logged = status

        if status is not None and status_is_at_least(status, baseline_status):
            if transition_seen:
                return True, status, ""
            if time.time() - started >= grace:
                # Coolify refreshes statuses on its own schedule; a restart that
                # finished between two polls is invisible to us. That is the
                # normal case for a service that comes back in a few seconds.
                return True, status, f" {int(grace)} s after the redeploy (restart too quick to observe)"
        elif status is not None:
            transition_seen = True

        if time.time() >= deadline:
            logger.warning(f"Gave up watching {container_name}: last status={status}")
            return False, status, ""

        await asyncio.sleep(watch_interval(time.time() - started, grace))


async def watch_deployment(coolify_url: str, coolify_token: str, uuid: str,
                           baseline_status: str, container_name: str, image: str,
                           server: str, timeout: float = WATCH_TIMEOUT_SECONDS,
                           grace: float = WATCH_TRANSITION_GRACE_SECONDS) -> None:
    """Follow a redeploy until the resource is back, then notify.

    Coolify notifies webhooks for deployments, not for the restart we trigger, so
    the outcome is read from the resource status instead. Success means the status
    left its baseline and came back to it — with the health the baseline had.
    """
    back, status, note = await wait_for_service(
        coolify_url, coolify_token, uuid, baseline_status, container_name,
        timeout=timeout, grace=grace)
    if back:
        _notify_deployment_done(container_name, image, server, status, note)
        return
    send_notification(
        load_apprise_urls(),
        f"⏱️ {container_name} — deployment status unknown",
        build_notification_body(
            server, image, container_name,
            f"\n\n⚠️ Redeploy triggered, but the service never came back."
            f"\n📊 Last status: {status}" + build_deploy_link(uuid),
        ),
    )


def _notify_deployment_done(container_name: str, image: str, server: str,
                            status: str, extra: str) -> None:
    send_notification(
        load_apprise_urls(),
        f"✅ {container_name} — deployed",
        build_notification_body(server, image, container_name,
                                f"\n📊 Status: {status}{extra}"),
    )


# ---------------------------------------------------------------------------
# Virtual series: follow the patches of an image that publishes no series tag
# ---------------------------------------------------------------------------
#
# A resource pinned on a series tag (gitea/gitea:1.27) gets its patches by
# itself: the publisher republishes 1.27 and Diun sends an "update". Some
# publishers have no series tag at all (lychee v6.10.1, mealie v3.21.0, n8n
# 2.40.5): pinned on an exact version, they would never receive anything. A
# label on the compose service lets the dispatcher move the tag itself, within
# a limit:
#   diun-dispatcher.follow=patch  same major and minor (v6.10.1 -> v6.10.4)
#   diun-dispatcher.follow=minor  same major           (v6.10.1 -> v6.11.0)
# Never a major change, whatever the policy.

SERIES_LABEL = "diun-dispatcher.follow"
# How many leading version numbers each policy keeps fixed
SERIES_POLICIES = {"patch": 2, "minor": 1}
SERIES_DEFAULT_CHECK_HOUR = 5
SERIES_MIN_INTERVAL_SECONDS = 24 * 60 * 60
REGISTRY_MAX_PAGES = 50

# In-memory only: a restart forgets them, at worst one more proposal or attempt.
_series_last_attempt: dict[str, float] = {}
_series_proposed: dict[str, str] = {}
_series_locks: dict[str, asyncio.Lock] = {}
_series_task: asyncio.Task | None = None
_background_tasks: set[asyncio.Task] = set()

TAG_PATTERN = re.compile(r"(v?)(\d+(?:\.\d+)*)(-[A-Za-z0-9.-]+)?")


def parse_tag(tag: str) -> tuple[str, tuple[int, ...], str] | None:
    """Split a version tag into its form: "v6.10.1" -> ("v", (6, 10, 1), ""),
    "11.8-noble" -> ("", (11, 8), "-noble"). None for a tag that is not a version."""
    match = TAG_PATTERN.fullmatch(tag.strip())
    if not match:
        return None
    prefix, numbers, suffix = match.groups()
    return prefix, tuple(int(part) for part in numbers.split(".")), suffix or ""


def pick_series_target(current_tag: str, tags: list[str], policy: str) -> str | None:
    """The highest published tag the policy allows moving to, if newer than the
    one in service. Only tags of the very same form qualify: same "v" prefix, as
    many numbers, same suffix -- which also leaves out pre-releases (-rc1,
    -beta, -legacy) for a resource running a plain version."""
    fixed = SERIES_POLICIES.get(policy)
    current = parse_tag(current_tag)
    if fixed is None or current is None:
        return None
    prefix, numbers, suffix = current
    best, best_numbers = None, numbers
    for tag in tags:
        parsed = parse_tag(tag)
        if parsed is None:
            continue
        tag_prefix, tag_numbers, tag_suffix = parsed
        if (tag_prefix, len(tag_numbers), tag_suffix) != (prefix, len(numbers), suffix):
            continue
        if tag_numbers[:fixed] != numbers[:fixed]:
            continue
        if tag_numbers > best_numbers:
            best, best_numbers = tag, tag_numbers
    return best


def _load_compose(raw: str | None) -> dict:
    """Parse a compose file with every scalar kept as a string.

    BaseLoader resolves no types, so 'yes' and yes, or '8000' and 8000, compare
    equal: Coolify re-dumps the compose on save and adds quotes (around image:,
    among others) that must not count as a change.
    """
    try:
        doc = yaml.load(raw or "", Loader=yaml.BaseLoader)
    except yaml.YAMLError as e:
        logger.warning(f"Could not parse a compose file: {type(e).__name__}")
        return {}
    return doc if isinstance(doc, dict) else {}


def series_policies(raw: str | None) -> dict[str, dict]:
    """The compose services that carry a series label: {name: {policy, image}}."""
    services = _load_compose(raw).get("services")
    if not isinstance(services, dict):
        return {}
    found = {}
    for name, service in services.items():
        if not isinstance(service, dict):
            continue
        labels = service.get("labels")
        policy = None
        if isinstance(labels, dict):
            policy = labels.get(SERIES_LABEL)
        elif isinstance(labels, list):
            for label in labels:
                key, _, value = str(label).partition("=")
                if key.strip() == SERIES_LABEL:
                    policy = value
        if policy is None:
            continue
        policy = str(policy).strip().lower()
        image = service.get("image")
        if policy not in SERIES_POLICIES:
            logger.warning(f"Unknown {SERIES_LABEL}={policy} on {name}, ignored")
        elif isinstance(image, str) and image and "$" not in image:
            found[name] = {"policy": policy, "image": image}
    return found


IMAGE_LINE = re.compile(r"(\s*image:\s*)(['\"]?)([^'\"#\s]+)\2(\s*(?:#.*)?)")


def rewrite_image_line(raw: str, service_name: str, new_image: str) -> str | None:
    """Replace the image: line of one compose service, and nothing else.

    Text is edited rather than parsed and re-dumped: a YAML round trip here
    would reinterpret values (yes, on, 010) and reorder the file. None when the
    service or its image: line cannot be found.
    """
    lines = raw.splitlines(keepends=True)
    in_services = in_target = False
    service_indent = key_indent = None
    for i, line in enumerate(lines):
        text = line.rstrip("\r\n")
        stripped = text.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(text) - len(text.lstrip(" "))
        if indent == 0:
            in_services = re.fullmatch(r"services:\s*(#.*)?", stripped) is not None
            in_target, service_indent = False, None
            continue
        if not in_services:
            continue
        if service_indent is None:
            service_indent = indent
        if indent <= service_indent:
            name = re.fullmatch(r"(['\"]?)([^'\":]+)\1:\s*(#.*)?", stripped)
            in_target = indent == service_indent and name is not None and name.group(2) == service_name
            key_indent = None
            continue
        if not in_target:
            continue
        if key_indent is None:
            key_indent = indent
        if indent == key_indent:
            match = IMAGE_LINE.fullmatch(text)
            if match:
                lead, quote, _, trail = match.groups()
                lines[i] = f"{lead}{quote}{new_image}{quote}{trail}{line[len(text):]}"
                return "".join(lines)
    return None


def compose_matches(saved_raw: str | None, original_raw: str, service_name: str,
                    new_image: str) -> bool:
    """Is the saved compose the original one with only that image changed?"""
    expected = _load_compose(original_raw)
    services = expected.get("services")
    if not isinstance(services, dict) or not isinstance(services.get(service_name), dict):
        return False
    services[service_name]["image"] = new_image
    return _load_compose(saved_raw) == expected


def with_tag(image: str, tag: str) -> str:
    """The same image reference with another tag."""
    ref = image.strip().split("@", 1)[0]
    head, _, last = ref.rpartition("/")
    name = last.split(":", 1)[0]
    return f"{head}/{name}:{tag}" if head else f"{name}:{tag}"


def registry_repository(image: str) -> tuple[str, str]:
    """Where to list an image's tags: (registry host, repository)."""
    normalized = normalize_image(image)
    head, separator, remainder = normalized.partition("/")
    if separator and ("." in head or ":" in head or head == "localhost"):
        return head, remainder
    repository = normalized if "/" in normalized else DOCKER_HUB_IMPLICIT_NAMESPACE + normalized
    return "registry-1.docker.io", repository


async def _registry_token(client: httpx.AsyncClient, challenge: str) -> str | None:
    """Get an anonymous pull token from the realm a registry's 401 points to."""
    params = dict(re.findall(r'(\w+)="([^"]*)"', challenge))
    realm = params.pop("realm", None)
    if not realm:
        return None
    resp = await client.get(realm, params=params)
    resp.raise_for_status()
    payload = resp.json()
    return payload.get("token") or payload.get("access_token")


async def list_registry_tags(image: str) -> list[str] | None:
    """Every tag the registry publishes for this image, or None on failure.

    Registry API v2 with an anonymous token: Docker Hub, ghcr.io and other
    public registries. Private registries are out of scope.
    """
    host, repository = registry_repository(image)
    url = f"https://{host}/v2/{repository}/tags/list?n=1000"
    headers: dict = {}
    tags: list[str] = []
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            for _ in range(REGISTRY_MAX_PAGES):
                resp = await client.get(url, headers=headers)
                if resp.status_code == 401 and not headers:
                    token = await _registry_token(client, resp.headers.get("WWW-Authenticate", ""))
                    if not token:
                        raise RuntimeError("no anonymous token offered")
                    headers = {"Authorization": f"Bearer {token}"}
                    resp = await client.get(url, headers=headers)
                resp.raise_for_status()
                tags.extend(resp.json().get("tags") or [])
                next_url = resp.links.get("next", {}).get("url")
                if not next_url:
                    break
                url = urljoin(url, next_url)
    except Exception as e:
        logger.error(f"✗ Could not list the tags of {host}/{repository}: {e}")
        return None
    logger.info(f"Registry {host}/{repository}: {len(tags)} tag(s)")
    return tags


def get_write_token() -> str:
    """The Coolify token allowed to read and rewrite composes.

    Reading docker_compose_raw needs read:sensitive and rewriting it needs
    write, which the deploy-only COOLIFY_TOKEN usually lacks.
    """
    return os.getenv("COOLIFY_WRITE_TOKEN", "").strip() or os.getenv("COOLIFY_TOKEN", "").strip()


async def get_service(coolify_url: str, coolify_token: str, uuid: str) -> dict | None:
    """Read one Coolify service, or None if unreachable."""
    url = f"{coolify_url.rstrip('/')}/api/v1/services/{uuid}"
    headers = {"Authorization": f"Bearer {coolify_token}", **get_cloudflare_headers()}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        logger.warning(f"Could not read service {uuid}: {type(e).__name__}")
        return None


async def patch_compose(coolify_url: str, coolify_token: str, uuid: str, raw: str) -> bool:
    """Save a new compose for a Coolify service.

    The response carries the service's environment variables in clear: it is
    never logged, only its status code.
    """
    url = f"{coolify_url.rstrip('/')}/api/v1/services/{uuid}"
    headers = {"Authorization": f"Bearer {coolify_token}", **get_cloudflare_headers()}
    body = {"docker_compose_raw": base64.b64encode(raw.encode()).decode()}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.patch(url, headers=headers, json=body)
    except Exception as e:
        logger.error(f"✗ Could not save the compose of service {uuid}: {type(e).__name__}")
        return False
    if resp.is_success:
        logger.info(f"✓ Compose of service {uuid} saved (status {resp.status_code})")
        return True
    logger.error(f"✗ Coolify refused the compose of service {uuid}: status {resp.status_code}")
    return False


def ignored_containers() -> list[str]:
    raw = os.getenv("IGNORE_CONTAINERS", "").strip()
    return [c.strip() for c in raw.split(",") if c.strip()]


def is_series_ignored(uuid: str, service_name: str) -> bool:
    """IGNORE_CONTAINERS names containers: Coolify names them <service>-<uuid>."""
    ignored = ignored_containers()
    return service_name in ignored or f"{service_name}-{uuid}" in ignored


def _series_lock(key: str) -> asyncio.Lock:
    return _series_locks.setdefault(key, asyncio.Lock())


def series_upgrade_blocked(key: str) -> str | None:
    """Why an upgrade of this resource cannot start now: "busy", "too-soon", or None."""
    if _series_lock(key).locked():
        logger.info(f"Upgrade of {key} already running, skipped")
        return "busy"
    if time.time() - _series_last_attempt.get(key, 0) < SERIES_MIN_INTERVAL_SECONDS:
        logger.info(f"{key} was already upgraded in the last 24 h, skipped")
        return "too-soon"
    return None


def spawn(coro) -> None:
    """Run a coroutine in the background, keeping a reference until it ends."""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


def build_upgrade_link(uuid: str, service_name: str, tag: str) -> str:
    """The manual link that applies a proposed upgrade (empty if not configurable)."""
    dispatcher_url = os.getenv("DISPATCHER_URL", "").strip()
    if not dispatcher_url:
        return ""
    webhook_secret = os.getenv("WEBHOOK_SECRET", "").strip()
    uuid_short = uuid[:SHORT_UUID_LENGTH]
    cache_uuid(uuid_short, uuid)
    secret_param = f"&secret={webhook_secret}" if webhook_secret else ""
    return (f"\n\n🚀 Apply: {dispatcher_url}/upgrade?uuid={uuid_short}"
            f"&service={service_name}&tag={tag}{secret_param}")


def _server_name(service: dict) -> str:
    return (service.get("server") or {}).get("name") or "unknown"


async def upgrade_resource(service: dict, service_name: str, target_tag: str) -> str:
    """Move one compose service of a Coolify service to target_tag, guarded.

    One upgrade at a time per resource, at most one attempt a day. Returns
    "applied", "failed", "busy" or "too-soon".
    """
    key = f"{service.get('uuid', '')}/{service_name}"
    blocked = series_upgrade_blocked(key)
    if blocked:
        return blocked
    async with _series_lock(key):
        _series_last_attempt[key] = time.time()
        return await _apply_series_upgrade(service, service_name, target_tag)


async def _apply_series_upgrade(service: dict, service_name: str, target_tag: str) -> str:
    coolify_url = os.getenv("COOLIFY_API_URL", "").strip()
    deploy_token = os.getenv("COOLIFY_TOKEN", "").strip()
    write_token = get_write_token()
    uuid = service.get("uuid", "")
    server = _server_name(service)
    urls = load_apprise_urls()

    # Work from the compose as saved right now, not from a listing that may be old
    current = await get_service(coolify_url, write_token, uuid)
    original_raw = (current or {}).get("docker_compose_raw")
    entry = series_policies(original_raw).get(service_name)
    if not entry:
        logger.error(f"✗ Cannot read the compose of {service_name} (service {uuid}): "
                     f"is COOLIFY_WRITE_TOKEN allowed to read sensitive data?")
        send_notification(urls, f"❌ {service_name} — cannot upgrade to {target_tag}",
                          build_notification_body(server, target_tag, service_name,
                                                  "\n\n⚠️ Compose not readable: nothing was changed."))
        return "failed"

    old_image = entry["image"]
    new_image = with_tag(old_image, target_tag)
    change = f"{image_tag(old_image)} → {target_tag}"
    baseline_status = current.get("status") or service.get("status", "")

    def fail(reason: str, restored: bool) -> str:
        restored_text = "\n↩️ Original compose restored." if restored else ""
        send_notification(urls, f"❌ {service_name} {change} failed",
                          build_notification_body(server, new_image, service_name,
                                                  f"\n\n⚠️ {reason}{restored_text}"))
        return "failed"

    async def restore() -> bool:
        return await patch_compose(coolify_url, write_token, uuid, original_raw)

    new_raw = rewrite_image_line(original_raw, service_name, new_image)
    if new_raw is None or not compose_matches(new_raw, original_raw, service_name, new_image):
        return fail("The image: line could not be rewritten: nothing was changed.", False)

    logger.info(f"Upgrading {service_name} (service {uuid}): {old_image} → {new_image}")
    if not await patch_compose(coolify_url, write_token, uuid, new_raw):
        # The save may still have gone through (a timeout): put the original back
        return fail("Coolify refused the new compose.", await restore())

    saved = await get_service(coolify_url, write_token, uuid)
    if not compose_matches((saved or {}).get("docker_compose_raw"), original_raw, service_name, new_image):
        logger.error(f"✗ The compose Coolify saved for {service_name} differs beyond the image line")
        return fail("The compose Coolify saved differs beyond the image: line.",
                    await restore())

    if not (await trigger_coolify(coolify_url, deploy_token, uuid))["ok"]:
        return fail("Coolify refused the redeploy.", await restore())

    back, status, note = await wait_for_service(coolify_url, deploy_token, uuid,
                                                baseline_status, service_name)
    if back:
        send_notification(urls, f"✅ {service_name} {change} applied",
                          build_notification_body(server, new_image, service_name,
                                                  f"\n📊 Status: {status}{note}"))
        return "applied"

    restored = await restore()
    if restored:
        # Bring the previous version back up
        await trigger_coolify(coolify_url, deploy_token, uuid)
    return fail(f"The service never came back (last status: {status}).", restored)


async def check_series(service: dict, service_name: str, policy: str, image: str) -> str | None:
    """Look for a newer tag within the policy and apply it, or propose it."""
    uuid = service.get("uuid", "")
    tags = await list_registry_tags(image)
    if not tags:
        return None
    current_tag = image_tag(image)
    target = pick_series_target(current_tag, tags, policy)
    if target is None:
        logger.info(f"{service_name} (service {uuid}) is up to date on {current_tag} ({policy})")
        return None

    if is_auto_deploy_enabled():
        return await upgrade_resource(service, service_name, target)

    key = f"{uuid}/{service_name}"
    if _series_proposed.get(key) == target:
        return None
    _series_proposed[key] = target
    send_notification(
        load_apprise_urls(),
        f"🆕 {service_name} {current_tag} → {target} available",
        build_notification_body(
            _server_name(service), with_tag(image, target), service_name,
            f"\n\nℹ️ Follow policy {policy}: AUTO_DEPLOY is off, nothing was applied."
            + build_upgrade_link(uuid, service_name, target)),
    )
    return "proposed"


async def run_series_check() -> None:
    """One pass over every Coolify resource carrying a series label."""
    coolify_url = os.getenv("COOLIFY_API_URL", "").strip()
    token = get_write_token()
    if not coolify_url or not token:
        logger.warning("Series check skipped: Coolify not configured")
        return
    services = await get_coolify_applications(coolify_url, token)
    if services and not any(s.get("docker_compose_raw") for s in services):
        logger.warning("Series check: Coolify hides every compose, "
                       "COOLIFY_WRITE_TOKEN needs the read:sensitive permission")
        return
    for service in services:
        uuid = service.get("uuid", "")
        for name, entry in series_policies(service.get("docker_compose_raw")).items():
            if is_series_ignored(uuid, name):
                logger.info(f"{name} (service {uuid}) is in IGNORE_CONTAINERS, series check skipped")
                continue
            try:
                await check_series(service, name, entry["policy"], entry["image"])
            except Exception:
                logger.exception(f"Series check of {name} (service {uuid}) failed")


async def find_series_entry(coolify_url: str, service: dict, image: str) -> tuple[dict, str, dict] | None:
    """The labelled compose service of this Coolify service that runs the image:
    (service with its compose, compose service name, {policy, image})."""
    full = await get_service(coolify_url, get_write_token(), service.get("uuid", ""))
    if not full:
        return None
    merged = {**service, **full}
    wanted = normalize_image(image)
    for name, entry in series_policies(merged.get("docker_compose_raw")).items():
        if normalize_image(entry["image"]) == wanted:
            return merged, name, entry
    return None


def series_check_hour() -> int:
    raw = os.getenv("SERIES_CHECK_HOUR", "").strip()
    if raw.isdigit() and int(raw) < 24:
        return int(raw)
    if raw:
        logger.warning(f"SERIES_CHECK_HOUR={raw} is not an hour (0-23), using {SERIES_DEFAULT_CHECK_HOUR}")
    return SERIES_DEFAULT_CHECK_HOUR


def seconds_until_next_check(now: datetime, hour: int) -> float:
    """Seconds from now to the next time the clock shows hour:00."""
    target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


async def series_check_loop() -> None:
    while True:
        await asyncio.sleep(seconds_until_next_check(datetime.now(), series_check_hour()))
        try:
            await run_series_check()
        except Exception:
            logger.exception("Series check failed")


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
    if container_name in ignored_containers():
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

    # "new": never a redeploy (see classify_new_tag). Only a tag newer than the
    # one in service is worth a notification; the user adopts it by changing the
    # tag in Coolify. Before this, a "new" event redeployed like an "update": a
    # Diun restarted with an empty database redeployed every resource at once.
    configured = find_configured_image(matched_service, image) if matched_service else None
    if status == "new":
        kind = classify_new_tag(configured, image)
        if kind != "newer":
            logger.info(f"New tag {image} is {kind} for {container_name} "
                        f"(configured: {configured}), nothing to do")
            return JSONResponse({"ok": True, "uuid": uuid, "action": f"new-tag-{kind}"})
        # A resource following a virtual series (diun-dispatcher.follow) moves
        # to a tag within its policy by itself: check it now rather than
        # announcing it. Beyond the policy, announce as usual.
        series = await find_series_entry(coolify_url, matched_service, image) if uuid else None
        if series:
            series_service, name, entry = series
            if pick_series_target(image_tag(entry["image"]), [image_tag(image)], entry["policy"]):
                logger.info(f"New tag {image} is within the {entry['policy']} policy of {name}, "
                            f"checking its series")
                spawn(check_series(series_service, name, entry["policy"], entry["image"]))
                return JSONResponse({"ok": True, "uuid": uuid, "action": "series-check"})
        running = f" (running {image_tag(configured)})" if configured else ""
        send_notification(
            apprise_urls,
            f"{status_emoji} {container_name} — new version available: {image_tag(image)}{running}",
            build_notification_body(
                server_display, image, container_name,
                "\n\nℹ️ No automatic deployment: change the tag in Coolify to upgrade."),
        )
        return JSONResponse({"ok": True, "uuid": uuid, "action": "new-tag-notified"})

    # "update" for another tag than the one in service: with watch_repo, Diun
    # also follows the other series it listed, and reports them when they are
    # republished. Resources are matched by repository, so without this check a
    # republished 1.26 or 1.28 would restart the resource running 1.27 for nothing.
    if configured and image_tag(configured) != image_tag(image):
        logger.info(f"Update of {image} is not the tag in service for {container_name} "
                    f"(configured: {configured}), nothing to do")
        return JSONResponse({"ok": True, "uuid": uuid, "action": "update-other-tag"})

    # AUTO_DEPLOY ("update" of the tag in service): redeploy right away and stay
    # silent until Coolify reports back.
    if uuid and is_auto_deploy_enabled():
        result = await trigger_coolify(coolify_url, coolify_token, uuid)
        if result["ok"]:
            baseline_status = matched_service.get("status", "")
            logger.info(
                f"Auto-deploy triggered for {container_name} (service {uuid}), "
                f"baseline status={baseline_status}"
            )
            asyncio.create_task(watch_deployment(
                coolify_url, coolify_token, uuid, baseline_status,
                container_name=container_name, image=image, server=server_display,
            ))
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


@app.get("/upgrade")
async def manual_upgrade(request: Request, uuid: str, service: str, tag: str, secret: str = ""):
    """Apply an upgrade proposed by the series check (link in the notification)."""
    expected_secret = os.getenv("WEBHOOK_SECRET", "").strip()
    if expected_secret and secret != expected_secret:
        logger.warning("Invalid upgrade secret")
        raise HTTPException(status_code=401, detail="Unauthorized")

    resolved_uuid = uuid
    if len(uuid) <= SHORT_UUID_LENGTH:
        resolved_uuid = get_uuid_from_cache(uuid)
        if not resolved_uuid:
            raise HTTPException(status_code=404, detail="UUID not found in cache (may be expired)")

    coolify_url = os.getenv("COOLIFY_API_URL", "").strip()
    if not coolify_url or not get_write_token():
        raise HTTPException(status_code=500, detail="Coolify not configured")

    coolify_service = await get_service(coolify_url, get_write_token(), resolved_uuid)
    entry = series_policies((coolify_service or {}).get("docker_compose_raw")).get(service)
    if not entry:
        raise HTTPException(status_code=404, detail=f"No {SERIES_LABEL} label on {service}")

    # Re-check everything the link claims: the tag must still be newer, within
    # the policy, and published.
    tags = await list_registry_tags(entry["image"]) or []
    if tag not in tags or pick_series_target(image_tag(entry["image"]), [tag], entry["policy"]) != tag:
        raise HTTPException(status_code=400,
                            detail=f"{tag} is not an upgrade within the {entry['policy']} policy")

    # The upgrade lasts as long as the restart (up to WATCH_TIMEOUT_SECONDS):
    # run it in the background, its outcome comes as a notification.
    blocked = series_upgrade_blocked(f"{resolved_uuid}/{service}")
    if not blocked:
        spawn(upgrade_resource(coolify_service, service, tag))
    return templates.TemplateResponse("deploy_confirmation.html", {
        "request": request,
        "deployed": blocked is None,
        "container_name": f"{service} ({blocked or 'upgrade started'})",
        "image": with_tag(entry["image"], tag),
        "hostname": _server_name(coolify_service),
        "uuid": resolved_uuid,
        "recent_deployments": _recent_deployments,
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


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return FileResponse(STATIC_DIR / "favicon.svg", media_type="image/svg+xml")
