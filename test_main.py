import os
import json
import time
import httpx
from unittest.mock import patch, AsyncMock, MagicMock
from fastapi.testclient import TestClient
from main import app, get_cloudflare_headers


def test_get_cloudflare_headers_with_both_credentials():
    """Test that both headers are returned when both env vars are set"""
    with patch.dict(os.environ, {
        "CF_ACCESS_CLIENT_ID": "test-client-id",
        "CF_ACCESS_CLIENT_SECRET": "test-client-secret"
    }):
        headers = get_cloudflare_headers()
        assert headers == {
            "CF-Access-Client-Id": "test-client-id",
            "CF-Access-Client-Secret": "test-client-secret"
        }


def test_get_cloudflare_headers_missing_client_id():
    """Test that empty dict is returned when CF_ACCESS_CLIENT_ID is missing"""
    with patch.dict(os.environ, {
        "CF_ACCESS_CLIENT_SECRET": "test-secret"
    }, clear=True):
        headers = get_cloudflare_headers()
        assert headers == {}


def test_get_cloudflare_headers_missing_secret():
    """Test that empty dict is returned when CF_ACCESS_CLIENT_SECRET is missing"""
    with patch.dict(os.environ, {
        "CF_ACCESS_CLIENT_ID": "test-id"
    }, clear=True):
        headers = get_cloudflare_headers()
        assert headers == {}


def test_get_cloudflare_headers_both_empty():
    """Test that empty dict is returned when both env vars are empty"""
    with patch.dict(os.environ, {
        "CF_ACCESS_CLIENT_ID": "",
        "CF_ACCESS_CLIENT_SECRET": ""
    }, clear=True):
        headers = get_cloudflare_headers()
        assert headers == {}


# ============================================================================
# Integration Tests for Routes
# ============================================================================

client = TestClient(app)


def test_health_endpoint():
    """Test that /health returns 200 OK"""
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_favicon_endpoint():
    """Test that /favicon.ico is served (no 404 when opening the page)"""
    response = client.get("/favicon.ico")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/svg+xml")
    assert b"<svg" in response.content


def test_deploy_missing_secret():
    """Test that /deploy rejects request without valid secret"""
    with patch.dict(os.environ, {"WEBHOOK_SECRET": "test-secret"}):
        response = client.get("/deploy?uuid=test-uuid&secret=wrong-secret")
        assert response.status_code == 401


def test_deploy_missing_uuid_in_cache():
    """Test that /deploy returns 404 when UUID not in cache"""
    with patch.dict(os.environ, {"WEBHOOK_SECRET": "test-secret"}):
        response = client.get("/deploy?uuid=unknown&secret=test-secret")
        assert response.status_code == 404


@patch('main.get_coolify_applications')
@patch('main.trigger_coolify')
def test_deploy_success(mock_trigger, mock_coolify):
    """Test successful /deploy with valid credentials"""
    # Mock Coolify API responses
    mock_coolify.return_value = [
        {
            "uuid": "full-uuid-12345",
            "server": {"name": "server1"},
            "applications": [
                {"name": "app1", "image": "nginx:latest"}
            ],
            "databases": []
        }
    ]
    mock_trigger.return_value = {"ok": True, "deployment_uuid": "dep-1"}

    with patch.dict(os.environ, {
        "WEBHOOK_SECRET": "test-secret",
        "COOLIFY_API_URL": "http://coolify",
        "COOLIFY_TOKEN": "token"
    }):
        # Cache the UUID first
        from main import cache_uuid
        cache_uuid("full-uui", "full-uuid-12345")

        # The background wait for the service is covered by its own tests
        with patch("main.spawn", new=lambda coro: coro.close()):
            response = client.get("/deploy?uuid=full-uui&secret=test-secret")
        assert response.status_code == 200
        assert "Deployment Status" in response.text


def test_api_deployments_missing_secret():
    """Test that /api/deployments requires secret"""
    with patch.dict(os.environ, {"WEBHOOK_SECRET": "test-secret"}):
        response = client.get("/api/deployments?secret=wrong")
        assert response.status_code == 401


@patch('main.get_coolify_applications')
def test_api_deployments_success(mock_coolify):
    """Test /api/deployments with valid secret"""
    mock_coolify.return_value = [
        {
            "uuid": "service-1",
            "server": {"name": "prod"},
            "applications": [
                {"name": "web", "image": "nginx:latest"}
            ],
            "databases": [
                {"name": "db", "image": "postgres:14"}
            ]
        }
    ]

    with patch.dict(os.environ, {
        "WEBHOOK_SECRET": "test-secret",
        "COOLIFY_API_URL": "http://coolify",
        "COOLIFY_TOKEN": "token"
    }):
        response = client.get("/api/deployments?secret=test-secret")
        assert response.status_code == 200
        data = response.json()
        assert "deployments" in data
        assert len(data["deployments"]) == 2
        assert data["deployments"][0]["container_name"] == "web"
        assert data["deployments"][1]["container_name"] == "db"


@patch('main.get_coolify_applications')
def test_api_deployments_filtering(mock_coolify):
    """Test /api/deployments with container filter"""
    mock_coolify.return_value = [
        {
            "uuid": "service-1",
            "server": {"name": "prod"},
            "applications": [
                {"name": "web", "image": "nginx:latest"},
                {"name": "api", "image": "node:18"}
            ],
            "databases": []
        }
    ]

    with patch.dict(os.environ, {
        "WEBHOOK_SECRET": "test-secret",
        "COOLIFY_API_URL": "http://coolify",
        "COOLIFY_TOKEN": "token"
    }):
        response = client.get("/api/deployments?secret=test-secret&container=web")
        assert response.status_code == 200
        data = response.json()
        assert len(data["deployments"]) == 1
        assert data["deployments"][0]["container_name"] == "web"


def test_webhook_invalid_secret():
    """Test that /webhook rejects invalid secret"""
    with patch.dict(os.environ, {"WEBHOOK_SECRET": "test-secret"}):
        payload = {"hostname": "host1", "status": "update", "image": "test:1.0"}
        response = client.post(
            "/webhook?secret=wrong",
            json=payload,
            headers={"Content-Type": "application/json"}
        )
        assert response.status_code == 401


@patch('main.send_notification')
def test_webhook_ignored_status(mock_notify):
    """Test that /webhook ignores non-update statuses"""
    with patch.dict(os.environ, {"WEBHOOK_SECRET": "test-secret"}):
        payload = {
            "hostname": "host1",
            "status": "ignored-status",
            "image": "test:1.0"
        }
        response = client.post(
            "/webhook?secret=test-secret",
            json=payload,
            headers={"Content-Type": "application/json"}
        )
        assert response.status_code == 200
        assert response.json()["action"] == "ignored"
        mock_notify.assert_not_called()


# ============================================================================
# Image normalization / matching (regression: official Docker Hub images)
# ============================================================================

import pytest
from main import normalize_image, find_service_uuid_by_image, find_service_by_image


@pytest.mark.parametrize("diun_image,coolify_image", [
    # Official Docker Hub images: Diun normalizes them with the "library/"
    # namespace, Coolify stores them bare.
    ("docker.io/library/nextcloud:34-apache", "nextcloud:34-apache"),
    ("docker.io/library/redis:7-alpine", "redis:7-alpine"),
    ("docker.io/library/postgres:17-alpine", "postgres:17-alpine"),
    ("docker.io/library/eclipse-mosquitto:latest", "eclipse-mosquitto"),
    ("docker.io/library/busybox:latest", "busybox"),
    # Namespaced Docker Hub images (already working, guard against regression)
    ("docker.io/crazymax/diun:latest", "crazymax/diun:latest"),
    ("docker.io/vikunja/vikunja:latest", "vikunja/vikunja"),
    ("docker.io/privatebin/nginx-fpm-alpine:latest", "privatebin/nginx-fpm-alpine"),
    # Explicit registries (already working, guard against regression)
    ("ghcr.io/mealie-recipes/mealie:v3.21.0", "ghcr.io/mealie-recipes/mealie:v3.21.0"),
    ("docker.n8n.io/n8nio/n8n:latest", "docker.n8n.io/n8nio/n8n"),
    ("registry.example.com/lgnap/utick-sapiti-filling:latest",
     "registry.example.com/lgnap/utick-sapiti-filling:latest"),
])
def test_normalize_image_matches_diun_and_coolify_forms(diun_image, coolify_image):
    """Diun sends fully normalized refs; Coolify stores compose-style refs."""
    assert normalize_image(diun_image) == normalize_image(coolify_image)


def test_normalize_image_keeps_registry_port():
    """A registry port must not be mistaken for a tag separator."""
    assert normalize_image("registry.internal:5000/team/app:1.2") == \
        "registry.internal:5000/team/app"


def test_normalize_image_strips_digest():
    assert normalize_image("docker.io/library/redis@sha256:abc123") == "redis"


def test_normalize_image_does_not_strip_library_from_other_registries():
    """'library' is only the implicit Docker Hub namespace."""
    assert normalize_image("ghcr.io/library/thing:1") == "ghcr.io/library/thing"


def test_find_service_uuid_matches_official_image_in_databases():
    """Regression: nextcloud-server2's postgres was reported as not deployable."""
    services = [
        {
            "uuid": "svc-nextcloud",
            "server": {"name": "server2"},
            "applications": [{"name": "nextcloud", "image": "nextcloud:34-apache"}],
            "databases": [
                {"name": "redis", "image": "redis:7-alpine"},
                {"name": "postgres", "image": "postgres:17-alpine"},
            ],
        }
    ]
    assert find_service_uuid_by_image(
        services, "docker.io/library/postgres:17-alpine") == "svc-nextcloud"
    assert find_service_uuid_by_image(
        services, "docker.io/library/nextcloud:34-apache") == "svc-nextcloud"


def test_find_service_uuid_returns_none_for_unknown_image():
    services = [
        {
            "uuid": "svc-1",
            "applications": [{"name": "a", "image": "nginx:latest"}],
            "databases": [],
        }
    ]
    assert find_service_uuid_by_image(services, "docker.io/library/mariadb:11") is None


# ============================================================================
# Server name comes from Coolify, not from Diun's container-id hostname
# ============================================================================


def test_find_service_by_image_exposes_server_name():
    services = [
        {
            "uuid": "svc-1",
            "server": {"name": "server2-prod"},
            "applications": [{"name": "nextcloud", "image": "nextcloud:34-apache"}],
            "databases": [],
        }
    ]
    service = find_service_by_image(services, "docker.io/library/nextcloud:34-apache")
    assert service is not None
    assert service["uuid"] == "svc-1"
    assert service["server"]["name"] == "server2-prod"


# Two services pulling the same repository with different tags (prod :latest,
# staging :staging): the event must reach the one running the container.
STAGING_AND_PROD = [
    {"uuid": "svc000000000000000000001", "server": {"name": "server1"},
     "applications": [{"name": "nginx", "image": "registry.example.com/namour/mmss-nginx:staging"}], "databases": []},
    {"uuid": "svc000000000000000000002", "server": {"name": "server1"},
     "applications": [{"name": "nginx", "image": "registry.example.com/namour/mmss-nginx:latest"}], "databases": []},
]


def test_find_service_by_image_prefers_the_service_running_the_container():
    service = find_service_by_image(STAGING_AND_PROD, "registry.example.com/namour/mmss-nginx:latest",
                                    container_name="nginx-svc000000000000000000002")
    assert service["uuid"] == "svc000000000000000000002"


def test_find_service_by_image_reads_every_container_name():
    """Diun joins the names of a container with commas."""
    service = find_service_by_image(STAGING_AND_PROD, "registry.example.com/namour/mmss-nginx:staging",
                                    container_name="other, nginx-svc000000000000000000001")
    assert service["uuid"] == "svc000000000000000000001"


def test_find_service_by_image_without_container_prefers_the_same_tag():
    service = find_service_by_image(STAGING_AND_PROD, "registry.example.com/namour/mmss-nginx:latest")
    assert service["uuid"] == "svc000000000000000000002"


def test_find_service_by_image_falls_back_to_the_repository():
    """watch_repo: another tag of the repository still finds the service (and is
    then left alone because its tag is not the one in service)."""
    service = find_service_by_image(STAGING_AND_PROD[:1], "registry.example.com/namour/mmss-nginx:1.2",
                                    container_name="unknown")
    assert service["uuid"] == "svc000000000000000000001"


