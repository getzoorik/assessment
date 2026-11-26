"""What a recording may keep: kubectl JSON only through its allowlist, and of a kubeconfig only one fact."""

import json
from typing import Literal, Mapping, Sequence, Union

# True keeps a value whole; a mapping keeps only its keys, each by its own rule. A list takes its rule for each item.
Keep = Union[Literal[True], Mapping[str, "Keep"]]


class UnredactedOutput(Exception):
    """A kubectl answer of a kind with no allowlist, which can't be recorded without risking private data."""


def prune(value: object, keep: Keep) -> object:
    """Keep only the parts of value that keep allows; a value not shaped as the rule expects becomes null."""
    if keep is True:
        return value
    if isinstance(value, list):
        return [prune(item, keep) for item in value]
    if isinstance(value, dict):
        return {key: prune(value[key], rule) for key, rule in keep.items() if key in value}
    return None


def kubeconfig(text: str) -> str:
    """Stand in for a kubeconfig: keep only whether its user signs in through kubelogin, never a server or secret."""
    if "kubelogin" in text:
        return "# kubeconfig redacted while recording; its user signs in through kubelogin\n"
    return "# kubeconfig redacted while recording\n"


class Redactor:
    """Redacts each answer before it reaches a recording."""

    def __init__(self, kubectl_lists: Mapping[str, Keep]) -> None:
        """Keep kubectl JSON by the allowlist for its kind, such as NodeList or PodList."""
        self._lists = kubectl_lists

    def output(self, argv: Sequence[str], text: str) -> str:
        """Give the part of a program's output that may be recorded: kubectl JSON pruned, anything else whole."""
        if argv[0] != "kubectl" or not text.lstrip().startswith("{"):
            return text
        document = json.loads(text)
        kind = document.get("kind")
        if kind not in self._lists:
            # Without an allowlist nothing says which fields are safe, so nothing is recorded.
            raise UnredactedOutput(f"no allowlist for kubectl answers of kind {kind}")
        return json.dumps(prune(document, self._lists[kind]), sort_keys=True)

    def written(self, text: str) -> str:
        """Give the part of a file a program wrote that may be recorded; the only such files are kubeconfigs."""
        return kubeconfig(text)
