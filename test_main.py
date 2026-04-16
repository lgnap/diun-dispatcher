import os
import json
from unittest.mock import patch, AsyncMock
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
    mock_trigger.return_value = True

    with patch.dict(os.environ, {
        "WEBHOOK_SECRET": "test-secret",
        "COOLIFY_URL": "http://coolify",
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
        "COOLIFY_URL": "http://coolify",
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
        "COOLIFY_URL": "http://coolify",
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