@patch('main.send_notification')
@patch('main.get_coolify_applications')
def test_webhook_uses_coolify_server_name(mock_coolify, mock_notify):
    """A matched image is labelled with Coolify's server name, not Diun's hostname."""
    mock_coolify.return_value = [
        {
            "uuid": "svc-1",
            "server": {"name": "server2-prod"},
            "applications": [{"name": "nextcloud", "image": "nextcloud:34-apache"}],
            "databases": [],
        }
    ]
    with patch.dict(os.environ, {
        "COOLIFY_API_URL": "http://coolify",
        "COOLIFY_TOKEN": "token",
    }, clear=True):
        payload = {
            "hostname": "b90c71eaee78",  # Diun's default container-id hostname
            "status": "update",
            "image": "docker.io/library/nextcloud:34-apache",
            "metadata": {"ctn_names": "nextcloud-abc"},
        }
        resp = client.post("/webhook", json=payload,
                           headers={"Content-Type": "application/json"})
        assert resp.status_code == 200
        body = mock_notify.call_args.args[2]
        assert "Server: server2-prod" in body
        assert "b90c71eaee78" not in body


@patch('main.send_notification')
@patch('main.get_coolify_applications')
def test_webhook_accepts_get_with_body(mock_coolify, mock_notify):
    """Diun's default webhook method is GET with a JSON body; it must not 405."""
    mock_coolify.return_value = []
    with patch.dict(os.environ, {}, clear=True):
        payload = {"hostname": "srv", "status": "update", "image": "nextcloud:34-apache"}
        resp = client.request("GET", "/webhook", content=json.dumps(payload),
                              headers={"Content-Type": "application/json"})
        assert resp.status_code == 200
        mock_notify.assert_called_once()


@patch('main.send_notification')
@patch('main.get_coolify_applications')
def test_webhook_falls_back_to_diun_hostname_without_match(mock_coolify, mock_notify):
    """With no Coolify match, keep Diun's reported hostname (domain trimmed)."""
    mock_coolify.return_value = [
        {
            "uuid": "svc-1",
            "server": {"name": "server2-prod"},
            "applications": [{"name": "other", "image": "otherimage:1"}],
            "databases": [],
        }
    ]
    with patch.dict(os.environ, {
        "COOLIFY_API_URL": "http://coolify",
        "COOLIFY_TOKEN": "token",
    }, clear=True):
        payload = {
            "hostname": "diun-host.example.com",
            "status": "update",
            "image": "docker.io/library/nginx:latest",
            "metadata": {"ctn_names": "nginx"},
        }
        resp = client.post("/webhook", json=payload,
                           headers={"Content-Type": "application/json"})
        assert resp.status_code == 200
        body = mock_notify.call_args.args[2]
        assert "Server: diun-host" in body
        assert "server2-prod" not in body


# ============================================================================
# Coolify deploy trigger must use POST (GET returns 405 Method Not Allowed)
# ============================================================================


@patch("main.httpx.AsyncClient")
def test_trigger_coolify_uses_post(mock_client_cls):
    import asyncio
    from main import trigger_coolify

    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status = MagicMock()

    http_client = MagicMock()
    http_client.post = AsyncMock(return_value=resp)
    http_client.get = AsyncMock(side_effect=AssertionError("deploy must POST, not GET"))

    cm = mock_client_cls.return_value
    cm.__aenter__ = AsyncMock(return_value=http_client)
    cm.__aexit__ = AsyncMock(return_value=False)

    result = asyncio.run(trigger_coolify("http://coolify", "tok", "svc-1"))

    assert result["ok"] is True
    http_client.post.assert_awaited_once()
    assert http_client.post.call_args.args[0] == \
        "http://coolify/api/v1/services/svc-1/restart?latest=true"


# ============================================================================
# AUTO_DEPLOY: the dispatcher redeploys by itself instead of sending a link
# ============================================================================


MATCHING_SERVICE = {
    "uuid": "svc-1",
    "status": "running:healthy",
    "server": {"name": "server2-prod"},
    "applications": [{"name": "nextcloud", "image": "nextcloud:34-apache"}],
    "databases": [],
}

# "update": the tag in service was republished -- the only event that deploys.
DIUN_PAYLOAD = {
    "hostname": "diun-host",
    "status": "update",
    "image": "docker.io/library/nextcloud:34-apache",
    "metadata": {"ctn_names": "nextcloud"},
}

AUTO_DEPLOY_ENV = {
    "COOLIFY_API_URL": "http://coolify",
    "COOLIFY_TOKEN": "token",
    "AUTO_DEPLOY": "true",
    "DISPATCHER_URL": "http://dispatcher",
    "WEBHOOK_SECRET": "s3cret",
}


def test_auto_deploy_is_disabled_by_default():
    from main import is_auto_deploy_enabled
    with patch.dict(os.environ, {}, clear=True):
        assert is_auto_deploy_enabled() is False


def test_auto_deploy_accepts_common_truthy_values():
    from main import is_auto_deploy_enabled
    for value in ("true", "TRUE", "1", "yes", "on"):
        with patch.dict(os.environ, {"AUTO_DEPLOY": value}, clear=True):
            assert is_auto_deploy_enabled() is True, value


def test_auto_deploy_treats_other_values_as_disabled():
    from main import is_auto_deploy_enabled
    for value in ("false", "0", "no", ""):
        with patch.dict(os.environ, {"AUTO_DEPLOY": value}, clear=True):
            assert is_auto_deploy_enabled() is False, value


@patch("main.get_service_status", new_callable=AsyncMock, return_value="running:healthy")
@patch('main.watch_deployment')
@patch('main.send_notification')
@patch('main.trigger_coolify')
@patch('main.get_coolify_applications')
def test_webhook_triggers_deploy_when_auto_deploy_enabled(mock_coolify, mock_trigger, mock_notify, mock_watch, mock_status):
    """With AUTO_DEPLOY on, a matched image is redeployed without waiting for a click."""
    mock_coolify.return_value = [MATCHING_SERVICE]
    mock_trigger.return_value = {"ok": True, "deployment_uuid": "dep-1"}

    resp, = _post_webhooks([DIUN_PAYLOAD])

    assert resp.status_code == 200
    assert mock_trigger.call_args.args[2] == "svc-1"


@patch('main.watch_deployment')
@patch('main.send_notification')
@patch('main.trigger_coolify')
@patch('main.get_coolify_applications')
def test_webhook_stays_silent_until_deployment_finishes(mock_coolify, mock_trigger, mock_notify, mock_watch):
    """A successful auto-deploy notifies only once Coolify reports back."""
    mock_coolify.return_value = [MATCHING_SERVICE]
    mock_trigger.return_value = {"ok": True, "deployment_uuid": "dep-1"}

    with patch.dict(os.environ, AUTO_DEPLOY_ENV, clear=True):
        client.post("/webhook", json=DIUN_PAYLOAD, headers={"X-Diun-Secret": "s3cret"})

    mock_notify.assert_not_called()


@patch("main.get_service_status", new_callable=AsyncMock, return_value="running:healthy")
@patch('main.send_notification')
@patch('main.trigger_coolify')
@patch('main.get_coolify_applications')
def test_webhook_notifies_with_manual_link_when_trigger_fails(mock_coolify, mock_trigger, mock_notify, mock_status):
    """If Coolify refuses the deploy, the user is told and gets the manual link."""
    mock_coolify.return_value = [MATCHING_SERVICE]
    mock_trigger.return_value = {"ok": False, "deployment_uuid": None}

    _post_webhooks([DIUN_PAYLOAD])

    mock_notify.assert_called_once()
    title = mock_notify.call_args.args[1]
    body = mock_notify.call_args.args[2]
    assert "nextcloud" in title
    assert "\u274c" in title, "an auto-deploy that could not be triggered must read as a failure"
    assert "/deploy?uuid=" in body


@patch('main.send_notification')
@patch('main.trigger_coolify')
@patch('main.get_coolify_applications')
def test_webhook_keeps_manual_link_when_auto_deploy_disabled(mock_coolify, mock_trigger, mock_notify):
    """Default behaviour is unchanged: notify with a link, deploy nothing."""
    mock_coolify.return_value = [MATCHING_SERVICE]

    env = {**AUTO_DEPLOY_ENV, "AUTO_DEPLOY": "false"}
    with patch.dict(os.environ, env, clear=True):
        client.post("/webhook", json=DIUN_PAYLOAD, headers={"X-Diun-Secret": "s3cret"})

    mock_trigger.assert_not_called()
    mock_notify.assert_called_once()
    assert "/deploy?uuid=" in mock_notify.call_args.args[2]


@patch('main.send_notification')
@patch('main.trigger_coolify')
@patch('main.get_coolify_applications')
def test_ignored_container_is_never_auto_deployed(mock_coolify, mock_trigger, mock_notify):
    """IGNORE_CONTAINERS wins over AUTO_DEPLOY."""
    mock_coolify.return_value = [MATCHING_SERVICE]

    env = {**AUTO_DEPLOY_ENV, "IGNORE_CONTAINERS": "nextcloud"}
    with patch.dict(os.environ, env, clear=True):
        client.post("/webhook", json=DIUN_PAYLOAD, headers={"X-Diun-Secret": "s3cret"})

    mock_trigger.assert_not_called()
    mock_notify.assert_not_called()


def _mock_httpx_post(mock_client_cls, resp=None, error=None):
    """Wire a mocked httpx.AsyncClient whose post() returns resp or raises error."""
    http_client = MagicMock()
    if error is not None:
        http_client.post = AsyncMock(side_effect=error)
    else:
        http_client.post = AsyncMock(return_value=resp)
    cm = mock_client_cls.return_value
    cm.__aenter__ = AsyncMock(return_value=http_client)
    cm.__aexit__ = AsyncMock(return_value=False)
    return http_client


@patch("main.httpx.AsyncClient")
def test_trigger_coolify_returns_the_deployment_uuid(mock_client_cls):
    """Coolify answers with the queued deployment id; we need it to track the result."""
    import asyncio
    from main import trigger_coolify

    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {
        "deployments": [{"resource_uuid": "svc-1", "deployment_uuid": "dep-9"}]
    }
    _mock_httpx_post(mock_client_cls, resp=resp)

    result = asyncio.run(trigger_coolify("http://coolify", "tok", "svc-1"))

    assert result == {"ok": True, "deployment_uuid": "dep-9"}


@patch("main.httpx.AsyncClient")
def test_trigger_coolify_survives_an_unexpected_response_body(mock_client_cls):
    """A deploy that succeeds without a parsable body is still a success."""
    import asyncio
    from main import trigger_coolify

    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status = MagicMock()
    resp.json.side_effect = ValueError("not json")
    _mock_httpx_post(mock_client_cls, resp=resp)

    result = asyncio.run(trigger_coolify("http://coolify", "tok", "svc-1"))

    assert result == {"ok": True, "deployment_uuid": None}


@patch("main.httpx.AsyncClient")
def test_trigger_coolify_reports_failure(mock_client_cls):
    import asyncio
    from main import trigger_coolify

    _mock_httpx_post(mock_client_cls, error=RuntimeError("boom"))

    result = asyncio.run(trigger_coolify("http://coolify", "tok", "svc-1"))

    assert result == {"ok": False, "deployment_uuid": None}


# ============================================================================
# Deploying must pull the new image, or the whole feature is pointless
# ============================================================================


@patch("main.httpx.AsyncClient")
def test_trigger_coolify_asks_coolify_to_pull_the_latest_images(mock_client_cls):
    """/api/v1/deploy does not repull an image a service already has locally.

    The restart endpoint with latest=true is Coolify's "pull latest images and
    restart", and it leaves the compose file alone: an ordinary restart still
    deploys the same content.
    """
    import asyncio
    from main import trigger_coolify

    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {"message": "Service restaring request queued."}
    http_client = _mock_httpx_post(mock_client_cls, resp=resp)

    result = asyncio.run(trigger_coolify("http://coolify", "tok", "svc-1"))

    url = http_client.post.call_args.args[0]
    assert "/api/v1/services/svc-1/restart" in url
    assert "latest=true" in url
    assert result == {"ok": True, "deployment_uuid": None}


# ============================================================================
# Watching a deployment through Coolify's resource status
# ============================================================================


def test_a_running_service_meets_an_unhealthy_baseline():
    """A resource with no healthcheck reports running:unhealthy forever."""
    from main import status_is_at_least
    assert status_is_at_least("running:unhealthy", "running:unhealthy") is True
    assert status_is_at_least("running:healthy", "running:unhealthy") is True


def test_a_healthy_baseline_demands_a_healthy_return():
    """A resource that reported healthy before must report healthy again."""
    from main import status_is_at_least
    assert status_is_at_least("running:unhealthy", "running:healthy") is False
    assert status_is_at_least("running:healthy", "running:healthy") is True


def test_a_service_that_is_not_running_never_qualifies():
    from main import status_is_at_least
    for status in ("restarting:unhealthy", "exited:unhealthy", "degraded:unhealthy"):
        assert status_is_at_least(status, "running:unhealthy") is False, status


def test_a_status_without_health_suffix_is_accepted():
    from main import status_is_at_least
    assert status_is_at_least("running", "running") is True


