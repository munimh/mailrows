"""A tiny JSON-over-HTTP client with a rate limit and retries, on the standard library."""

from __future__ import annotations

import http.client
import json
import random
import time
import urllib.error
import urllib.request

RETRY_CODES = {429, 500, 502, 503, 504}


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """urllib would follow a redirect and resend our Authorization and secret headers to
    whatever host it points at. An API call never needs a redirect, so refuse them all."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code,
                                     f"refused to follow a redirect to {newurl.split('?')[0]}", headers, fp)


_OPENER = urllib.request.build_opener(_NoRedirects)


class HttpError(Exception):
    def __init__(self, message: str, status: int | None = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body


class RateLimiter:
    """Never more than `per_second` calls in any one second (spaced evenly)."""

    def __init__(self, per_second: float, clock=time.monotonic, sleep=time.sleep):
        self.gap = 1.0 / per_second if per_second > 0 else 0.0
        self.clock = clock
        self.sleep = sleep
        self.last = None

    def wait(self) -> None:
        if self.last is not None:
            left = self.last + self.gap - self.clock()
            if left > 0:
                self.sleep(left)
        self.last = self.clock()


class JsonClient:
    def __init__(self, headers: dict[str, str], per_second: float = 4.5, retries: int = 5,
                 backoff: float = 1.0, max_wait: float = 30.0, timeout: float = 30.0, sleep=time.sleep):
        self.headers = headers
        self.limiter = RateLimiter(per_second, sleep=sleep)
        self.retries = retries
        self.backoff = backoff
        self.max_wait = max_wait
        self.timeout = timeout
        self.sleep = sleep

    def call(self, method: str, url: str, body=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = dict(self.headers)
        if data is not None:
            headers["Content-Type"] = "application/json"
        last: HttpError | None = None
        for attempt in range(self.retries + 1):
            self.limiter.wait()
            req = urllib.request.Request(url, data=data, method=method, headers=headers)
            try:
                with _OPENER.open(req, timeout=self.timeout) as resp:
                    raw = resp.read()
                    return json.loads(raw.decode("utf-8")) if raw.strip() else {}
            except urllib.error.HTTPError as exc:
                if 300 <= exc.code < 400:
                    raise HttpError(f"HTTP {exc.code}: {exc.reason}", exc.code) from None
                text = exc.read().decode("utf-8", "replace")[:500] if exc.fp else ""
                last = HttpError(f"HTTP {exc.code}", exc.code, text)
                if exc.code not in RETRY_CODES:
                    raise last from None
                wait = _retry_after(exc.headers.get("Retry-After") if exc.headers else None)
                if wait is not None:
                    wait = min(wait, self.max_wait * 2)
            except (urllib.error.URLError, http.client.HTTPException, OSError) as exc:
                reason = getattr(exc, "reason", exc)
                last = HttpError(f"could not reach the server ({reason})")
                wait = None
            except ValueError:
                raise HttpError("the server sent back a reply that isn't JSON") from None
            if attempt == self.retries:
                break
            if wait is None:
                wait = min(self.max_wait, self.backoff * (2 ** attempt)) * random.uniform(0.8, 1.2)
            self.sleep(wait)
        raise last


def _retry_after(value):
    try:
        return max(0.0, float(value)) if value else None
    except ValueError:
        return None
