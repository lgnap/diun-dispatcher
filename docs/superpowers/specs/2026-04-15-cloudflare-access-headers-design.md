# Cloudflare Access Headers Support — Design Spec

**Date:** 2026-04-15  
**Author:** Claude Code  
**Status:** Approved

## Overview

Add support for Cloudflare Access authentication headers (`CF-Access-Client-Id` and `CF-Access-Client-Secret`) to all HTTP requests made to the Coolify API. This enables the dispatcher to authenticate through Cloudflare tunnels with access control enabled.

## Motivation

When a Coolify instance is exposed through a Cloudflare tunnel with access control (Zero Trust), requests must include Cloudflare service token credentials. The dispatcher currently has no way to include these headers, making it unable to communicate with protected Coolify deployments.

## Design

### 1. New Helper Function

Add `get_cloudflare_headers()` to the Helpers section (after `load_apprise_urls()`):

```python
def get_cloudflare_headers() -> dict:
    """
    Returns a dict with Cloudflare Access headers if both credentials are configured.
    If either is missing, returns an empty dict (headers are optional).
    """
    cf_id = os.getenv("CF_ACCESS_CLIENT_ID", "").strip()
    cf_secret = os.getenv("CF_ACCESS_CLIENT_SECRET", "").strip()
    
    if cf_id and cf_secret:
        return {
            "CF-Access-Client-Id": cf_id,
            "CF-Access-Client-Secret": cf_secret
        }
    return {}
```

### 2. Integration Points

Update both Coolify API calls to merge Cloudflare headers:

**In `get_coolify_applications()`:**
```python
headers = {
    "Authorization": f"Bearer {coolify_token}",
    **get_cloudflare_headers()
}
async with httpx.AsyncClient(timeout=10) as client:
    resp = await client.get(url, headers=headers)
```

**In `trigger_coolify()`:**
```python
headers = {
    "Authorization": f"Bearer {coolify_token}",
    **get_cloudflare_headers()
}
async with httpx.AsyncClient(timeout=10) as client:
    resp = await client.get(url, headers=headers)
```

### 3. Configuration

Users configure via environment variables in `.env`:

```
CF_ACCESS_CLIENT_ID=<service-token-client-id>
CF_ACCESS_CLIENT_SECRET=<service-token-secret>
```

Both variables must be present and non-empty for headers to be included. If either is missing or empty, requests are sent without Cloudflare headers (backward compatible).

## Behavior

- **Headers present & non-empty:** Both headers are added to all Coolify API requests
- **Either header missing or empty:** No Cloudflare headers are added; requests proceed with just Authorization bearer token
- **No new logging:** Cloudflare headers are silent by default (no auth logging)
- **No validation:** If headers are invalid, the Coolify API will reject the request with a 401/403 — this is expected behavior

## Out of Scope

- Cloudflare headers for non-Coolify requests (Apprise, etc.)
- Header validation or error-specific handling
- Rotating or refreshing service tokens

## Testing

Manual testing scenarios:
1. **With Cloudflare headers configured:** Verify requests to Coolify include both headers
2. **Without Cloudflare headers:** Verify requests work without them (existing behavior preserved)
3. **With only one header:** Verify both must be present for headers to be added

## Files Modified

- `main.py` — Add helper function and update two HTTP request sites
