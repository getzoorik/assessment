"""The one HTTP policy."""

import email.message
import http.client
import io
import socket
import unittest
import urllib.error
import urllib.request
from typing import List, Sequence, Tuple, Union

from tests.replay.fake_cloud import VirtualClock
from tests.replay.scenarios import FACTS
from zoorik import __version__
from zoorik.errors import PriceListUnreachable, UnexpectedResponse
from zoorik.pricing.http import RETRY_WAITS, TIMEOUT, UrllibHttpClient

URL = "https://prices.azure.com/api/retail/prices?%24filter=x"
Outcome = Union[bytes, Exception]


def http_error(code: int) -> urllib.error.HTTPError:
    """Make the error urllib raises for an HTTP status, holding the server's answer as urlopen's does."""
    return urllib.error.HTTPError(URL, code, "status", email.message.Message(), io.BytesIO(b"error page"))


class Server:
    """Stands in for the network: gives each fetch the next outcome, and keeps each request and timeout."""

    def __init__(self, outcomes: Sequence[Outcome]) -> None:
        """Answer with outcomes, in order."""
        self._outcomes = list(outcomes)
        self.requests: List[urllib.request.Request] = []
        self.timeouts: List[float] = []

    def __call__(self, request: urllib.request.Request, timeout: float) -> bytes:
        """Give the next outcome: a body, or a raised failure."""
        self.requests.append(request)
        self.timeouts.append(timeout)
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class HttpPolicyTest(unittest.TestCase):
    """UrllibHttpClient."""

    def setUp(self) -> None:
        """Start a virtual clock."""
        self.clock = VirtualClock(FACTS.started)

    def fetch(self, outcomes: Sequence[Outcome]) -> Tuple[object, Server]:
        """Fetch URL from a server that answers with outcomes; give the answer and the server."""
        server = Server(outcomes)
        return UrllibHttpClient(self.clock, server).get_json(URL), server

    def test_a_failure_that_passes_is_tried_again(self) -> None:
        """503, then 503, then 200: the third answer is given, after waits of 2 s and 4 s."""
        answer, server = self.fetch([http_error(503), http_error(503), b'{"Items": []}'])
        self.assertEqual(answer, {"Items": []})
        self.assertEqual(len(server.requests), 3)
        self.assertEqual(self.clock.monotonic(), sum(RETRY_WAITS))
        self.assertEqual(RETRY_WAITS, (2.0, 4.0))

    def test_throttling_and_broken_connections_are_tried_again(self) -> None:
        """429, a refused connection, a timeout and a broken answer all count as failures that may pass."""
        failures: List[Exception] = [
            http_error(429),
            urllib.error.URLError(ConnectionRefusedError()),
            socket.timeout(),
            http.client.RemoteDisconnected("Remote end closed connection without response"),
        ]
        for failure in failures:
            answer, _ = self.fetch([failure, b"[]"])
            self.assertEqual(answer, [], failure)

    def test_a_refused_request_stops_at_once(self) -> None:
        """A 404 is not tried again, and is named with its status."""
        with self.assertRaisesRegex(
            PriceListUnreachable, r"^the public price list \(prices\.azure\.com\) refused the request \(HTTP 404\)$"
        ):
            self.fetch([http_error(404)])
        self.assertEqual(self.clock.monotonic(), 0.0)

    def test_each_error_answer_is_closed(self) -> None:
        """The answer an HTTP error holds is closed, whether the request is tried again or stops."""
        errors = [http_error(503), http_error(404)]
        with self.assertRaises(PriceListUnreachable):
            self.fetch(errors)
        self.assertEqual([error.fp.closed for error in errors], [True, True])

    def test_three_failures_are_unreachable(self) -> None:
        """After three failures that may pass, the price list is named as not reachable."""
        server = Server([http_error(500), http_error(502), urllib.error.URLError("no route")])
        with self.assertRaisesRegex(
            PriceListUnreachable, r"^the public price list \(prices\.azure\.com\) is not reachable$"
        ):
            UrllibHttpClient(self.clock, server).get_json(URL)
        self.assertEqual(len(server.requests), 3)
        self.assertEqual(self.clock.monotonic(), 6.0)

    def test_an_answer_that_is_not_json_is_unexpected(self) -> None:
        """A body that is not JSON raises UnexpectedResponse naming the price list."""
        with self.assertRaisesRegex(UnexpectedResponse, r"the public price list \(prices\.azure\.com\) gave no JSON"):
            self.fetch([b"<html>maintenance</html>"])

    def test_each_request_names_the_tool_and_waits_60_seconds(self) -> None:
        """Each request carries the user agent with the package's version, and a 60 s timeout."""
        _, server = self.fetch([b"{}"])
        self.assertEqual(server.requests[0].get_header("User-agent"), f"zoorik-assessment/{__version__}")
        self.assertEqual(server.requests[0].full_url, URL)
        self.assertEqual(server.timeouts, [TIMEOUT])
        self.assertEqual(TIMEOUT, 60.0)
