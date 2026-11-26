"""What a recording may keep: kubectl JSON only through the package's allowlist, and of a kubeconfig only one fact."""

import json
from typing import Dict, List, Sequence

from zoorik.cloud.fields import Record
from zoorik.kubectl.objects import (
    KINDS,
    PODS,
    WHOLE,
    Fields,
    Json,
    Kind,
    placement_label_keys,
    read_pod,
    slim,
    slim_page,
)

# Of the version check, a recording keeps only the version.
VERSION = Fields({"gitVersion": WHOLE})
LISTS: Dict[str, Kind] = {kind.list_kind: kind for kind in KINDS}


class UnredactedOutput(Exception):
    """A kubectl answer with no allowlist, which can't be recorded without risking private data."""


def kubeconfig(text: str) -> str:
    """Stand in for a kubeconfig: keep only whether its user signs in through kubelogin, never a server or secret."""
    if "kubelogin" in text:
        return "# kubeconfig redacted while recording; its user signs in through kubelogin\n"
    return "# kubeconfig redacted while recording\n"


def page_document(kind: Kind, items: Sequence[Json], token: str = "") -> Dict[str, object]:
    """Write a page of kind's list as the API does, holding only items and, when there is a next page, its token."""
    document: Dict[str, object] = {"kind": kind.list_kind, "items": list(items)}
    if token:
        document["metadata"] = {"continue": token}
    return document


def pod_page(text: str) -> bool:
    """Tell whether recorded kubectl output is a page of pods."""
    return text.startswith("{") and json.loads(text).get("kind") == PODS.list_kind


def narrow_pod_items(items: Sequence[Json]) -> List[Json]:
    """Keep of each kept pod's labels only those whose keys a required placement term of one of these pods uses."""
    keys = placement_label_keys([read_pod(Record(item, f"pods[{index}]")) for index, item in enumerate(items)])
    narrowed: List[Json] = []
    for item in items:
        labels = item["metadata"].get("labels")
        if labels is None:
            narrowed.append(item)
        else:
            kept = {key: value for key, value in labels.items() if key in keys}
            narrowed.append({**item, "metadata": {**item["metadata"], "labels": kept}})
    return narrowed


class Redactor:
    """Redacts each answer before it reaches a recording."""

    def output(self, argv: Sequence[str], text: str) -> str:
        """Give the part of a program's output that may be recorded: kubectl JSON slimmed, anything else whole."""
        if argv[0] != "kubectl" or not text.lstrip().startswith("{"):
            return text
        document = json.loads(text)
        kind = LISTS.get(document.get("kind"))
        if kind is not None:
            page = slim_page(document, kind)
            return json.dumps(page_document(kind, page.items, page.token or ""), sort_keys=True)
        if argv[-1] == "/version":
            return json.dumps(slim(document, VERSION, "version"), sort_keys=True)
        # Without an allowlist nothing says which fields are safe, so nothing is recorded.
        raise UnredactedOutput(f"no allowlist for kubectl answers of kind {document.get('kind')}")

    def written(self, text: str) -> str:
        """Give the part of a file a program wrote that may be recorded; the only such files are kubeconfigs."""
        return kubeconfig(text)

    def narrow_pod_pages(self, pages: Sequence[str]) -> List[str]:
        """Narrow one cluster's recorded pod pages, together, to the labels the readers keep."""
        documents = [json.loads(page) for page in pages]
        items = narrow_pod_items([item for document in documents for item in document["items"]])
        narrowed: List[str] = []
        for document in documents:
            count = len(document["items"])
            narrowed.append(json.dumps({**document, "items": items[:count]}, sort_keys=True))
            items = items[count:]
        return narrowed