def _watch(statuses, baseline="running:healthy", **kwargs):
    """Run watch_deployment against a canned sequence of Coolify statuses, on a
    simulated clock that each sleep advances. The last status repeats."""
    import asyncio
    import main
    clock = {"now": 1000.0}
    remaining = list(statuses)

    async def next_status(*args):
        return remaining.pop(0) if len(remaining) > 1 else remaining[0]

    async def sleep(seconds):
        clock["now"] += seconds

    with patch('main.get_service_status', new=AsyncMock(side_effect=next_status)) as status, \
         patch('main.asyncio.sleep', new=sleep), \
         patch('main.time.time', new=lambda: clock["now"]):
        asyncio.run(main.watch_deployment(
            "http://coolify", "tok", "svc-1", baseline,
            container_name="meshmonitor", image="ghcr.io/yeraze/meshmonitor:latest",
            server="server2-prod", **kwargs))
    status.elapsed = clock["now"] - 1000.0
    return status


@patch('main.send_notification')
def test_watch_reports_success_once_the_service_is_back(mock_notify):
    _watch(["restarting:unhealthy", "running:healthy"])

    mock_notify.assert_called_once()
    title, body = mock_notify.call_args.args[1], mock_notify.call_args.args[2]
    assert "✅" in title
    assert "meshmonitor" in title
    assert "running:healthy" in body


@patch('main.send_notification')
def test_watch_accepts_unhealthy_for_a_service_without_healthcheck(mock_notify):
    _watch(["restarting:unhealthy", "running:unhealthy"], baseline="running:unhealthy")

    mock_notify.assert_called_once()
    assert "✅" in mock_notify.call_args.args[1]


@patch('main.send_notification')
def test_watch_keeps_waiting_while_a_healthy_service_is_still_unhealthy(mock_notify):
    """running:unhealthy is not good enough when the resource has a healthcheck."""
    status = _watch(["running:unhealthy", "running:unhealthy", "running:healthy"])

    assert status.await_count >= 3
    mock_notify.assert_called_once()
    assert "✅" in mock_notify.call_args.args[1]


@patch('main.send_notification')
def test_watch_concludes_anyway_when_the_restart_was_never_observed(mock_notify):
    """Coolify refreshes statuses on its own schedule; a quick restart can be missed."""
    _watch(["running:healthy"], grace=0)

    mock_notify.assert_called_once()
    assert "✅" in mock_notify.call_args.args[1]
    body = mock_notify.call_args.args[2]
    assert "too quick" in body.lower()
    assert "⚠️" not in body, "a quick restart is the normal case, not a warning"


def test_watch_polls_often_enough_to_catch_a_short_restart():
    """Measured: the 'starting' window lasts ~6 s. At 5 s we saw it exactly once."""
    import main
    assert main.WATCH_FAST_INTERVAL_SECONDS <= 1
    assert main.WATCH_TRANSITION_GRACE_SECONDS <= 60


def test_watch_slows_down_once_the_transition_window_is_over():
    """A stuck service must not be hammered every second for 15 minutes."""
    from main import watch_interval
    assert watch_interval(elapsed=0) == 1
    assert watch_interval(elapsed=59) == 1
    assert watch_interval(elapsed=60) == 15
    assert watch_interval(elapsed=600) == 15


@patch('main.send_notification')
def test_watch_logs_only_status_changes(mock_notify, caplog):
    """At 1 s a poll per line would be 60 identical lines a minute."""
    import logging
    with caplog.at_level(logging.INFO, logger="main"):
        _watch(["running:healthy", "running:healthy", "starting:unhealthy",
                "starting:unhealthy", "running:healthy"])
    watching = [r.message for r in caplog.records if r.message.startswith("Watching")]
    assert len(watching) == 3, watching


@patch('main.send_notification')
def test_watch_waits_for_the_service_to_stay_back(mock_notify):
    """Coolify said running:healthy 2 s after mealie's new container started,
    while Docker still had it at health: starting (2026-09-24): one good status
    right after the restart proves nothing."""
    import main
    status = _watch(["starting:unhealthy", "running:healthy"])

    mock_notify.assert_called_once()
    assert "✅" in mock_notify.call_args.args[1]
    assert status.elapsed >= main.WATCH_STABLE_SECONDS


@patch('main.send_notification')
def test_watch_does_not_report_success_for_a_version_that_crashes_after_starting(mock_notify):
    _watch(["starting:unhealthy", "running:healthy", "running:healthy", "exited:unhealthy"],
           timeout=300)

    mock_notify.assert_called_once()
    assert "⏱️" in mock_notify.call_args.args[1]
    assert "exited:unhealthy" in mock_notify.call_args.args[2]


@patch('main.send_notification')
def test_watch_restarts_the_stability_window_after_a_relapse(mock_notify):
    import main
    statuses = (["starting:unhealthy"] + ["running:healthy"] * 10
                + ["restarting:unhealthy", "running:healthy"])
    status = _watch(statuses)

    mock_notify.assert_called_once()
    assert "✅" in mock_notify.call_args.args[1]
    # 10 polls at 1 s before the relapse, then a full window after it
    assert status.elapsed >= 11 + main.WATCH_STABLE_SECONDS


def test_watch_stability_window_fits_in_the_fast_polling_window():
    """At 1 s polls, a relapse inside the window is caught; after it, polls are 15 s apart."""
    import main
    assert 15 <= main.WATCH_STABLE_SECONDS < main.WATCH_TRANSITION_GRACE_SECONDS


@patch('main.send_notification')
def test_watch_gives_up_after_the_timeout(mock_notify):
    _watch(["restarting:unhealthy"], timeout=0)

    mock_notify.assert_called_once()
    title, body = mock_notify.call_args.args[1], mock_notify.call_args.args[2]
    assert "⏱️" in title
    assert "restarting:unhealthy" in body


@patch('main.send_notification')
def test_watch_survives_an_unreachable_coolify(mock_notify):
    """A failed status call must not crash the watcher, nor count as success."""
    _watch([None], timeout=0)

    mock_notify.assert_called_once()
    assert "⏱️" in mock_notify.call_args.args[1]


@patch("main.get_service_status", new_callable=AsyncMock, return_value=None)
@patch('main.watch_deployment')
@patch('main.send_notification')
@patch('main.trigger_coolify')
@patch('main.get_coolify_applications')
def test_auto_deploy_watches_the_service_it_restarted(mock_coolify, mock_trigger,
                                                      mock_notify, mock_watch, mock_status):
    """Without a fresher status, the baseline is the one of the service listing."""
    mock_coolify.return_value = [MATCHING_SERVICE]
    mock_trigger.return_value = {"ok": True, "deployment_uuid": None}

    _post_webhooks([DIUN_PAYLOAD])

    mock_notify.assert_not_called()
    mock_watch.assert_called_once()
    args, kwargs = mock_watch.call_args
    assert args[2] == "svc-1"
    assert args[3] == "running:healthy"
    assert kwargs["container_name"] == "nextcloud"
    assert kwargs["server"] == "server2-prod"


# ---------------------------------------------------------------------------
# "new" events: never a redeploy, a notification only for a newer series
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("image, tag", [
    ("docker.io/library/nextcloud:34-apache", "34-apache"),
    ("gitea/gitea", "latest"),
    ("registry.local:5000/team/app:1.2", "1.2"),
    ("registry.local:5000/team/app", "latest"),
    ("redis:7@sha256:abc", "7"),
])
def test_image_tag(image, tag):
    from main import image_tag
    assert image_tag(image) == tag


@pytest.mark.parametrize("tag, key", [
    ("1.27", (1, 27)),
    ("v6.10.1", (6, 10, 1)),
    ("11.8-noble", (11, 8)),
    ("34-apache", (34,)),
    ("latest", None),
    ("alpine", None),
])
def test_version_key(tag, key):
    from main import version_key
    assert version_key(tag) == key


@pytest.mark.parametrize("configured, image, kind", [
    ("nextcloud:34-apache", "docker.io/library/nextcloud:34-apache", "in-service"),
    ("nextcloud:34-apache", "docker.io/library/nextcloud:33-apache", "older"),
    ("nextcloud:34-apache", "docker.io/library/nextcloud:35-apache", "newer"),
    ("gitea/gitea:1.27", "docker.io/gitea/gitea:1.28", "newer"),
    ("gitea/gitea:1.27", "docker.io/gitea/gitea:1.27.4", "in-series"),
    ("crazymax/diun:4", "docker.io/crazymax/diun:4.34.0", "in-series"),
    ("postgres:18-alpine", "docker.io/library/postgres:18.7", "in-series"),
    ("n8nio/n8n:2.40.5", "docker.io/n8nio/n8n:2.40.6", "newer"),
    ("n8nio/n8n:2.40.5", "docker.io/n8nio/n8n:2.41", "newer"),
    ("n8nio/n8n:2.40.5", "docker.io/n8nio/n8n:2.40", "older"),
    ("gitea/gitea:1.27", "docker.io/gitea/gitea:1.9", "older"),
    ("gitea/gitea:latest", "docker.io/gitea/gitea:1.28", "rolling"),
    ("mwader/postfix-relay:trixie", "docker.io/mwader/postfix-relay:2.1", "rolling"),
    ("gitea/gitea", "docker.io/gitea/gitea:latest", "in-service"),
    (None, "docker.io/library/nextcloud:35-apache", "unmanaged"),
])
def test_classify_new_tag(configured, image, kind):
    from main import classify_new_tag
    assert classify_new_tag(configured, image) == kind


def _new_event(image):
    return {**DIUN_PAYLOAD, "status": "new", "image": image}


@patch('main.send_notification')
@patch('main.trigger_coolify')
@patch('main.get_coolify_applications')
def test_new_event_for_the_tag_in_service_does_nothing(mock_coolify, mock_trigger, mock_notify):
    """A Diun restarted with an empty database reports every image in service as
    "new": that must neither redeploy nor notify (it used to redeploy them all)."""
    mock_coolify.return_value = [MATCHING_SERVICE]

    with patch.dict(os.environ, AUTO_DEPLOY_ENV, clear=True):
        resp = client.post("/webhook", json=_new_event("docker.io/library/nextcloud:34-apache"),
                           headers={"X-Diun-Secret": "s3cret"})

    assert resp.json()["action"] == "new-tag-in-service"
    mock_trigger.assert_not_called()
    mock_notify.assert_not_called()


@patch('main.send_notification')
@patch('main.trigger_coolify')
@patch('main.get_coolify_applications')
def test_new_event_for_an_older_series_is_ignored(mock_coolify, mock_trigger, mock_notify):
    """watch_repo also lists older tags: they are not news."""
    mock_coolify.return_value = [MATCHING_SERVICE]

    with patch.dict(os.environ, AUTO_DEPLOY_ENV, clear=True):
        resp = client.post("/webhook", json=_new_event("docker.io/library/nextcloud:33-apache"),
                           headers={"X-Diun-Secret": "s3cret"})

    assert resp.json()["action"] == "new-tag-older"
    mock_trigger.assert_not_called()
    mock_notify.assert_not_called()


@patch('main.send_notification')
@patch('main.trigger_coolify')
@patch('main.get_coolify_applications')
def test_new_event_for_a_newer_series_notifies_without_deploying(mock_coolify, mock_trigger, mock_notify):
    """A newer series is announced, never deployed: redeploying would only pull
    the tag already configured, and a major upgrade is the user's call."""
    mock_coolify.return_value = [MATCHING_SERVICE]

    with patch.dict(os.environ, AUTO_DEPLOY_ENV, clear=True), \
         patch("main.get_service", new=AsyncMock(return_value=None)):
        resp = client.post("/webhook", json=_new_event("docker.io/library/nextcloud:35-apache"),
                           headers={"X-Diun-Secret": "s3cret"})

    assert resp.json()["action"] == "new-tag-notified"
    mock_trigger.assert_not_called()
    mock_notify.assert_called_once()
    title = mock_notify.call_args.args[1]
    body = mock_notify.call_args.args[2]
    assert "35-apache" in title and "running 34-apache" in title
    assert "/deploy?uuid=" not in body, "a deploy link would redeploy the old tag"


@patch('main.send_notification')
@patch('main.trigger_coolify')
@patch('main.get_coolify_applications')
def test_new_event_without_matching_resource_is_not_announced(mock_coolify, mock_trigger, mock_notify):
    """Containers Coolify does not manage (its own database, buildkit) cannot be
    upgraded from here, and with no tag to compare to, every tag would look new."""
    mock_coolify.return_value = []

    with patch.dict(os.environ, AUTO_DEPLOY_ENV, clear=True):
        resp = client.post("/webhook", json=_new_event("docker.io/library/nextcloud:35-apache"),
                           headers={"X-Diun-Secret": "s3cret"})

    assert resp.json()["action"] == "new-tag-unmanaged"
    mock_trigger.assert_not_called()
    mock_notify.assert_not_called()


