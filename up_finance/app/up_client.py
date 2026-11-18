"""Minimal read-only client for the Up Banking API (https://developer.up.com.au).

Standard library only, so the add-on image needs no pip installs.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Iterator, Optional

log = logging.getLogger(__name__)

BASE_URL = "https://api.up.com.au/api/v1"


class UpError(Exception):
    pass


class UpAuthError(UpError):
    pass


class UpClient:
    def __init__(self, token: str, base_url: str = BASE_URL, timeout: int = 30):
        if not token:
            raise UpAuthError("No Up personal access token configured")
        self.token = token.strip()
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    # -- low level ---------------------------------------------------------
    def _get(self, url: str) -> dict:
        if not url.startswith("http"):
            url = self.base_url + url
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
                "User-Agent": "up-finance-ha",
            },
        )
        delay = 2.0
        for attempt in range(6):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                if e.code == 401:
                    raise UpAuthError("Up rejected the access token (401)") from e
                if e.code == 429 or e.code >= 500:
                    retry_after = e.headers.get("Retry-After") if e.headers else None
                    wait = float(retry_after) if retry_after and retry_after.isdigit() else delay
                    log.warning("Up API %s, retrying in %.0fs", e.code, wait)
                    time.sleep(wait)
                    delay = min(delay * 2, 60)
                    continue
                body = e.read().decode("utf-8", "replace")[:300]
                raise UpError(f"Up API error {e.code}: {body}") from e
            except (urllib.error.URLError, TimeoutError) as e:
                if attempt == 5:
                    raise UpError(f"Network error talking to Up: {e}") from e
                log.warning("Network error (%s), retrying in %.0fs", e, delay)
                time.sleep(delay)
                delay = min(delay * 2, 60)
        raise UpError("Up API kept failing after retries")

    def _paginate(self, path: str, params: Optional[dict] = None) -> Iterator[dict]:
        query = {"page[size]": "100"}
        if params:
            query.update(params)
        url = f"{self.base_url}{path}?{urllib.parse.urlencode(query)}"
        while url:
            page = self._get(url)
            yield from page.get("data", [])
            url = (page.get("links") or {}).get("next")

    # -- endpoints ---------------------------------------------------------
    def ping(self) -> dict:
        return self._get("/util/ping")

    def accounts(self) -> list[dict]:
        return list(self._paginate("/accounts"))

    def categories(self) -> list[dict]:
        # /categories is not paginated
        return self._get("/categories").get("data", [])

    def transactions(self, since: Optional[str] = None, account_id: Optional[str] = None) -> Iterator[dict]:
        """Transactions newest first - for one account, or all accounts if account_id is None.

        `since` is an RFC 3339 timestamp; omit it for the full history.
        """
        params = {"filter[since]": since} if since else None
        path = f"/accounts/{account_id}/transactions" if account_id else "/transactions"
        return self._paginate(path, params)
