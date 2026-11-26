"""The HTTP client the run's price reads go through; this module holds only its protocol."""

from typing import Protocol


class HttpClient(Protocol):
    """Fetches JSON documents from public URLs."""

    def get_json(self, url: str) -> object:
        """GET url and give its body, parsed as JSON."""