@patch("main.get_service_status", new_callable=AsyncMock, return_value="running:healthy")
@patch('main.watch_deployment')
@patch('main.send_notification')
@patch('main.trigger_coolify')
@patch('main.get_coolify_applications')
def test_update_event_still_auto_deploys(mock_coolify, mock_trigger, mock_notify, mock_watch, mock_status):
    """An "update" (the tag in service was republished) keeps deploying by itself."""
    mock_coolify.return_value = [MATCHING_SERVICE]
    mock_trigger.return_value = {"ok": True, "deployment_uuid": "dep-1"}

    resp, = _post_webhooks([DIUN_PAYLOAD])

    assert resp.json()["action"] == "auto-deploy-scheduled"
    mock_trigger.assert_called_once()


@patch('main.send_notification')
@patch('main.trigger_coolify')
@patch('main.get_coolify_applications')
def test_update_of_another_tag_than_the_one_in_service_does_nothing(mock_coolify, mock_trigger, mock_notify):
    """With watch_repo, Diun also reports republished tags of other series; they
    must not restart the resource, which is matched by repository only."""
    mock_coolify.return_value = [MATCHING_SERVICE]

    for other in ("docker.io/library/nextcloud:33-apache", "docker.io/library/nextcloud:35-apache"):
        with patch.dict(os.environ, AUTO_DEPLOY_ENV, clear=True):
            resp = client.post("/webhook", json={**DIUN_PAYLOAD, "image": other},
                               headers={"X-Diun-Secret": "s3cret"})
        assert resp.json()["action"] == "update-other-tag", other

    mock_trigger.assert_not_called()
    mock_notify.assert_not_called()


@patch('main.send_notification')
@patch('main.trigger_coolify')
@patch('main.get_coolify_applications')
def test_new_release_inside_the_series_in_service_is_not_announced(mock_coolify, mock_trigger, mock_notify):
    """A 1.27.4 for a resource pinned on 1.27 reaches it as an "update" of 1.27:
    announcing it as a new version would only be noise."""
    mock_coolify.return_value = [{**MATCHING_SERVICE, "applications": [
        {"name": "gitea", "image": "gitea/gitea:1.27"}]}]

    with patch.dict(os.environ, AUTO_DEPLOY_ENV, clear=True):
        resp = client.post("/webhook", json=_new_event("docker.io/gitea/gitea:1.27.4"),
                           headers={"X-Diun-Secret": "s3cret"})

    assert resp.json()["action"] == "new-tag-in-series"
    mock_trigger.assert_not_called()
    mock_notify.assert_not_called()


def test_deploy_link_does_not_log_the_secret(caplog):
    """The link carries WEBHOOK_SECRET: it goes to the notification, never to the logs."""
    import logging
    from main import build_deploy_link
    env = {"DISPATCHER_URL": "https://dispatcher.example", "WEBHOOK_SECRET": "s3cret"}
    with patch.dict(os.environ, env, clear=True), caplog.at_level(logging.INFO, logger="main"):
        link = build_deploy_link("abcdefgh12345678")
    assert "secret=s3cret" in link
    assert "s3cret" not in caplog.text


def test_access_log_masks_the_secret_query_parameter():
    """Uvicorn logs the full path of a click on /deploy, query string included."""
    import logging
    from main import SecretQueryFilter
    record = logging.LogRecord("uvicorn.access", logging.INFO, "", 0,
                               '%s - "%s %s HTTP/%s" %d',
                               ("1.2.3.4:5", "GET", "/deploy?uuid=abc&secret=s3cret", "1.1", 200),
                               None)
    assert SecretQueryFilter().filter(record)
    assert "s3cret" not in record.getMessage()
    assert "/deploy?uuid=abc&secret=***" in record.getMessage()


# ---------------------------------------------------------------------------
# Virtual series: the dispatcher moves the tag itself, within a policy
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tag, parsed", [
    ("v6.10.1", ("v", (6, 10, 1), "")),
    ("2.40.5", ("", (2, 40, 5), "")),
    ("11.8-noble", ("", (11, 8), "-noble")),
    ("v3.0.0-rc1", ("v", (3, 0, 0), "-rc1")),
    ("latest", None),
    ("alpine3.20", None),
])
def test_parse_tag(tag, parsed):
    from main import parse_tag
    assert parse_tag(tag) == parsed


LYCHEE_TAGS = ["v6.9.0", "v6.10.0", "v6.10.1", "v6.10.2", "v6.10.4", "v6.10.3",
               "v6.11.0", "v6.11.1", "v7.0.0", "v6.10.5-rc1", "6.10.9", "v6.10",
               "v6.10.6-legacy", "v6", "latest", "v6.10.10.1"]


@pytest.mark.parametrize("current, policy, target", [
    ("v6.10.1", "patch", "v6.10.4"),
    ("v6.10.1", "minor", "v6.11.1"),
    ("v6.10.4", "patch", None),
    ("v6.11.1", "minor", None),
    ("v7.0.0", "minor", None),          # never a major change
    ("v6.10", "patch", None),         # a series tag has no patch to follow
    ("v6.10", "minor", None),           # "v6.11" is not published
    ("latest", "minor", None),
    ("v6.10.1", "inconnue", None),
])
def test_pick_series_target(current, policy, target):
    from main import pick_series_target
    assert pick_series_target(current, LYCHEE_TAGS, policy) == target


def test_pick_series_target_keeps_the_suffix_of_the_tag_in_service():
    from main import pick_series_target
    tags = ["11.8-noble", "11.9-noble", "11.9", "11.10-bookworm", "12.0-noble"]
    assert pick_series_target("11.8-noble", tags, "minor") == "11.9-noble"


def test_pick_series_target_orders_numerically():
    from main import pick_series_target
    assert pick_series_target("2.40.5", ["2.40.9", "2.40.10"], "patch") == "2.40.10"


LYCHEE_COMPOSE = """services:
  lychee:
    image: 'lycheeorg/lychee:v6.10.1'
    labels:
      - diun.watch_repo=true
      - 'diun-dispatcher.follow=patch'
    environment:
      FLAG: yes
      PORT: 8000
  lychee-db:
    image: lycheeorg/lychee:v6.10.1
    command: worker
  redis:
    image: "redis:7"
    labels:
      diun-dispatcher.follow: minor
volumes:
  data: {}
"""


def test_series_policies_reads_list_and_mapping_labels():
    from main import series_policies
    assert series_policies(LYCHEE_COMPOSE) == {
        "lychee": {"policy": "patch", "image": "lycheeorg/lychee:v6.10.1"},
        "redis": {"policy": "minor", "image": "redis:7"},
    }


def test_series_policies_survives_a_broken_compose():
    from main import series_policies
    assert series_policies("services: [unclosed") == {}
    assert series_policies("") == {}


def test_rewrite_image_line_changes_only_that_service():
    from main import rewrite_image_line, compose_matches
    new = rewrite_image_line(LYCHEE_COMPOSE, "lychee", "lycheeorg/lychee:v6.10.4")

    changed = [(a, b) for a, b in zip(LYCHEE_COMPOSE.splitlines(), new.splitlines()) if a != b]
    assert changed == [("    image: 'lycheeorg/lychee:v6.10.1'",
                        "    image: 'lycheeorg/lychee:v6.10.4'")]
    assert compose_matches(new, LYCHEE_COMPOSE, "lychee", "lycheeorg/lychee:v6.10.4")


def test_rewrite_image_line_handles_unquoted_and_double_quoted_images():
    from main import rewrite_image_line
    assert "    image: \"redis:7.4\"\n" in rewrite_image_line(LYCHEE_COMPOSE, "redis", "redis:7.4")
    assert "    image: lycheeorg/lychee:v6.10.4\n    command: worker" in \
        rewrite_image_line(LYCHEE_COMPOSE, "lychee-db", "lycheeorg/lychee:v6.10.4")


def test_rewrite_image_line_refuses_an_unknown_service():
    from main import rewrite_image_line
    assert rewrite_image_line(LYCHEE_COMPOSE, "nope", "x:1") is None


def test_compose_matches_ignores_the_quotes_coolify_adds():
    """Coolify re-dumps the compose on save and quotes image: (seen on server1)."""
    from main import compose_matches
    saved = LYCHEE_COMPOSE.replace("image: lycheeorg/lychee:v6.10.1", "image: 'lycheeorg/lychee:v6.10.1'") \
                          .replace("FLAG: yes", "FLAG: 'yes'").replace("'lycheeorg/lychee:v6.10.1'\n    labels",
                                                                        "lycheeorg/lychee:v6.10.4\n    labels")
    assert compose_matches(saved, LYCHEE_COMPOSE, "lychee", "lycheeorg/lychee:v6.10.4")


def test_compose_matches_detects_any_other_change():
    from main import compose_matches, rewrite_image_line
    new = rewrite_image_line(LYCHEE_COMPOSE, "lychee", "lycheeorg/lychee:v6.10.4")
    assert not compose_matches(new.replace("PORT: 8000", "PORT: 8001"),
                               LYCHEE_COMPOSE, "lychee", "lycheeorg/lychee:v6.10.4")
    assert not compose_matches(LYCHEE_COMPOSE, LYCHEE_COMPOSE, "lychee", "lycheeorg/lychee:v6.10.4")


@pytest.mark.parametrize("image, where", [
    ("lycheeorg/lychee:v6.10.1", ("registry-1.docker.io", "lycheeorg/lychee")),
    ("postgres:17", ("registry-1.docker.io", "library/postgres")),
    ("docker.io/library/postgres:17", ("registry-1.docker.io", "library/postgres")),
    ("ghcr.io/mealie-recipes/mealie:v3.21.0", ("ghcr.io", "mealie-recipes/mealie")),
])
def test_registry_repository(image, where):
    from main import registry_repository
    assert registry_repository(image) == where


def _registry_response(status, json_body=None, headers=None):
    import httpx
    request = httpx.Request("GET", "https://registry.test/v2/x/tags/list")
    return httpx.Response(status, json=json_body, headers=headers or {}, request=request)


@patch("main.httpx.AsyncClient")
def test_list_registry_tags_gets_an_anonymous_token_and_follows_pages(mock_client_cls):
    import asyncio
    import main
    challenge = {"WWW-Authenticate": 'Bearer realm="https://auth.docker.io/token",'
                                     'service="registry.docker.io",scope="repository:lycheeorg/lychee:pull"'}
    client_ = MagicMock()
    client_.get = AsyncMock(side_effect=[
        _registry_response(401, {}, challenge),
        _registry_response(200, {"token": "anon"}),
        _registry_response(200, {"tags": ["v6.10.1"]},
                           {"Link": '</v2/lycheeorg/lychee/tags/list?last=v6.10.1&n=1000>; rel="next"'}),
        _registry_response(200, {"tags": ["v6.10.4"]}),
    ])
    mock_client_cls.return_value.__aenter__.return_value = client_

    tags = asyncio.run(main.list_registry_tags("lycheeorg/lychee:v6.10.1"))

    assert tags == ["v6.10.1", "v6.10.4"]
    token_call = client_.get.call_args_list[1]
    assert token_call.args[0] == "https://auth.docker.io/token"
    assert token_call.kwargs["params"] == {"service": "registry.docker.io",
                                           "scope": "repository:lycheeorg/lychee:pull"}
    assert client_.get.call_args_list[2].kwargs["headers"] == {"Authorization": "Bearer anon"}
    assert client_.get.call_args_list[3].args[0] == \
        "https://registry-1.docker.io/v2/lycheeorg/lychee/tags/list?last=v6.10.1&n=1000"


@patch("main.httpx.AsyncClient")
def test_list_registry_tags_returns_none_on_failure(mock_client_cls):
    import asyncio
    import main
    client_ = MagicMock()
    client_.get = AsyncMock(side_effect=RuntimeError("boom"))
    mock_client_cls.return_value.__aenter__.return_value = client_
    assert asyncio.run(main.list_registry_tags("lycheeorg/lychee:v6.10.1")) is None


# --- applying an upgrade ---------------------------------------------------

SERIES_ENV = {
    "COOLIFY_API_URL": "http://coolify",
    "COOLIFY_TOKEN": "coolify-token",
    "AUTO_DEPLOY": "true",
    "DISPATCHER_URL": "http://dispatcher",
    "WEBHOOK_SECRET": "s3cret",
}

LYCHEE_SERVICE = {
    "uuid": "lychee-svc-uuid",
    "status": "running:healthy",
    "server": {"name": "server1"},
    "docker_compose_raw": LYCHEE_COMPOSE,
    "applications": [{"name": "lychee", "image": "lycheeorg/lychee:v6.10.1"}],
    "databases": [],
}


def _saved(raw):
    return {**LYCHEE_SERVICE, "docker_compose_raw": raw}


