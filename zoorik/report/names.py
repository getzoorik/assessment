"""The names the report shows: each one as it is, or under --anonymize as a short hash made fresh for each run.

A hash is a prefix for the kind of name, then 8 hex digits of SHA-256 over the run's salt, the kind and the name. The
same name gives the same hash all through one report, and a new one in the next run. Every name the customer chose is
hidden: subscriptions, accounts, clusters, node pools, namespaces, workloads, VMs and the mount points their reasons
name. Machine types, disk types and regions stay. The console keeps the real names: it is the user's own terminal.
"""

import hashlib
import re
from enum import Enum
from typing import Mapping

from zoorik.cloud.keys import AZURE

DIGITS = 8
# A mount point inside a reason, such as /data: a slash that starts a word, up to a space or a mark that ends it.
MOUNT_POINT = re.compile(r"(?<![\w.])/[^\s,;()]+")


class Kind(Enum):
    """A kind of name the report shows, by the prefix its hash takes."""

    SUBSCRIPTION = "sub-"
    ACCOUNT = "acct-"
    CLUSTER = "cluster-"
    POOL = "pool-"
    NAMESPACE = "ns-"
    WORKLOAD = "wl-"
    VM = "vm-"
    MOUNT = "mount-"


class Names:
    """Gives each name as the report shows it: the name itself, or under --anonymize its hash."""

    def __init__(self, salt: str, *, anonymize: bool) -> None:
        """Hash with salt when anonymize is on; otherwise show every name as it is."""
        self._salt = salt
        self._anonymize = anonymize

    def name(self, kind: Kind, value: str, within: str = "") -> str:
        """Give value, a name of kind, as the report shows it; within is what it is unique in, such as a namespace."""
        if not self._anonymize:
            return value
        identity = f"{self._salt}\0{kind.value}\0{within}\0{value}"
        return kind.value + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:DIGITS]

    def account(self, cloud: str, account: str, subscriptions: Mapping[str, str]) -> str:
        """Give a subscription by its name, from subscriptions, or an AWS account by its id, as the report shows it."""
        if cloud == AZURE:
            return self.name(Kind.SUBSCRIPTION, subscriptions[account])
        return self.name(Kind.ACCOUNT, account)

    def mounts(self, reason: str) -> str:
        """Give a VM's reason as the report shows it: under --anonymize, each mount point it names hidden."""
        if not self._anonymize:
            return reason
        return MOUNT_POINT.sub(lambda point: self.name(Kind.MOUNT, point.group(0)), reason)
