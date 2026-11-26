"""The one report of a run: page 1, the Kubernetes pages, the Disks pages, then the closing, each once, as a PDF."""

from dataclasses import dataclass
from datetime import datetime
from typing import Tuple

from zoorik.cloud.azure import OtherDirectory
from zoorik.disks.model import DisksResult
from zoorik.format import count
from zoorik.kubernetes.model import KubernetesResult
from zoorik.report.blocks import Report
from zoorik.report.closing import CloudUnread, closing, left_out
from zoorik.report.disks_pages import disks_pages
from zoorik.report.figures import DOT
from zoorik.report.kubernetes_pages import kubernetes_pages
from zoorik.report.names import Names
from zoorik.report.summary import disclosure, page_one, summary

TITLE = "Cloud cost assessment"


@dataclass(frozen=True)
class Run:
    """What the report says of the run itself: its start, its usage sample window, and the clouds it read or not."""

    started: datetime
    sample_minutes: int
    # The clouds read, by name, such as Azure and AWS.
    clouds: Tuple[str, ...]
    unread: Tuple[CloudUnread, ...]
    elsewhere: Tuple[OtherDirectory, ...]


def report(kubernetes: KubernetesResult, disks: DisksResult, run: Run, names: Names) -> bytes:
    """Write the report of both parts as one PDF, the names in it as names gives them."""
    date = f"{run.started.day} {run.started:%B %Y}"
    read = [" and ".join(run.clouds)]
    if kubernetes.clusters:
        read.append(count(len(kubernetes.clusters), "cluster"))
    if disks.vms:
        read.append(count(len(disks.vms), "VM"))
    document = Report(TITLE, DOT.join([*read, date]), date, DOT.join(["Zoorik cloud cost assessment", date]))
    page_one(document, summary(kubernetes, disks), disclosure(kubernetes, disks, run.started, run.sample_minutes))
    number = 1
    if kubernetes.today > 0:
        kubernetes_pages(document, number, kubernetes, names)
        number += 1
    if disks.in_saving:
        disks_pages(document, number, disks, names)
        number += 1
    closing(document, number, left_out(kubernetes, disks, run.unread, run.elsewhere, names), kubernetes, disks)
    return document.pdf()