def _upgrade(get_service, patch_ok=True, trigger_ok=True, back=True):
    """Run upgrade_resource against mocked Coolify calls; return the mocks."""
    import asyncio
    import main
    mocks = {
        "get_service": AsyncMock(side_effect=get_service),
        "patch_compose": AsyncMock(return_value=patch_ok) if not isinstance(patch_ok, list)
        else AsyncMock(side_effect=patch_ok),
        "trigger_coolify": AsyncMock(side_effect=[{"ok": ok, "deployment_uuid": None} for ok in
                                                  (trigger_ok if isinstance(trigger_ok, list) else [trigger_ok, True])]),
        "wait_for_service": AsyncMock(return_value=(back, "running:healthy" if back else "exited", "")),
        "send_notification": MagicMock(),
    }
    main._series_last_attempt.clear()
    with patch.dict(os.environ, SERIES_ENV, clear=True), \
         patch.multiple("main", **mocks):
        outcome = asyncio.run(main.upgrade_resource(LYCHEE_SERVICE, "lychee", "v6.10.4"))
    return outcome, mocks


def test_upgrade_rewrites_the_compose_then_restarts_and_reports():
    from main import rewrite_image_line
    new_raw = rewrite_image_line(LYCHEE_COMPOSE, "lychee", "lycheeorg/lychee:v6.10.4")
    outcome, m = _upgrade([LYCHEE_SERVICE, _saved(new_raw.replace("'", ""))])

    assert outcome == "applied"
    m["patch_compose"].assert_awaited_once()
    args = m["patch_compose"].await_args.args
    assert args[1] == "coolify-token" and args[2] == "lychee-svc-uuid" and args[3] == new_raw
    assert m["trigger_coolify"].await_args.args[1] == "coolify-token"
    title = m["send_notification"].call_args.args[1]
    assert "✅" in title and "v6.10.1 → v6.10.4" in title


def test_upgrade_restores_the_compose_when_the_patch_fails():
    outcome, m = _upgrade([LYCHEE_SERVICE], patch_ok=[False, True])

    assert outcome == "failed"
    assert m["patch_compose"].await_args_list[-1].args[3] == LYCHEE_COMPOSE
    m["trigger_coolify"].assert_not_awaited()
    assert "❌" in m["send_notification"].call_args.args[1]


def test_upgrade_restores_the_compose_when_coolify_saved_something_else():
    from main import rewrite_image_line
    new_raw = rewrite_image_line(LYCHEE_COMPOSE, "lychee", "lycheeorg/lychee:v6.10.4")
    outcome, m = _upgrade([LYCHEE_SERVICE, _saved(new_raw.replace("PORT: 8000", "PORT: 1"))])

    assert outcome == "failed"
    assert m["patch_compose"].await_count == 2
    assert m["patch_compose"].await_args_list[-1].args[3] == LYCHEE_COMPOSE
    m["trigger_coolify"].assert_not_awaited()


def test_upgrade_restores_the_compose_when_the_restart_is_refused():
    from main import rewrite_image_line
    new_raw = rewrite_image_line(LYCHEE_COMPOSE, "lychee", "lycheeorg/lychee:v6.10.4")
    outcome, m = _upgrade([LYCHEE_SERVICE, _saved(new_raw)], trigger_ok=[False])

    assert outcome == "failed"
    assert m["patch_compose"].await_args_list[-1].args[3] == LYCHEE_COMPOSE
    m["wait_for_service"].assert_not_awaited()


def test_upgrade_restores_and_restarts_the_old_version_when_the_service_never_comes_back():
    from main import rewrite_image_line
    new_raw = rewrite_image_line(LYCHEE_COMPOSE, "lychee", "lycheeorg/lychee:v6.10.4")
    outcome, m = _upgrade([LYCHEE_SERVICE, _saved(new_raw)], back=False)

    assert outcome == "failed"
    assert m["patch_compose"].await_args_list[-1].args[3] == LYCHEE_COMPOSE
    assert m["trigger_coolify"].await_count == 2
    body = m["send_notification"].call_args.args[2]
    assert "restored" in body


def test_upgrade_never_touches_a_compose_it_cannot_read():
    """Without read:sensitive, Coolify hides docker_compose_raw."""
    outcome, m = _upgrade([{**LYCHEE_SERVICE, "docker_compose_raw": None}])
    assert outcome == "failed"
    m["patch_compose"].assert_not_awaited()


def test_upgrade_happens_at_most_once_a_day():
    import asyncio
    import main
    main._series_last_attempt.clear()
    main._series_last_attempt["lychee-svc-uuid/lychee"] = time.time() - 3600
    with patch.dict(os.environ, SERIES_ENV, clear=True), \
         patch("main.patch_compose", new=AsyncMock()) as patch_compose:
        outcome = asyncio.run(main.upgrade_resource(LYCHEE_SERVICE, "lychee", "v6.10.4"))
    main._series_last_attempt.clear()
    assert outcome == "too-soon"
    patch_compose.assert_not_awaited()


def test_upgrade_runs_once_at_a_time_per_resource():
    import asyncio
    import main

    async def scenario():
        main._series_last_attempt.clear()
        lock = main._series_lock("lychee-svc-uuid/lychee")
        async with lock:
            return await main.upgrade_resource(LYCHEE_SERVICE, "lychee", "v6.10.4")

    with patch.dict(os.environ, SERIES_ENV, clear=True):
        assert asyncio.run(scenario()) == "busy"


# --- the periodic check ----------------------------------------------------

def _run_check(services, env=SERIES_ENV, tags=LYCHEE_TAGS):
    import asyncio
    import main
    main._series_proposed.clear()
    with patch.dict(os.environ, env, clear=True), \
         patch("main.get_coolify_applications", new=AsyncMock(return_value=services)) as listing, \
         patch("main.list_registry_tags", new=AsyncMock(return_value=tags)) as registry, \
         patch("main.upgrade_resource", new=AsyncMock(return_value="applied")) as upgrade, \
         patch("main.send_notification") as notify:
        asyncio.run(main.run_series_check())
    return listing, registry, upgrade, notify


def test_series_check_only_looks_at_labelled_resources():
    plain = {**LYCHEE_SERVICE, "uuid": "plain", "docker_compose_raw": "services:\n  app:\n    image: nginx:1.27\n"}
    listing, registry, upgrade, notify = _run_check([LYCHEE_SERVICE, plain])

    assert listing.await_args.args[1] == "coolify-token"
    assert [c.args[0] for c in registry.await_args_list] == ["lycheeorg/lychee:v6.10.1", "redis:7"]
    upgrade.assert_awaited_once()
    assert upgrade.await_args.args[1:] == ("lychee", "v6.10.4")


def test_series_check_skips_ignored_containers():
    env = {**SERIES_ENV, "IGNORE_CONTAINERS": "other,lychee-lychee-svc-uuid,redis"}
    _, registry, upgrade, _ = _run_check([LYCHEE_SERVICE], env=env)
    registry.assert_not_awaited()
    upgrade.assert_not_awaited()


def test_series_check_without_auto_deploy_only_proposes_once():
    import asyncio
    import main
    env = {k: v for k, v in SERIES_ENV.items() if k != "AUTO_DEPLOY"}
    _, _, upgrade, notify = _run_check([LYCHEE_SERVICE], env=env)

    upgrade.assert_not_awaited()
    notify.assert_called_once()
    title, body = notify.call_args.args[1], notify.call_args.args[2]
    assert "v6.10.1 → v6.10.4" in title
    assert "/upgrade?uuid=lychee-s" in body and "service=lychee" in body and "tag=v6.10.4" in body

    # the next day, same proposal: silent
    with patch.dict(os.environ, env, clear=True), \
         patch("main.get_coolify_applications", new=AsyncMock(return_value=[LYCHEE_SERVICE])), \
         patch("main.list_registry_tags", new=AsyncMock(return_value=LYCHEE_TAGS)), \
         patch("main.send_notification") as notify_again:
        asyncio.run(main.run_series_check())
    notify_again.assert_not_called()


def test_series_check_does_nothing_without_a_newer_tag_in_policy():
    _, _, upgrade, notify = _run_check([LYCHEE_SERVICE], tags=["v6.10.1", "v7.0.0"])
    upgrade.assert_not_awaited()
    notify.assert_not_called()


def test_series_check_hour():
    from main import seconds_until_next_check
    from datetime import datetime
    assert seconds_until_next_check(datetime(2026, 9, 23, 4, 0, 0), 5) == 3600
    assert seconds_until_next_check(datetime(2026, 9, 23, 5, 0, 0), 5) == 24 * 3600
    assert seconds_until_next_check(datetime(2026, 9, 23, 6, 30, 0), 5) == 22.5 * 3600


# --- manual upgrade link ---------------------------------------------------

def test_upgrade_link_rejects_a_wrong_secret():
    with patch.dict(os.environ, SERIES_ENV, clear=True):
        resp = client.get("/upgrade?uuid=x&service=lychee&tag=v6.10.4&secret=nope")
    assert resp.status_code == 401


def test_upgrade_link_refuses_a_tag_outside_the_policy():
    with patch.dict(os.environ, SERIES_ENV, clear=True), \
         patch("main.get_service", new=AsyncMock(return_value=LYCHEE_SERVICE)), \
         patch("main.list_registry_tags", new=AsyncMock(return_value=LYCHEE_TAGS)), \
         patch("main.upgrade_resource", new=AsyncMock()) as upgrade:
        resp = client.get("/upgrade?uuid=lychee-svc-uuid&service=lychee&tag=v7.0.0&secret=s3cret")
    assert resp.status_code == 400
    upgrade.assert_not_awaited()


def test_upgrade_link_applies_a_tag_within_the_policy():
    with patch.dict(os.environ, SERIES_ENV, clear=True), \
         patch("main.get_service", new=AsyncMock(return_value=LYCHEE_SERVICE)), \
         patch("main.list_registry_tags", new=AsyncMock(return_value=LYCHEE_TAGS)), \
         patch("main.upgrade_resource", new=AsyncMock(return_value="applied")) as upgrade:
        resp = client.get("/upgrade?uuid=lychee-svc-uuid&service=lychee&tag=v6.10.4&secret=s3cret")
    assert resp.status_code == 200
    upgrade.assert_called_once()
    assert upgrade.await_args.args[1:] == ("lychee", "v6.10.4")


def test_upgrade_link_respects_the_daily_limit():
    import main
    main._series_last_attempt["lychee-svc-uuid/lychee"] = time.time()
    try:
        with patch.dict(os.environ, SERIES_ENV, clear=True), \
             patch("main.get_service", new=AsyncMock(return_value=LYCHEE_SERVICE)), \
             patch("main.list_registry_tags", new=AsyncMock(return_value=LYCHEE_TAGS)), \
             patch("main.upgrade_resource", new=AsyncMock()) as upgrade:
            resp = client.get("/upgrade?uuid=lychee-svc-uuid&service=lychee&tag=v6.10.4&secret=s3cret")
    finally:
        main._series_last_attempt.clear()
    assert "too-soon" in resp.text
    upgrade.assert_not_called()


# --- a Diun "new" event within the policy ----------------------------------

@patch('main.send_notification')
@patch('main.get_coolify_applications')
def test_new_event_within_the_policy_checks_the_series_instead_of_announcing(mock_coolify, mock_notify):
    mock_coolify.return_value = [LYCHEE_SERVICE]
    with patch.dict(os.environ, SERIES_ENV, clear=True), \
         patch("main.get_service", new=AsyncMock(return_value=LYCHEE_SERVICE)), \
         patch("main.check_series", new=AsyncMock()) as check:
        resp = client.post("/webhook", json={**DIUN_PAYLOAD, "status": "new",
                                             "image": "docker.io/lycheeorg/lychee:v6.10.4",
                                             "metadata": {"ctn_names": "lychee-lychee-svc-uuid"}},
                           headers={"X-Diun-Secret": "s3cret"})
    assert resp.json()["action"] == "series-check"
    mock_notify.assert_not_called()
    check.assert_called_once()


@patch('main.send_notification')
@patch('main.get_coolify_applications')
def test_new_event_beyond_the_policy_is_still_announced(mock_coolify, mock_notify):
    mock_coolify.return_value = [LYCHEE_SERVICE]
    with patch.dict(os.environ, SERIES_ENV, clear=True), \
         patch("main.get_service", new=AsyncMock(return_value=LYCHEE_SERVICE)), \
         patch("main.check_series", new=AsyncMock()) as check:
        resp = client.post("/webhook", json={**DIUN_PAYLOAD, "status": "new",
                                             "image": "docker.io/lycheeorg/lychee:v7.0.0",
                                             "metadata": {"ctn_names": "lychee-lychee-svc-uuid"}},
                           headers={"X-Diun-Secret": "s3cret"})
    assert resp.json()["action"] == "new-tag-notified"
    mock_notify.assert_called_once()
    check.assert_not_called()


# --- announce-only series ----------------------------------------------------

ANNOUNCED_LYCHEE = {**LYCHEE_SERVICE, "docker_compose_raw": LYCHEE_COMPOSE.replace(
    "diun-dispatcher.follow=patch", "diun-dispatcher.follow=announce")}


def test_announce_policy_follows_the_same_major():
    from main import pick_series_target
    assert pick_series_target("v6.10.1", ["v6.10.4", "v6.11.0", "v7.0.0"], "announce") == "v6.11.0"


