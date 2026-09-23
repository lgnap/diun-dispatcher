import os
import json
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
        payload = {"hostname": "srv", "status": "new", "image": "nextcloud:34-apache"}
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
    ("gitea/gitea:1.27", "docker.io/gitea/gitea:1.9", "older"),
    ("gitea/gitea:latest", "docker.io/gitea/gitea:1.28", "newer"),
    ("gitea/gitea", "docker.io/gitea/gitea:latest", "in-service"),
    (None, "docker.io/library/nextcloud:35-apache", "newer"),
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

    with patch.dict(os.environ, AUTO_DEPLOY_ENV, clear=True):
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
def test_new_event_without_matching_resource_is_still_announced(mock_coolify, mock_trigger, mock_notify):
    mock_coolify.return_value = []

    with patch.dict(os.environ, AUTO_DEPLOY_ENV, clear=True):
        client.post("/webhook", json=_new_event("docker.io/library/nextcloud:35-apache"),
                    headers={"X-Diun-Secret": "s3cret"})

    mock_trigger.assert_not_called()
    mock_notify.assert_called_once()


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
