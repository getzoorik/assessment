"""The one HTTP policy: every public price list the run fetches goes through UrllibHttpClient."""

import http.client
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Optional

from zoorik import __version__
from zoorik.cloud.fields import parse_json
from zoorik.errors import PriceListUnreachable
from zoorik.system.clock import Clock

# A failure that may pass is tried 3 times: it waits 2 s after the first try and 4 s after the second.
RETRY_WAITS = (2.0, 4.0)
TIMEOUT = 60.0
USER_AGENT = f"zoorik-assessment/{__version__}"
TOO_MANY_REQUESTS = 429
SERVER_ERRORS = 500

Fetch = Callable[[urllib.request.Request, float], bytes]


def urlopen_body(request: urllib.request.Request, timeout: float) -> bytes:
    """Send request and give the whole body of the answer."""
    with urllib.request.urlopen(request, timeout=timeout) as answer:
        body: bytes = answer.read()
    return body


def may_pass(status: int) -> bool:
    """Tell whether an HTTP error may pass on a later try: the server throttled the request, or failed itself."""
    return status == TOO_MANY_REQUESTS or status >= SERVER_ERRORS


class UrllibHttpClient:
    """Fetches JSON from public URLs, trying again while a failure may pass."""

    def __init__(self, clock: Clock, fetch: Fetch = urlopen_body) -> None:
        """Wait between tries on clock; fetch sends one request, and tests replace it."""
        self._clock = clock
        self._fetch = fetch

    def get_json(self, url: str) -> object:
        """GET url and give its body, parsed as JSON."""
        source = f"the public price list ({urllib.parse.urlsplit(url).hostname})"
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        for wait in RETRY_WAITS:
            body = self._try(request, source)
            if body is not None:
                return parse_json(body.decode("utf-8", errors="replace"), source)
            self._clock.sleep(wait)
        body = self._try(request, source)
        if body is None:
            raise PriceListUnreachable(f"{source} is not reachable")
        return parse_json(body.decode("utf-8", errors="replace"), source)

    def _try(self, request: urllib.request.Request, source: str) -> Optional[bytes]:
        """Send request once: give its body, or None after a failure that may pass; raise any other failure."""
        try:
            return self._fetch(request, TIMEOUT)
        except urllib.error.HTTPError as error:
            # The error holds the server's open answer; closing it frees the connection now, not at garbage collection.
            error.close()
            if may_pass(error.code):
                return None
            raise PriceListUnreachable(f"{source} refused the request (HTTP {error.code})") from None
        except (urllib.error.URLError, http.client.HTTPException, OSError):
            # The connection could not be made, broke off or timed out, which may pass.
            return None
