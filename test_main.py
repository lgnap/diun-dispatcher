import os
from unittest.mock import patch
from main import get_cloudflare_headers


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
