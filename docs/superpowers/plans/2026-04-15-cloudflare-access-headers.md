# Cloudflare Access Headers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add support for Cloudflare Access authentication headers to all Coolify API requests, enabling the dispatcher to work with Coolify instances protected by Cloudflare Zero Trust tunnels.

**Architecture:** Add a single helper function that reads optional environment variables and returns Cloudflare authentication headers as a dict. Merge these headers into both existing Coolify API calls (fetching applications and triggering deployments). The implementation is backward-compatible — if headers aren't configured, requests work as before.

**Tech Stack:** Python 3.x, httpx (async HTTP client), os module for environment variables

---

## File Structure

- **main.py** — Add `get_cloudflare_headers()` helper; modify `get_coolify_applications()` and `trigger_coolify()` to include Cloudflare headers in requests

---

## Tasks

### Task 1: Add `get_cloudflare_headers()` helper function

**Files:**
- Modify: `main.py:41-48` (add function after `load_apprise_urls()`)

- [ ] **Step 1: Write the test first**

Create a test file to verify the helper function behavior. Run the following to create `test_main.py`:

```python
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
```

Save this as `test_main.py` in the project root.

- [ ] **Step 2: Run tests to verify they all fail**

```bash
python -m pytest test_main.py -v
```

Expected output: All 4 tests fail with `ImportError: cannot import name 'get_cloudflare_headers' from 'main'`

- [ ] **Step 3: Implement the helper function**

Open `main.py` and add the function after `load_apprise_urls()` (around line 48):

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest test_main.py -v
```

Expected output: All 4 tests pass (PASSED)

- [ ] **Step 5: Commit**

```bash
git add main.py test_main.py
git commit -m "feat: add get_cloudflare_headers helper function with tests"
```

---

### Task 2: Update `get_coolify_applications()` to use Cloudflare headers

**Files:**
- Modify: `main.py:27-38` (update headers dict in the function)

- [ ] **Step 1: Update the function to include Cloudflare headers**

In `get_coolify_applications()`, replace the `headers` line (currently line 30) with:

```python
async def get_coolify_applications(coolify_url: str, coolify_token: str) -> list[dict]:
    """Fetch all services/applications from Coolify API"""
    url = f"{coolify_url.rstrip('/')}/api/v1/services"
    headers = {
        "Authorization": f"Bearer {coolify_token}",
        **get_cloudflare_headers()
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        logger.error(f"Failed to fetch Coolify services: {e}")
        return []
```

- [ ] **Step 2: Verify the change compiles and imports work**

Run:
```bash
python -c "from main import get_coolify_applications; print('OK')"
```

Expected output: `OK`

- [ ] **Step 3: Commit**

```bash
git add main.py
git commit -m "feat: add cloudflare headers to get_coolify_applications requests"
```

---

### Task 3: Update `trigger_coolify()` to use Cloudflare headers

**Files:**
- Modify: `main.py:100-111` (update headers dict in the function)

- [ ] **Step 1: Update the function to include Cloudflare headers**

In `trigger_coolify()`, replace the `headers` line (currently line 102) with:

```python
async def trigger_coolify(coolify_url: str, coolify_token: str, uuid: str) -> bool:
    url = f"{coolify_url.rstrip('/')}/api/v1/deploy?uuid={uuid}&force=false"
    headers = {
        "Authorization": f"Bearer {coolify_token}",
        **get_cloudflare_headers()
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            logger.info(f"Coolify deploy triggered uuid={uuid} status={resp.status_code}")
            return True
    except Exception as e:
        logger.error(f"Coolify deploy failed uuid={uuid}: {e}")
        return False
```

- [ ] **Step 2: Verify the change compiles**

Run:
```bash
python -c "from main import trigger_coolify; print('OK')"
```

Expected output: `OK`

- [ ] **Step 3: Commit**

```bash
git commit -am "feat: add cloudflare headers to trigger_coolify requests"
```

---

### Task 4: Run full test suite

**Files:**
- Test: `test_main.py`

- [ ] **Step 1: Run all tests**

```bash
python -m pytest test_main.py -v
```

Expected output: All 4 tests PASS

- [ ] **Step 2: Verify no import or syntax errors in main.py**

```bash
python -m py_compile main.py
```

Expected output: No errors

- [ ] **Step 3: Commit (if any additional changes)**

If tests pass and no compilation errors:

```bash
git log --oneline | head -5
```

Should show the three feature commits from Tasks 1-3.

---

## Testing Checklist

Manual verification (no automated tests possible without a real Coolify instance):

- [ ] **Scenario 1: With Cloudflare headers configured**
  - Set `CF_ACCESS_CLIENT_ID=test-id` and `CF_ACCESS_CLIENT_SECRET=test-secret` in `.env`
  - Run the dispatcher and verify logs show requests are made
  - (In a real Coolify setup with Cloudflare tunnel, requests should succeed)

- [ ] **Scenario 2: Without Cloudflare headers**
  - Remove or comment out `CF_ACCESS_CLIENT_ID` and `CF_ACCESS_CLIENT_SECRET` in `.env`
  - Run the dispatcher
  - Verify requests work as before (backward compatible)

- [ ] **Scenario 3: Only one header configured**
  - Set `CF_ACCESS_CLIENT_ID=test-id` but leave `CF_ACCESS_CLIENT_SECRET` empty
  - Run the dispatcher
  - Verify Cloudflare headers are NOT added to requests (both required)

---

## Summary

- **+1 function:** `get_cloudflare_headers()` with 4 test cases
- **+2 modifications:** Headers merged into `get_coolify_applications()` and `trigger_coolify()`
- **Backward compatible:** Existing setups without Cloudflare headers continue to work
- **Configuration:** Optional `CF_ACCESS_CLIENT_ID` and `CF_ACCESS_CLIENT_SECRET` env vars