def test_announce_policy_never_applies_even_with_auto_deploy():
    """The label says: tell me about my series, never touch it."""
    _, _, upgrade, notify = _run_check([ANNOUNCED_LYCHEE], tags=["v6.10.1", "v6.10.4", "v7.0.0"])

    upgrade.assert_not_awaited()
    notify.assert_called_once()
    title, body = notify.call_args.args[1], notify.call_args.args[2]
    assert "v6.10.1 → v6.10.4" in title
    assert "AUTO_DEPLOY" not in body, "AUTO_DEPLOY is on: the reason is the label"
    assert "/upgrade?uuid=lychee-s" in body and "tag=v6.10.4" in body


def test_announce_policy_announces_a_tag_once():
    import asyncio
    import main
    _run_check([ANNOUNCED_LYCHEE], tags=["v6.10.1", "v6.10.4"])
    with patch.dict(os.environ, SERIES_ENV, clear=True), \
         patch("main.get_coolify_applications", new=AsyncMock(return_value=[ANNOUNCED_LYCHEE])), \
         patch("main.list_registry_tags", new=AsyncMock(return_value=["v6.10.1", "v6.10.4"])), \
         patch("main.send_notification") as notify_again:
        asyncio.run(main.run_series_check())
    notify_again.assert_not_called()


def test_upgrade_link_applies_an_announced_tag():
    """The link in the announcement is the one-click way to apply it."""
    with patch.dict(os.environ, SERIES_ENV, clear=True), \
         patch("main.get_service", new=AsyncMock(return_value=ANNOUNCED_LYCHEE)), \
         patch("main.list_registry_tags", new=AsyncMock(return_value=LYCHEE_TAGS)), \
         patch("main.upgrade_resource", new=AsyncMock(return_value="applied")) as upgrade:
        resp = client.get("/upgrade?uuid=lychee-svc-uuid&service=lychee&tag=v6.10.4&secret=s3cret")
    assert resp.status_code == 200
    upgrade.assert_called_once()


def test_lifespan_loads_the_cache_starts_the_series_check_and_saves_on_exit():
    """Starlette 1.0 removed on_event: startup and shutdown go through the lifespan."""
    import main
    with patch("main._load_cache_from_disk") as load, \
         patch("main._save_cache_to_disk") as save, \
         patch("main.log_environment_config"), \
         patch("main.series_check_loop", new=AsyncMock()) as loop:
        with TestClient(app) as c:
            load.assert_called_once()
            save.assert_not_called()
            assert c.get("/health").status_code == 200
        save.assert_called_once()
    loop.assert_called_once()
    assert main._series_task is not None


@patch("main.get_service_status", new_callable=AsyncMock, return_value="running:healthy")
@patch('main.watch_deployment')
@patch('main.send_notification')
@patch('main.trigger_coolify')
@patch('main.get_coolify_applications')
def test_update_reaches_prod_when_staging_pulls_the_same_repository(mock_coolify, mock_trigger, mock_notify, mock_watch, mock_status):
    """2026-10-03: the prod :latest update matched the staging service first and
    was dropped as "another tag"; prod was never redeployed."""
    mock_coolify.return_value = STAGING_AND_PROD
    mock_trigger.return_value = {"ok": True, "deployment_uuid": None}
    event = {**DIUN_PAYLOAD, "image": "registry.example.com/namour/mmss-nginx:latest",
             "metadata": {**DIUN_PAYLOAD.get("metadata", {}), "ctn_names": "nginx-svc000000000000000000002"}}

    resp, = _post_webhooks([event])

    assert resp.json()["action"] == "auto-deploy-scheduled"
    assert resp.json()["uuid"] == "svc000000000000000000002"
    assert mock_trigger.call_args.args[2] == "svc000000000000000000002"


# --- base image rebuild: version and series ---------------------------------

import pytest


@pytest.mark.parametrize("version,series,expected", [
    ("v4.5.1-33486634677", "v4", True),
    ("v4.6.0", "v4", True),
    ("v5.0.0-beta3", "v4", False),
    ("v5.0.0", "v4", False),
    ("4.5.1", "v4", True),
    (None, "v4", False),
    ("", "v4", False),
    ("v4.5.1", "", False),
    ("v4.5.1", None, False),
    ("latest", "v4", False),
])
def test_base_series_matches(version, series, expected):
    from main import base_series_matches
    assert base_series_matches(version, series) is expected


def _manifest_response(body, status=200, headers=None):
    request = httpx.Request("GET", "https://registry.test/v2/x/manifests/y")
    return httpx.Response(status, json=body, headers=headers or {}, request=request)


INDEX = {"manifests": [
    {"digest": "sha256:arm", "platform": {"os": "linux", "architecture": "arm64"}},
    {"digest": "sha256:amd", "platform": {"os": "linux", "architecture": "amd64"}},
]}
MANIFEST = {"config": {"digest": "sha256:cfg"}}
CONFIG = {"config": {"Labels": {"org.opencontainers.image.version": "v4.5.1-33486634677"}}}


@patch("main.httpx.AsyncClient")
def test_fetch_image_version_follows_index_manifest_and_config(mock_client_cls):
    import asyncio
    import main
    challenge = {"WWW-Authenticate": 'Bearer realm="https://auth.docker.io/token",'
                                     'service="registry.docker.io",scope="repository:serversideup/php:pull"'}
    client_ = MagicMock()
    client_.get = AsyncMock(side_effect=[
        _manifest_response({}, 401, challenge),
        _manifest_response({"token": "anon"}),
        _manifest_response(INDEX),
        _manifest_response(MANIFEST),
        _manifest_response(CONFIG),
    ])
    mock_client_cls.return_value.__aenter__.return_value = client_

    version = asyncio.run(main.fetch_image_version(
        "docker.io/serversideup/php:8.4-fpm-nginx", "sha256:index"))

    assert version == "v4.5.1-33486634677"
    urls = [c.args[0] for c in client_.get.call_args_list]
    assert urls[2] == "https://registry-1.docker.io/v2/serversideup/php/manifests/sha256:index"
    assert urls[3] == "https://registry-1.docker.io/v2/serversideup/php/manifests/sha256:amd"
    assert urls[4] == "https://registry-1.docker.io/v2/serversideup/php/blobs/sha256:cfg"
    assert client_.get.call_args_list[2].kwargs["headers"]["Authorization"] == "Bearer anon"
    assert "manifest.list" in client_.get.call_args_list[2].kwargs["headers"]["Accept"]


@patch("main.httpx.AsyncClient")
def test_fetch_image_version_reads_a_single_manifest(mock_client_cls):
    import asyncio
    import main
    client_ = MagicMock()
    client_.get = AsyncMock(side_effect=[_manifest_response(MANIFEST), _manifest_response(CONFIG)])
    mock_client_cls.return_value.__aenter__.return_value = client_

    version = asyncio.run(main.fetch_image_version("serversideup/php:8.4-fpm-nginx"))

    assert version == "v4.5.1-33486634677"
    assert client_.get.call_args_list[0].args[0] == \
        "https://registry-1.docker.io/v2/serversideup/php/manifests/8.4-fpm-nginx"


@patch("main.httpx.AsyncClient")
def test_fetch_image_version_without_amd64_is_none(mock_client_cls):
    import asyncio
    import main
    client_ = MagicMock()
    client_.get = AsyncMock(side_effect=[_manifest_response({"manifests": [INDEX["manifests"][0]]})])
    mock_client_cls.return_value.__aenter__.return_value = client_
    assert asyncio.run(main.fetch_image_version("serversideup/php:8.4-fpm-nginx")) is None


@patch("main.httpx.AsyncClient")
def test_fetch_image_version_without_label_is_none(mock_client_cls):
    import asyncio
    import main
    client_ = MagicMock()
    client_.get = AsyncMock(side_effect=[_manifest_response(MANIFEST),
                                         _manifest_response({"config": {"Labels": None}})])
    mock_client_cls.return_value.__aenter__.return_value = client_
    assert asyncio.run(main.fetch_image_version("serversideup/php:8.4-fpm-nginx")) is None


@patch("main.httpx.AsyncClient")
def test_fetch_image_version_on_registry_failure_is_none(mock_client_cls):
    import asyncio
    import main
    client_ = MagicMock()
    client_.get = AsyncMock(side_effect=RuntimeError("boom"))
    mock_client_cls.return_value.__aenter__.return_value = client_
    assert asyncio.run(main.fetch_image_version("serversideup/php:8.4-fpm-nginx")) is None


# --- base image rebuild: deploying the applications ------------------------

def _coolify_response(body, status=200, method="POST", url="http://coolify/api/v1/deploy"):
    return httpx.Response(status, json=body, request=httpx.Request(method, url))


@patch("main.httpx.AsyncClient")
def test_deploy_application_posts_and_returns_the_deployment(mock_client_cls):
    import asyncio
    import main
    client_ = MagicMock()
    client_.post = AsyncMock(return_value=_coolify_response(
        {"deployments": [{"message": "queued", "resource_uuid": "app-1", "deployment_uuid": "dep-1"}]}))
    mock_client_cls.return_value.__aenter__.return_value = client_

    dep = asyncio.run(main.deploy_application("http://coolify/", "token", "app-1"))

    assert dep == "dep-1"
    call = client_.post.call_args
    assert call.args[0] == "http://coolify/api/v1/deploy"
    assert call.kwargs["params"] == {"uuid": "app-1"}
    assert call.kwargs["headers"]["Authorization"] == "Bearer token"


@patch("main.httpx.AsyncClient")
def test_deploy_application_refused_is_none(mock_client_cls):
    import asyncio
    import main
    client_ = MagicMock()
    client_.post = AsyncMock(return_value=_coolify_response({"message": "Unauthenticated."}, 401))
    mock_client_cls.return_value.__aenter__.return_value = client_
    assert asyncio.run(main.deploy_application("http://coolify", "token", "app-1")) is None


def _wait(statuses, timeout=1800.0):
    """Run wait_for_application_deployment against a scripted list of statuses."""
    import asyncio
    import main
    clock = {"now": 1000.0}

    async def fake_sleep(seconds):
        clock["now"] += seconds

    status = AsyncMock(side_effect=statuses)
    with patch("main.get_deployment_status", new=status), \
         patch("main.asyncio.sleep", new=fake_sleep), \
         patch("main.time.time", new=lambda: clock["now"]):
        outcome = asyncio.run(main.wait_for_application_deployment(
            "http://coolify", "token", "dep-1", timeout=timeout))
    return outcome, status


def test_wait_for_application_deployment_ends_on_finished():
    outcome, status = _wait(["queued", "in_progress", "in_progress", "finished"])
    assert outcome == "finished"
    assert status.await_count == 4


def test_wait_for_application_deployment_ends_on_failed():
    outcome, _ = _wait(["in_progress", "failed"])
    assert outcome == "failed"


def test_wait_for_application_deployment_survives_unreachable_coolify():
    outcome, _ = _wait([None, None, "in_progress", "finished"])
    assert outcome == "finished"


def test_wait_for_application_deployment_times_out():
    outcome, _ = _wait(["in_progress"] * 1000, timeout=120.0)
    assert outcome == "timeout"


def _rebuild(outcomes, deploys=None, names=None):
    """Run rebuild_applications with scripted deploy results and outcomes."""
    import asyncio
    import main
    uuids = ["stg", "acc", "prod"]
    mocks = {
        "deploy_application": AsyncMock(side_effect=deploys or [f"dep-{u}" for u in uuids]),
        "wait_for_application_deployment": AsyncMock(side_effect=outcomes),
        "get_application_names": AsyncMock(return_value=names if names is not None else
                                           {"stg": "roadbook-staging", "acc": "roadbook-acc",
                                            "prod": "roadbook-prod"}),
        "send_notification": MagicMock(),
        "load_apprise_urls": MagicMock(return_value=["json://x"]),
    }
    with patch.multiple("main", **mocks):
        results = asyncio.run(main.rebuild_applications(
            "http://coolify", "token", "docker.io/serversideup/php:8.4-fpm-nginx",
            "v4.5.2-1", uuids))
    return results, mocks


def test_rebuild_applications_deploys_in_order_and_reports_success():
    results, m = _rebuild(["finished", "finished", "finished"])

    assert [r[:2] for r in results] == [("stg", "finished"), ("acc", "finished"), ("prod", "finished")]
    assert [c.args[2] for c in m["deploy_application"].await_args_list] == ["stg", "acc", "prod"]
    m["send_notification"].assert_called_once()
    title, body = m["send_notification"].call_args.args[1], m["send_notification"].call_args.args[2]
    assert "✅" in title and "v4.5.2-1" in title
    assert "roadbook-prod: finished" in body


