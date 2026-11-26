"""Azure's VMs with data disks, through Resource Graph, and what each VM's size allows, from the Compute SKUs.

The VMs of AKS clusters are left out: the Kubernetes part counts them, so each cost is counted once.
"""

import re
from dataclasses import replace
from typing import Dict, FrozenSet, List, Mapping, Optional, Sequence, Tuple, Union

from zoorik.cloud.azure import AzureCli, AzureScope, DataDiskRef, DiskRecord, ManagedGroup, RegionSkus, VmRecord
from zoorik.cloud.keys import AZURE
from zoorik.console import Progress
from zoorik.disks.facts import azure_limits
from zoorik.disks.model import DataDisk, Disk, Inventory, NotAssessed, ScopeRead, Unread, Vm
from zoorik.errors import AssessmentError, UnexpectedResponse
from zoorik.system.workers import parallel

# The resource type whose resource groups hold AKS node VMs, in lowercase: Azure compares resource types without case.
CLUSTER_NODES = "microsoft.containerservice/managedclusters"
PROVIDER_TYPE = re.compile(r"/providers/([^/]+/[^/]+)")
NOT_READABLE = "Resource Graph cannot read it (no Reader access?)"
UNMANAGED = "unmanaged (VHD) disk; not priced"
NO_RECORD = "disk details not readable"
NO_SIZE = "disk size not reported"


class AzureInventory:
    """Azure's VMs and managed disks in the signed-in directory's subscriptions."""

    def __init__(self, cli: AzureCli, scope: AzureScope) -> None:
        """Read through cli, in scope's subscriptions."""
        self._cli = cli
        self._scope = scope

    def read(self, progress: Progress) -> Inventory:
        """Read every VM with data disks, its disks and its size's limits, and count what each subscription holds."""
        names = {sub.id: sub.name for sub in self._scope.subscriptions}
        readable, unread = self._readable()
        if not readable:
            return Inventory((), (), (), tuple(unread), names)
        ids = [sub_id for sub_id in names if sub_id in readable]
        try:
            disks = self._cli.managed_disks(ids)
            records = self._cli.virtual_machines(ids)
        except AssessmentError as error:
            unread += [Unread(AZURE, names[sub_id], f"Resource Graph: {error}") for sub_id in ids]
            return Inventory((), (), (), tuple(unread), names)
        read = tuple(
            ScopeRead(
                names[sub_id],
                sum(1 for record in records if record.subscription == sub_id),
                sum(1 for disk in disks if disk.subscription == sub_id),
            )
            for sub_id in ids
        )
        with_data = [record for record in records if record.data_disks]
        try:
            services = managing_types(self._cli.managed_groups(ids))
        except AssessmentError as error:
            reason = f"could not tell which VMs are cluster nodes: {error}"
            unsure = tuple(
                NotAssessed(AZURE, vm.subscription, vm.location.lower(), vm.name, reason) for vm in with_data
            )
            return Inventory((), unsure, read, tuple(unread), names)
        by_id = {disk.id.lower(): disk for disk in disks}
        vms = [
            make_vm(record, services.get((record.subscription, record.resource_group.lower())), by_id)
            for record in with_data
        ]
        kept = [vm for vm in vms if vm.managed_by is None or vm.managed_by.lower() != CLUSTER_NODES]
        return Inventory(tuple(self._with_limits(kept, progress)), (), read, tuple(unread), names)

    def _readable(self) -> Tuple[FrozenSet[str], List[Unread]]:
        """Give the subscriptions Resource Graph can read, and name the others with the reason."""
        subscriptions = self._scope.subscriptions
        if not subscriptions:
            return frozenset(), []
        try:
            readable = self._cli.readable_subscriptions([sub.id for sub in subscriptions])
        except AssessmentError as error:
            return frozenset(), [Unread(AZURE, sub.name, f"Resource Graph: {error}") for sub in subscriptions]
        return readable, [Unread(AZURE, sub.name, NOT_READABLE) for sub in subscriptions if sub.id not in readable]

    def _with_limits(self, vms: Sequence[Vm], progress: Progress) -> List[Vm]:
        """Give each VM what its size allows, from its subscription's Compute SKUs in its region, read in parallel."""
        keys = sorted({(vm.account, vm.region) for vm in vms})
        found = dict(zip(keys, parallel(self._skus, keys, progress)))
        return [with_limits(vm, found[(vm.account, vm.region)]) for vm in vms]

    def _skus(self, key: Tuple[str, str]) -> RegionSkus:
        """Read the Compute SKUs of one subscription and region."""
        return self._cli.compute_skus(*key)


def managing_types(groups: Sequence[ManagedGroup]) -> Dict[Tuple[str, str], str]:
    """Give the resource type that runs each managed resource group, by its subscription and lowercase name."""
    types: Dict[Tuple[str, str], str] = {}
    for group in groups:
        match = PROVIDER_TYPE.search(group.managed_by)
        if match is None:
            raise UnexpectedResponse("unexpected answer: a resource group's managedBy names no resource type")
        types[(group.subscription, group.name.lower())] = match.group(1)
    return types


def make_vm(record: VmRecord, managed_by: Optional[str], disks: Mapping[str, DiskRecord]) -> Vm:
    """Build one VM from its record, its data disks from their own records."""
    return Vm(
        id=record.id,
        name=record.name,
        cloud=AZURE,
        account=record.subscription,
        region=record.location.lower(),
        zone=",".join(record.zones) if record.zones else None,
        size=record.size,
        os_type=record.os_type,
        power=record.power.split("/")[-1].capitalize(),
        data=tuple(sorted((data_disk(ref, disks) for ref in record.data_disks), key=lambda disk: disk.lun)),
        managed_by=managed_by,
        channel=None,
        limits=None,
        facts_note="",
    )


def data_disk(ref: DataDiskRef, disks: Mapping[str, DiskRecord]) -> DataDisk:
    """Build one data disk: a managed disk takes its size from its own record, an unmanaged VHD from the VM's."""
    if ref.managed_disk is None:
        return DataDisk(ref.lun, ref.size_gb, None, UNMANAGED, "")
    record = disks.get(ref.managed_disk.lower())
    if record is None:
        return DataDisk(ref.lun, None, None, NO_RECORD, "")
    if record.size_gb is None:
        return DataDisk(ref.lun, None, None, NO_SIZE, "")
    disk = Disk(
        id=record.id,
        cloud=AZURE,
        region=record.location.lower(),
        sku=record.sku,
        size_gb=record.size_gb,
        tier=record.tier,
        iops=record.iops,
        mbps=record.mbps,
        ade=record.ade is True,
        shared=record.max_shares is not None and record.max_shares > 1,
    )
    return DataDisk(ref.lun, record.size_gb, disk, "", "")


def with_limits(vm: Vm, skus: Union[RegionSkus, AssessmentError]) -> Vm:
    """Give the VM what its size allows, or the reason that isn't known."""
    if isinstance(skus, AssessmentError):
        return replace(vm, facts_note=str(skus))
    try:
        limits = azure_limits(vm, skus)
    except UnexpectedResponse as error:
        return replace(vm, facts_note=str(error))
    if limits is None:
        return replace(vm, facts_note=f"{vm.size} not listed in {vm.region}")
    return replace(vm, limits=limits)
