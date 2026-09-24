import os
import json
import time
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
    ("git.helpcomputer.eu/lgnap/utick-sapiti-filling:latest",
     "git.helpcomputer.eu/lgnap/utick-sapiti-filling:latest"),
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
    """Regression: nextcloud-grawie's postgres was reported as not deployable."""
    services = [
        {
            "uuid": "svc-nextcloud",
            "server": {"name": "grawie"},
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
            "server": {"name": "grawie-prod"},
            "applications": [{"name": "nextcloud", "image": "nextcloud:34-apache"}],
            "databases": [],
        }
    ]
    service = find_service_by_image(services, "docker.io/library/nextcloud:34-apache")
    assert service is not None
    assert service["uuid"] == "svc-1"
    assert service["server"]["name"] == "grawie-prod"


@patch('main.send_notification')
@patch('main.get_coolify_applications')
def test_webhook_uses_coolify_server_name(mock_coolify, mock_notify):
    """A matched image is labelled with Coolify's server name, not Diun's hostname."""
    mock_coolify.return_value = [
        {
            "uuid": "svc-1",
            "server": {"name": "grawie-prod"},
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
        assert "Server: grawie-prod" in body
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
            "server": {"name": "grawie-prod"},
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
        assert "grawie-prod" not in body


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
    "server": {"name": "grawie-prod"},
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


@patch('main.watch_deployment')
@patch('main.send_notification')
@patch('main.trigger_coolify')
@patch('main.get_coolify_applications')
def test_webhook_triggers_deploy_when_auto_deploy_enabled(mock_coolify, mock_trigger, mock_notify, mock_watch):
    """With AUTO_DEPLOY on, a matched image is redeployed without waiting for a click."""
    mock_coolify.return_value = [MATCHING_SERVICE]
    mock_trigger.return_value = {"ok": True, "deployment_uuid": "dep-1"}

    with patch.dict(os.environ, AUTO_DEPLOY_ENV, clear=True):
        resp = client.post("/webhook", json=DIUN_PAYLOAD,
                           headers={"X-Diun-Secret": "s3cret"})

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


@patch('main.send_notification')
@patch('main.trigger_coolify')
@patch('main.get_coolify_applications')
def test_webhook_notifies_with_manual_link_when_trigger_fails(mock_coolify, mock_trigger, mock_notify):
    """If Coolify refuses the deploy, the user is told and gets the manual link."""
    mock_coolify.return_value = [MATCHING_SERVICE]
    mock_trigger.return_value = {"ok": False, "deployment_uuid": None}

    with patch.dict(os.environ, AUTO_DEPLOY_ENV, clear=True):
        client.post("/webhook", json=DIUN_PAYLOAD, headers={"X-Diun-Secret": "s3cret"})

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
    """Run watch_deployment against a canned sequence of Coolify statuses."""
    import asyncio
    import main
    with patch('main.get_service_status', new=AsyncMock(side_effect=statuses)) as status, \
         patch('main.asyncio.sleep', new=AsyncMock()):
        asyncio.run(main.watch_deployment(
            "http://coolify", "tok", "svc-1", baseline,
            container_name="meshmonitor", image="ghcr.io/yeraze/meshmonitor:latest",
            server="grawie-prod", **kwargs))
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

    assert status.await_count == 3
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


@patch('main.watch_deployment')
@patch('main.send_notification')
@patch('main.trigger_coolify')
@patch('main.get_coolify_applications')
def test_auto_deploy_watches_the_service_it_restarted(mock_coolify, mock_trigger,
                                                      mock_notify, mock_watch):
    """The baseline status is read from the service listing we already fetched."""
    mock_coolify.return_value = [MATCHING_SERVICE]
    mock_trigger.return_value = {"ok": True, "deployment_uuid": None}

    with patch.dict(os.environ, AUTO_DEPLOY_ENV, clear=True):
        client.post("/webhook", json=DIUN_PAYLOAD, headers={"X-Diun-Secret": "s3cret"})

    mock_notify.assert_not_called()
    mock_watch.assert_called_once()
    args, kwargs = mock_watch.call_args
    assert args[2] == "svc-1"
    assert args[3] == "running:healthy"
    assert kwargs["container_name"] == "nextcloud"
    assert kwargs["server"] == "grawie-prod"


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


@patch('main.watch_deployment')
@patch('main.send_notification')
@patch('main.trigger_coolify')
@patch('main.get_coolify_applications')
def test_update_event_still_auto_deploys(mock_coolify, mock_trigger, mock_notify, mock_watch):
    """An "update" (the tag in service was republished) keeps deploying by itself."""
    mock_coolify.return_value = [MATCHING_SERVICE]
    mock_trigger.return_value = {"ok": True, "deployment_uuid": "dep-1"}

    with patch.dict(os.environ, AUTO_DEPLOY_ENV, clear=True):
        resp = client.post("/webhook", json=DIUN_PAYLOAD, headers={"X-Diun-Secret": "s3cret"})

    assert resp.json()["action"] == "auto-deploy"
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
    """Coolify re-dumps the compose on save and quotes image: (seen on boum)."""
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
    "server": {"name": "boum"},
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