def test_rebuild_applications_stops_at_the_first_failure():
    results, m = _rebuild(["finished", "failed"])

    assert [r[:2] for r in results] == [("stg", "finished"), ("acc", "failed"), ("prod", "not deployed")]
    assert m["deploy_application"].await_count == 2
    title, body = m["send_notification"].call_args.args[1], m["send_notification"].call_args.args[2]
    assert "❌" in title and "roadbook-acc" in title
    assert "dep-acc" in body
    assert "roadbook-prod: not deployed" in body


def test_rebuild_applications_stops_when_coolify_refuses_a_deploy():
    results, m = _rebuild(["finished"], deploys=["dep-stg", None])

    assert [r[:2] for r in results] == [("stg", "finished"), ("acc", "refused"), ("prod", "not deployed")]
    assert m["wait_for_application_deployment"].await_count == 1


def test_rebuild_applications_names_unknown_uuids_by_uuid():
    _, m = _rebuild(["finished", "finished", "finished"], names={})
    assert "stg: finished" in m["send_notification"].call_args.args[2]


def test_rebuild_applications_runs_once_per_base():
    import asyncio
    import main

    async def scenario():
        lock = main.base_rebuild_lock("serversideup/php:8.4-fpm-nginx")
        async with lock:
            return await main.rebuild_applications(
                "http://coolify", "token", "docker.io/serversideup/php:8.4-fpm-nginx",
                "v4.5.2-1", ["stg"])

    deploy = AsyncMock()
    with patch("main.deploy_application", new=deploy):
        assert asyncio.run(scenario()) == []
    deploy.assert_not_awaited()


# --- base image rebuild: webhook routing -----------------------------------

BASE_ENV = {
    "COOLIFY_API_URL": "http://coolify",
    "COOLIFY_TOKEN": "token",
    "AUTO_DEPLOY": "true",
    "WEBHOOK_SECRET": "s3cret",
    "APPRISE_URLS": "json://x",
}


def _base_payload(status="update", rebuild="stg, acc,prod,", series="v4"):
    return {
        "hostname": "server2", "status": status, "provider": "file",
        "image": "docker.io/serversideup/php:8.4-fpm-nginx",
        "digest": "sha256:index", "platform": "linux/amd64",
        "metadata": {"rebuild": rebuild, "rebuild_series": series},
    }


def _post_base(payload, env=BASE_ENV, version="v4.5.2-1"):
    mocks = {
        "fetch_image_version": AsyncMock(return_value=version),
        "rebuild_applications": AsyncMock(return_value=[]),
        "send_notification": MagicMock(),
        "get_coolify_applications": AsyncMock(side_effect=AssertionError("no service matching")),
    }
    with patch.dict(os.environ, env, clear=True), patch.multiple("main", **mocks):
        resp = client.post("/webhook", json=payload, headers={"X-Diun-Secret": "s3cret"})
    return resp, mocks


@pytest.mark.parametrize("value,expected", [
    ("stg, acc,prod,", ["stg", "acc", "prod"]),
    ("", []),
    (None, []),
    (" , ", []),
    ("only", ["only"]),
])
def test_parse_rebuild_list(value, expected):
    from main import parse_rebuild_list
    assert parse_rebuild_list(value) == expected


def test_base_update_in_series_rebuilds_in_order():
    resp, m = _post_base(_base_payload())

    assert resp.json()["action"] == "base-rebuild"
    m["fetch_image_version"].assert_awaited_once_with(
        "docker.io/serversideup/php:8.4-fpm-nginx", "sha256:index")
    m["rebuild_applications"].assert_called_once()
    args = m["rebuild_applications"].call_args.args
    assert args[1:] == ("token", "docker.io/serversideup/php:8.4-fpm-nginx", "v4.5.2-1",
                        ["stg", "acc", "prod"])
    m["send_notification"].assert_not_called()


def test_base_new_is_ignored():
    resp, m = _post_base(_base_payload(status="new"))

    assert resp.json()["action"] == "base-ignored"
    m["fetch_image_version"].assert_not_awaited()
    m["rebuild_applications"].assert_not_called()


def test_base_in_another_major_is_only_announced():
    resp, m = _post_base(_base_payload(), version="v5.0.0")

    assert resp.json()["action"] == "base-announced"
    m["rebuild_applications"].assert_not_called()
    title, body = m["send_notification"].call_args.args[1], m["send_notification"].call_args.args[2]
    assert "v5.0.0" in title and "v4" in title
    assert "3 application(s)" in body


def test_base_with_unreadable_version_is_only_announced():
    resp, m = _post_base(_base_payload(), version=None)

    assert resp.json()["action"] == "base-announced"
    m["rebuild_applications"].assert_not_called()
    assert "unknown" in m["send_notification"].call_args.args[1]


def test_base_without_auto_deploy_only_notifies():
    env = {k: v for k, v in BASE_ENV.items() if k != "AUTO_DEPLOY"}
    resp, m = _post_base(_base_payload(), env=env)

    assert resp.json()["action"] == "base-notified"
    m["rebuild_applications"].assert_not_called()
    assert "stg" in m["send_notification"].call_args.args[2]


def test_base_rebuild_already_running_is_dropped():
    import main
    lock = main.base_rebuild_lock("serversideup/php:8.4-fpm-nginx")
    with patch.object(lock, "locked", return_value=True):
        resp, m = _post_base(_base_payload())

    assert resp.json()["action"] == "base-busy"
    m["rebuild_applications"].assert_not_called()


def test_event_without_rebuild_metadata_keeps_the_service_path():
    payload = _base_payload(rebuild="")
    payload["metadata"]["ctn_names"] = "nextcloud"
    mocks = {"get_coolify_applications": AsyncMock(return_value=[]),
             "send_notification": MagicMock(),
             "fetch_image_version": AsyncMock(side_effect=AssertionError("not a base event"))}
    with patch.dict(os.environ, BASE_ENV, clear=True), patch.multiple("main", **mocks):
        resp = client.post("/webhook", json=payload, headers={"X-Diun-Secret": "s3cret"})

    assert resp.status_code == 200
    mocks["get_coolify_applications"].assert_awaited()


# --- base image rebuild: review fixes ---------------------------------------

def test_rebuild_applications_reports_an_unexpected_error():
    results, m = None, None
    import asyncio
    import main
    mocks = {
        "deploy_application": AsyncMock(side_effect=RuntimeError("boom")),
        "get_application_names": AsyncMock(return_value={"stg": "roadbook-staging"}),
        "send_notification": MagicMock(),
        "load_apprise_urls": MagicMock(return_value=["json://x"]),
    }
    with patch.multiple("main", **mocks):
        with pytest.raises(RuntimeError):
            asyncio.run(main.rebuild_applications(
                "http://coolify", "token", "docker.io/serversideup/php:8.4-fpm-nginx",
                "v4.5.2-1", ["stg", "acc"]))
    mocks["send_notification"].assert_called_once()
    title, body = mocks["send_notification"].call_args.args[1], mocks["send_notification"].call_args.args[2]
    assert "❌" in title and "interrupted" in title
    assert "boom" in body and "acc" in body


def test_base_rebuild_lock_is_per_tag():
    import main
    assert main.base_rebuild_lock("docker.io/serversideup/php:8.4-fpm-nginx") is \
        main.base_rebuild_lock("serversideup/php:8.4-fpm-nginx")
    assert main.base_rebuild_lock("serversideup/php:8.4-fpm-nginx") is not \
        main.base_rebuild_lock("serversideup/php:8.4-cli")


def test_base_event_refused_without_webhook_secret():
    env = {k: v for k, v in BASE_ENV.items() if k != "WEBHOOK_SECRET"}
    resp, m = _post_base(_base_payload(), env=env)

    assert resp.json()["action"] == "base-refused"
    m["fetch_image_version"].assert_not_awaited()
    m["rebuild_applications"].assert_not_called()


# ---------------------------------------------------------------------------
# One restart at a time per Coolify service
#
# 2026-10-05: two images of the same service were updated in the same Diun
# pass; two webhooks restarted the service twice at once, and the two
# overlapping "compose up" left two database containers on one data directory.
# ---------------------------------------------------------------------------

NO_DEBOUNCE_ENV = {**AUTO_DEPLOY_ENV, "DEPLOY_DEBOUNCE_SECONDS": "0"}


def _reset_redeploy_state():
    import main
    main._pending_redeploys.clear()
    main._service_restart_locks.clear()
    main._last_restart_request.clear()


def _post_webhooks(events, env=NO_DEBOUNCE_ENV):
    """Post the events, then run the background redeploys they scheduled, together."""
    import asyncio
    _reset_redeploy_state()
    spawned = []
    with patch.dict(os.environ, env, clear=True), \
         patch("main.spawn", new=lambda coro: spawned.append(coro)):
        responses = [client.post("/webhook", json=e, headers={"X-Diun-Secret": "s3cret"})
                     for e in events]

        async def run_all():
            await asyncio.gather(*spawned)

        asyncio.run(run_all())
    return responses


def _event(image, container):
    return {**DIUN_PAYLOAD, "image": image, "metadata": {"ctn_names": container}}


TWO_IMAGE_SERVICE = {
    "uuid": "svc-2",
    "status": "running:healthy",
    "server": {"name": "server1"},
    "applications": [
        {"name": "nginx", "image": "registry.example.com/team/app-nginx:latest"},
        {"name": "php", "image": "registry.example.com/team/app-php:latest"},
    ],
    "databases": [{"name": "db", "image": "postgres:18-alpine"}],
}

NGINX_EVENT = _event("registry.example.com/team/app-nginx:latest", "nginx-svc-2")
PHP_EVENT = _event("registry.example.com/team/app-php:latest", "php-svc-2")


def test_service_restart_lock_is_per_service():
    import main
    _reset_redeploy_state()
    assert main.service_restart_lock("svc-1") is main.service_restart_lock("svc-1")
    assert main.service_restart_lock("svc-1") is not main.service_restart_lock("svc-2")


@pytest.mark.parametrize("value, expected", [
    (None, 10.0), ("0", 0.0), ("2.5", 2.5), ("-1", 10.0), ("soon", 10.0),
])
def test_deploy_debounce_seconds(value, expected):
    import main
    env = {} if value is None else {"DEPLOY_DEBOUNCE_SECONDS": value}
    with patch.dict(os.environ, env, clear=True):
        assert main.deploy_debounce_seconds() == expected


@patch("main.get_service_status", new_callable=AsyncMock, return_value="running:healthy")
@patch("main.watch_deployment", new_callable=AsyncMock)
@patch("main.send_notification")
@patch("main.trigger_coolify", new_callable=AsyncMock)
@patch("main.get_coolify_applications", new_callable=AsyncMock)
def test_two_images_of_one_service_restart_it_once(mock_coolify, mock_trigger, mock_notify,
                                                   mock_watch, mock_status):
    mock_coolify.return_value = [TWO_IMAGE_SERVICE]
    mock_trigger.return_value = {"ok": True, "deployment_uuid": None}

    first, second = _post_webhooks([NGINX_EVENT, PHP_EVENT])

    assert first.json()["action"] == "auto-deploy-scheduled"
    assert second.json()["action"] == "auto-deploy-merged"
    mock_trigger.assert_awaited_once()
    assert mock_trigger.call_args.args[2] == "svc-2"
    mock_watch.assert_awaited_once()
    kwargs = mock_watch.call_args.kwargs
    assert "nginx-svc-2" in kwargs["container_name"] and "php-svc-2" in kwargs["container_name"]
    assert "app-nginx" in kwargs["image"] and "app-php" in kwargs["image"]


@patch("main.get_service_status", new_callable=AsyncMock, return_value="running:healthy")
@patch("main.watch_deployment", new_callable=AsyncMock)
@patch("main.send_notification")
@patch("main.trigger_coolify", new_callable=AsyncMock)
@patch("main.get_coolify_applications", new_callable=AsyncMock)
def test_two_services_restart_each(mock_coolify, mock_trigger, mock_notify, mock_watch, mock_status):
    mock_coolify.return_value = [MATCHING_SERVICE, TWO_IMAGE_SERVICE]
    mock_trigger.return_value = {"ok": True, "deployment_uuid": None}

    responses = _post_webhooks([DIUN_PAYLOAD, NGINX_EVENT])

    assert [r.json()["action"] for r in responses] == ["auto-deploy-scheduled"] * 2
    assert sorted(c.args[2] for c in mock_trigger.call_args_list) == ["svc-1", "svc-2"]


def test_update_during_a_restart_restarts_again_afterwards_never_at_once():
    """An image published after the first restart pulled must still be deployed,
    by a second restart that starts only once the first one is over."""
    import asyncio
    import main
    _reset_redeploy_state()
    timeline = []

    async def trigger(url, token, uuid):
        timeline.append(("trigger", uuid))
        return {"ok": True, "deployment_uuid": None}

    async def watch(url, token, uuid, baseline, **kwargs):
        timeline.append(("watch-start", kwargs["container_name"]))
        if len(timeline) == 2:
            # A webhook arrives while the first restart is being watched
            main.schedule_service_redeploy("http://coolify", "token", "svc-2", "running:healthy",
                                           "php-svc-2", "app-php:latest", "server1", "")
        await asyncio.sleep(0.05)
        timeline.append(("watch-end", kwargs["container_name"]))

    async def scenario():
        spawned = []
        with patch("main.spawn", new=lambda coro: spawned.append(asyncio.ensure_future(coro))):
            main.schedule_service_redeploy("http://coolify", "token", "svc-2", "running:healthy",
                                           "nginx-svc-2", "app-nginx:latest", "server1", "")
            while any(not t.done() for t in spawned):
                await asyncio.gather(*spawned)

    with patch.dict(os.environ, {"DEPLOY_DEBOUNCE_SECONDS": "0"}, clear=True), \
         patch("main.trigger_coolify", new=trigger), \
         patch("main.watch_deployment", new=watch), \
         patch("main.get_service_status", new=AsyncMock(return_value="running:healthy")):
        asyncio.run(scenario())

    assert timeline == [
        ("trigger", "svc-2"), ("watch-start", "nginx-svc-2"), ("watch-end", "nginx-svc-2"),
        ("trigger", "svc-2"), ("watch-start", "php-svc-2"), ("watch-end", "php-svc-2"),
    ]


@patch("main.get_service_status", new_callable=AsyncMock, return_value="running:healthy")
@patch("main.watch_deployment", new_callable=AsyncMock)
@patch("main.send_notification")
@patch("main.trigger_coolify", new_callable=AsyncMock)
@patch("main.get_coolify_applications", new_callable=AsyncMock)
def test_redeploy_rereads_the_baseline_once_it_holds_the_lock(mock_coolify, mock_trigger,
                                                             mock_notify, mock_watch, mock_status):
    """The listing seen by the webhook may be from the middle of another restart."""
    mock_coolify.return_value = [{**TWO_IMAGE_SERVICE, "status": "exited"}]
    mock_trigger.return_value = {"ok": True, "deployment_uuid": None}

    _post_webhooks([NGINX_EVENT])

    assert mock_watch.call_args.args[3] == "running:healthy"


@patch("main.get_service_status", new_callable=AsyncMock, return_value=None)
@patch("main.watch_deployment", new_callable=AsyncMock)
@patch("main.send_notification")
@patch("main.trigger_coolify", new_callable=AsyncMock)
@patch("main.get_coolify_applications", new_callable=AsyncMock)
def test_redeploy_keeps_the_listing_baseline_when_coolify_is_unreachable(
        mock_coolify, mock_trigger, mock_notify, mock_watch, mock_status):
    mock_coolify.return_value = [TWO_IMAGE_SERVICE]
    mock_trigger.return_value = {"ok": True, "deployment_uuid": None}

    _post_webhooks([NGINX_EVENT])

    assert mock_watch.call_args.args[3] == "running:healthy"


@patch("main.get_service_status", new_callable=AsyncMock, return_value="running:healthy")
@patch("main.watch_deployment", new_callable=AsyncMock)
@patch("main.send_notification")
@patch("main.trigger_coolify", new_callable=AsyncMock)
@patch("main.get_coolify_applications", new_callable=AsyncMock)
def test_merged_redeploy_that_cannot_be_triggered_is_notified_once(mock_coolify, mock_trigger,
                                                                  mock_notify, mock_watch, mock_status):
    mock_coolify.return_value = [TWO_IMAGE_SERVICE]
    mock_trigger.return_value = {"ok": False, "deployment_uuid": None}

    _post_webhooks([NGINX_EVENT, PHP_EVENT])

    mock_watch.assert_not_awaited()
    mock_notify.assert_called_once()
    title, body = mock_notify.call_args.args[1], mock_notify.call_args.args[2]
    assert "❌" in title and "nginx-svc-2" in title and "php-svc-2" in title
    assert "/deploy?uuid=" in body


def test_series_upgrade_waits_for_a_running_restart():
    import asyncio
    import main
    _reset_redeploy_state()
    main._series_last_attempt.clear()
    order = []

    async def apply(service, service_name, target_tag):
        order.append("upgrade")
        return "applied"

    async def scenario():
        lock = main.service_restart_lock("lychee-svc-uuid")
        await lock.acquire()
        upgrade = asyncio.ensure_future(main.upgrade_resource(LYCHEE_SERVICE, "lychee", "v6.10.4"))
        await asyncio.sleep(0.01)
        order.append("restart-over")
        lock.release()
        return await upgrade

    with patch.dict(os.environ, SERIES_ENV, clear=True), \
         patch("main._apply_series_upgrade", new=apply):
        assert asyncio.run(scenario()) == "applied"
    assert order == ["restart-over", "upgrade"]


@patch("main.trigger_coolify", new_callable=AsyncMock)
@patch("main.get_coolify_applications", new_callable=AsyncMock)
def test_manual_deploy_refuses_while_the_service_restarts(mock_coolify, mock_trigger):
    import asyncio
    import main
    _reset_redeploy_state()
    full_uuid = "svc000000000000000000001"
    mock_coolify.return_value = [{**MATCHING_SERVICE, "uuid": full_uuid}]
    lock = main.service_restart_lock(full_uuid)
    asyncio.run(lock.acquire())
    try:
        with patch.dict(os.environ, AUTO_DEPLOY_ENV, clear=True):
            resp = client.get("/deploy", params={"uuid": full_uuid, "secret": "s3cret"})
    finally:
        lock.release()

    mock_trigger.assert_not_awaited()
    assert "already running" in resp.text


@patch("main.trigger_coolify", new_callable=AsyncMock)
@patch("main.get_coolify_applications", new_callable=AsyncMock)
def test_manual_deploy_holds_the_lock_until_the_service_is_back(mock_coolify, mock_trigger):
    import asyncio
    import main
    _reset_redeploy_state()
    full_uuid = "svc000000000000000000001"
    mock_coolify.return_value = [{**MATCHING_SERVICE, "uuid": full_uuid}]
    mock_trigger.return_value = {"ok": True, "deployment_uuid": None}
    spawned = []
    seen = {}

    async def wait(url, token, uuid, baseline, container_name, **kwargs):
        seen["locked"] = main.service_restart_lock(uuid).locked()
        seen["baseline"] = baseline
        return True, baseline, ""

    with patch.dict(os.environ, AUTO_DEPLOY_ENV, clear=True), \
         patch("main.spawn", new=lambda coro: spawned.append(coro)), \
         patch("main.wait_for_service", new=wait):
        resp = client.get("/deploy", params={"uuid": full_uuid, "secret": "s3cret"})
        assert main.service_restart_lock(full_uuid).locked()

        async def run_all():
            await asyncio.gather(*spawned)

        asyncio.run(run_all())

    assert "Success" in resp.text
    assert seen == {"locked": True, "baseline": "running:healthy"}
    assert not main.service_restart_lock(full_uuid).locked()


@patch("main.trigger_coolify", new_callable=AsyncMock)
@patch("main.get_coolify_applications", new_callable=AsyncMock)
def test_manual_deploy_releases_the_lock_when_coolify_refuses(mock_coolify, mock_trigger):
    import main
    _reset_redeploy_state()
    full_uuid = "svc000000000000000000001"
    mock_coolify.return_value = [{**MATCHING_SERVICE, "uuid": full_uuid}]
    mock_trigger.return_value = {"ok": False, "deployment_uuid": None}

    with patch.dict(os.environ, AUTO_DEPLOY_ENV, clear=True):
        resp = client.get("/deploy", params={"uuid": full_uuid, "secret": "s3cret"})

    assert "Failed" in resp.text
    assert not main.service_restart_lock(full_uuid).locked()


def test_hold_after_restart_waits_out_the_minimum_since_the_request():
    """The watch can call a service back while Coolify is still pulling its images."""
    import asyncio
    import main
    _reset_redeploy_state()
    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    main._last_restart_request["svc-2"] = time.time() - 60
    with patch("main.asyncio.sleep", new=fake_sleep):
        asyncio.run(main.hold_after_restart("svc-2"))
        asyncio.run(main.hold_after_restart("never-restarted"))

    assert len(slept) == 1
    assert main.RESTART_MIN_HOLD_SECONDS - 61 < slept[0] <= main.RESTART_MIN_HOLD_SECONDS - 60


def test_trigger_records_the_request_even_when_it_fails():
    import asyncio
    import main
    _reset_redeploy_state()
    failing = MagicMock()
    failing.__aenter__ = AsyncMock(side_effect=httpx.ReadTimeout("slow"))
    failing.__aexit__ = AsyncMock(return_value=False)

    with patch("main.httpx.AsyncClient", return_value=failing):
        result = asyncio.run(main.trigger_coolify("http://coolify", "tok", "svc-2"))

    assert result["ok"] is False
    assert time.time() - main._last_restart_request["svc-2"] < 5


def test_restart_lock_is_held_after_a_quick_watch():
    """A second restart must not start right after a watch that saw nothing."""
    import asyncio
    import main
    _reset_redeploy_state()
    order = []

    async def trigger(url, token, uuid):
        main._last_restart_request[uuid] = time.time()
        order.append("trigger")
        return {"ok": True, "deployment_uuid": None}

    async def watch(*args, **kwargs):
        order.append("watched")

    async def hold(uuid):
        order.append("hold")

    async def scenario():
        spawned = []
        with patch("main.spawn", new=lambda coro: spawned.append(asyncio.ensure_future(coro))):
            main.schedule_service_redeploy("http://coolify", "token", "svc-2", "running:healthy",
                                           "nginx-svc-2", "app-nginx:latest", "server1", "")
            await asyncio.gather(*spawned)

    with patch.dict(os.environ, {"DEPLOY_DEBOUNCE_SECONDS": "0"}, clear=True), \
         patch("main.trigger_coolify", new=trigger), \
         patch("main.watch_deployment", new=watch), \
         patch("main.hold_after_restart", new=hold), \
         patch("main.get_service_status", new=AsyncMock(return_value="running:healthy")):
        asyncio.run(scenario())

    assert order == ["trigger", "watched", "hold"]


def test_series_upgrade_holds_the_lock_after_its_rollback():
    import asyncio
    import main
    _reset_redeploy_state()
    main._series_last_attempt.clear()
    order = []

    async def apply(service, service_name, target_tag):
        order.append("rollback-triggered")
        return "failed"

    async def hold(uuid):
        assert main.service_restart_lock(uuid).locked()
        order.append("hold")

    with patch.dict(os.environ, SERIES_ENV, clear=True), \
         patch("main._apply_series_upgrade", new=apply), \
         patch("main.hold_after_restart", new=hold):
        assert asyncio.run(main.upgrade_resource(LYCHEE_SERVICE, "lychee", "v6.10.4")) == "failed"
    assert order == ["rollback-triggered", "hold"]


def test_updates_during_the_debounce_join_the_same_restart():
    """With real timing: a webhook arriving while the first one waits is merged."""
    import asyncio
    import main
    _reset_redeploy_state()
    triggers = []

    async def trigger(url, token, uuid):
        triggers.append(uuid)
        return {"ok": True, "deployment_uuid": None}

    async def scenario():
        spawned = []
        with patch("main.spawn", new=lambda coro: spawned.append(asyncio.ensure_future(coro))):
            first = main.schedule_service_redeploy("http://coolify", "token", "svc-2", "running:healthy",
                                                   "nginx-svc-2", "app-nginx:latest", "server1", "")
            await asyncio.sleep(0.02)
            second = main.schedule_service_redeploy("http://coolify", "token", "svc-2", "running:healthy",
                                                    "php-svc-2", "app-php:latest", "server1", "")
            await asyncio.gather(*spawned)
        return first, second

    with patch.dict(os.environ, {"DEPLOY_DEBOUNCE_SECONDS": "0.1"}, clear=True), \
         patch("main.trigger_coolify", new=trigger), \
         patch("main.watch_deployment", new=AsyncMock()), \
         patch("main.get_service_status", new=AsyncMock(return_value="running:healthy")):
        assert asyncio.run(scenario()) == ("auto-deploy-scheduled", "auto-deploy-merged")
    assert triggers == ["svc-2"]


@patch("main.trigger_coolify", new_callable=AsyncMock)
@patch("main.get_coolify_applications", new_callable=AsyncMock)
def test_manual_deploy_refuses_while_an_automatic_redeploy_waits(mock_coolify, mock_trigger):
    """Never queue behind it: the request would hang for the whole restart."""
    import main
    _reset_redeploy_state()
    full_uuid = "svc000000000000000000001"
    mock_coolify.return_value = [{**MATCHING_SERVICE, "uuid": full_uuid}]
    main._pending_redeploys[full_uuid] = {"containers": ["nextcloud"], "images": []}

    with patch.dict(os.environ, AUTO_DEPLOY_ENV, clear=True):
        resp = client.get("/deploy", params={"uuid": full_uuid, "secret": "s3cret"})

    mock_trigger.assert_not_awaited()
    assert "already running" in resp.text
