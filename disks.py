#!/usr/bin/env python3
"""Zoorik Amoeba: disk assessment.

Reads the VMs and disks you can see in Azure and AWS, estimates what Zoorik
Amoeba (Zoorik block storage) could save by rightsizing the data disks of
your Linux VMs, and writes one PDF report for you to e-mail to Zoorik.

Run it in Azure Cloud Shell or AWS CloudShell, or in any other terminal on
Linux, macOS or Windows where az or aws is signed in. It assesses each cloud
whose CLI is signed in:
    npx zoorik assess amoeba
    zoorik assess amoeba       (after pip install zoorik)
    curl -fsSL https://zoorik.com/assess/disks | python3 -
The curl line is for bash or zsh: Linux, macOS and both Cloud Shells. On
Windows, use one of the first two, in PowerShell or Command Prompt, where it
can ask its question.

The source is at github.com/getzoorik/assessment.

What it does. Apart from the one step marked below, which runs only if you
answer y, it only reads, and it sends nothing anywhere.
  Azure, in every enabled subscription of the directory you are signed in to:
  - Azure Resource Graph: your VMs, managed disks and resource groups.
  - Azure Compute: each VM size's data-disk slots and disk limits, and where
    Premium SSD v2 is offered.
  - Azure Monitor: 14 days of IOPS and throughput for the data disks the pool
    would replace, on each VM whose disk usage was read.
  - prices.azure.com: the public list prices of managed disks.
  AWS, in every enabled region of the account you are signed in to:
  - EC2: your instances, EBS volumes, and the EBS limits of each instance
    type with data volumes.
  - Systems Manager: which instances its agent manages.
  - CloudWatch: 14 days of IOPS and throughput for the data volumes the pool
    would replace, on each instance whose disk usage was read.
  - the AWS Price List API: EBS list prices.
  Only if you answer y to its question: one command that only reads, inside
  each running Linux VM with data disks, through Azure Run Command or AWS
  Systems Manager Run Command. It reads used space (df, lsblk), the kernel
  version and whether btrfs is available. On its
  side, Azure adds its Run Command extension to a VM that does not have it
  yet, keeps a copy of the command and its output under /var/lib/waagent on
  the VM, and records each run in the activity log. Systems Manager keeps the
  command and its output in its Run Command history, its agent keeps a copy
  under /var/lib/amazon/ssm on the instance, and CloudTrail records each call.
It writes one file: the PDF report, in the current directory. On Windows,
an az argument that cmd.exe would change goes through a temporary file that
az reads and the script deletes at once.
"""

import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
import zlib

if sys.version_info < (3, 8):
    print("This needs Python 3.8 or later. Open Azure Cloud Shell or AWS CloudShell and run it there.")
    sys.exit(1)

import http.client
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Tuple

# ---------------------------------------------------------------- config

VERSION = "1.2.1"
ONE_LINER = "curl -fsSL https://zoorik.com/assess/disks | python3 -"
SEND_TO = "founders@zoorik.com"

HOURS_PER_MONTH = 730
GIB = 2 ** 30
METRIC_DAYS = 14
WORKERS = 8
RUN_COMMAND_TIMEOUT = 300
RUN_COMMAND_SECONDS = 45
RUN_COMMAND_PERMISSION = "Microsoft.Compute/virtualMachines/runCommand/action"
PRICES_URL = "https://prices.azure.com/api/retail/prices"
GRAPH_URL = "/providers/Microsoft.ResourceGraph/resources?api-version=2022-10-01"
SKUS_API = "2021-07-01"
METRICS_API = "2023-10-01"
METRICS = ("Composite Disk Read Operations/sec", "Composite Disk Write Operations/sec",
           "Composite Disk Read Bytes/sec", "Composite Disk Write Bytes/sec")

CLOUD_NAMES = {"azure": "Azure", "aws": "AWS"}
# MB/s: 10^6 bytes per second on Azure; on AWS MiB/s, the unit gp3 throughput is set in.
MB = {"azure": 1e6, "aws": float(2 ** 20)}
AWS_ENV = dict(os.environ, AWS_PAGER="")
PRICE_LIST_REGION = "us-east-1"
LIVE_STATES = "Name=instance-state-name,Values=pending,running,stopping,stopped"
SSM_DOCUMENT = "AWS-RunShellScript"
SSM_PERMISSIONS = "ssm:SendCommand and ssm:GetCommandInvocation"
SSM_EXECUTION_SECONDS = 120
SSM_POLL_SECONDS = 3
# CloudWatch keeps 1-minute EBS data points; an hour's Maximum / 60 is its busiest minute, per second.
EBS_METRICS = (("VolumeReadOps", True, False), ("VolumeWriteOps", True, True),
               ("VolumeReadBytes", False, False), ("VolumeWriteBytes", False, True))
EBS_PRICE_FAMILIES = ("Storage", "System Operation", "Provisioned Throughput")
# What each EBS type bills besides its GB, as Amoeba prices it: gp3 the IOPS above 3,000 and the MB/s above 125,
# io1 and io2 every IOPS (io2 in bands, its tiers 2 and 3 from 32,000 and 64,000), the others their GB only.
EBS_BILLS = {"gp3": ("iops", "mbps"), "gp2": (), "io1": ("iops",), "io2": ("iops",), "st1": (), "sc1": ()}
IO2_TIER_STARTS = {2: 32000, 3: 64000}

# How Zoorik Amoeba sizes a pool: the same rules and numbers the product plans with.
HEADROOM_FRACTION = 0.20
HEADROOM_MIN_GIB = 50
ANCHOR_POOL_THRESHOLD_GIB = 512
ANCHOR_COUNT = 2
ANCHOR_CAP_FRACTION = 0.75
ANCHOR_MAX_GIB = 2048
MIN_ELASTICS = 3
MAX_ELASTICS = 6
ELASTIC_FLOOR_V2_GIB = 64
ELASTIC_FLOOR_V1_GIB = 128
ELASTIC_CAP_GIB = 512
ELASTIC_DIVISOR = 16
ELASTIC_FILL_FRACTION = 0.80
EVAC_SHARE = 0.30
EVAC_WINDOW_MINUTES = 120
SMALL_MEMBER_GIB = 8
DEGRADED_ANCHORS = 1
DEGRADED_MAX_ELASTICS = 3
MIN_POOL_SLOTS = 2
MIN_VM_SLOTS = 8
RESERVED_SLOTS = 1
DEMAND_FACTOR = 1.3
KERNEL_FLOOR = (5, 14)
NO_PRICE = "no list price"

# Premium SSD v2 limits, from Azure's documentation.
V2_MIN_IOPS = 3000
V2_MIN_MBPS = 125
V2_MAX_IOPS_PER_GIB = 500
V2_MBPS_PER_IOPS = 0.25
V2_MAX_MBPS = 2000

# How Zoorik Amoeba lays out a new pool on AWS, from its current planner: the hold size, then equal gp3 volumes.
GP3 = "gp3"
# Amoeba holds a pool at most 90% full, the high edge of the band every VM starts with.
BAND_HIGH = 0.90
URGENT_USED = 0.95
FREE_FLOOR_GIB = 10
BTRFS_OVERHEAD = 0.03
MIN_MEMBERS = 2
GP3_BASE_IOPS = 3000
GP3_BASE_MBPS = 125
GP3_MAX_IOPS = 80000
GP3_IOPS_PER_GIB = 500
GP3_MAX_MBPS = 2000
GP3_MBPS_PER_IOPS = 0.25
GP3_MAX_GIB = 16384
# EBS slots as Amoeba counts them: device names /dev/sdf to /dev/sdz, and AWS's shared Nitro limit for a type
# that reports none.
EBS_DEVICE_NAMES = 21
NITRO_SHARED_LIMIT = 28

# The pool goes on Premium SSD v2 wherever the VM's region and zone offer it, otherwise on Premium SSD.
# Regions where Azure takes Premium SSD v2 on a VM without an availability zone (nonzonal disks), from
# Microsoft Learn, "Deploy a Premium SSD v2 managed disk", Regional availability (updated 10 Sept 2026).
# Azure Compute lists only the zones that offer Premium SSD v2, not this, so a region missing here gets Premium SSD.
V2_NONZONAL_REGIONS = frozenset((
    "australiacentral2", "australiasoutheast", "brazilsoutheast", "canadaeast", "germanynorth", "northcentralus",
    "norwaywest", "southindia", "switzerlandwest", "taiwannorth", "ukwest", "usgovarizona", "westcentralus",
    "westus", "australiaeast", "brazilsouth", "eastasia", "francecentral", "germanywestcentral", "italynorth",
    "koreacentral", "mexicocentral", "newzealandnorth", "norwayeast", "polandcentral", "spaincentral",
    "southafricanorth", "southeastasia", "swedencentral", "switzerlandnorth"))

TIER_SIZES = ((4, "1"), (8, "2"), (16, "3"), (32, "4"), (64, "6"), (128, "10"), (256, "15"), (512, "20"),
              (1024, "30"), (2048, "40"), (4096, "50"), (8192, "60"), (16384, "70"), (32767, "80"))
HDD_TIER_SIZES = TIER_SIZES[3:]
V1_TIER_GIB = tuple(size for size, _ in TIER_SIZES)
# Provisioned IOPS and MB/s of each Premium SSD size, from Azure's documentation (bursting not counted).
V1_TIER_PERFORMANCE = {4: (120, 25), 8: (120, 25), 16: (120, 25), 32: (120, 25), 64: (240, 50), 128: (500, 100),
                       256: (1100, 125), 512: (2300, 150), 1024: (5000, 200), 2048: (7500, 250),
                       4096: (7500, 250), 8192: (16000, 500), 16384: (18000, 750), 32767: (20000, 900)}
DISK_TYPES = {
    "premium_lrs": ("P", "LRS", "Premium SSD"),
    "premium_zrs": ("P", "ZRS", "Premium SSD ZRS"),
    "standardssd_lrs": ("E", "LRS", "Standard SSD"),
    "standardssd_zrs": ("E", "ZRS", "Standard SSD ZRS"),
    "standard_lrs": ("S", "LRS", "Standard HDD"),
    "premiumv2_lrs": ("V2", "LRS", "Premium SSD v2"),
    "ultrassd_lrs": ("ULTRA", "LRS", "Ultra Disk"),
}
TIER_PRODUCTS = ("Premium SSD Managed Disks", "Standard SSD Managed Disks", "Standard HDD Managed Disks")
PROVISIONED_PRODUCTS = {"Azure Premium SSD v2": "Premium LRS ", "Ultra Disks": "Ultra LRS "}
PROVISIONED_METERS = {"Provisioned Capacity": "capacity", "Provisioned IOPS": "iops",
                      "Provisioned Throughput (MBps)": "mbps"}

# The one command that runs inside a VM, and only after a yes. It only reads. The two clouds differ only in how a
# data disk shows in the VM: by LUN on Azure, by volume id (an NVMe serial) on AWS.
_GUEST_HEAD = r"""echo S
echo "K $(uname -r)"
if grep -qw btrfs /proc/filesystems; then echo "B loaded"; elif modinfo -n btrfs >/dev/null 2>&1; then echo "B module"; else echo "B absent"; fi
"""
_GUEST_TAIL = r"""
timeout 30 df -P -B1 -l | awk '$1 ~ /^\/dev\// && $1 !~ /^\/dev\/loop/ {m=$6; for (i=7; i<=NF; i++) m=m" "$i; print "D", $3, m}'
echo E"""
_AZURE_DISKS = r"""for l in /dev/disk/azure/scsi1/lun*; do n=${l##*/lun}; case $n in ''|*[!0-9]*) continue;; esac; echo "L $n $(lsblk -nro MOUNTPOINT "$l" | tr '\n' ' ')"; done"""
_AWS_DISKS = r"""for l in /dev/disk/by-id/nvme-Amazon_Elastic_Block_Store_vol*; do case $l in *-part*) continue;; esac; [ -e "$l" ] || continue; v=${l##*_vol}; echo "V vol-${v%%[-_]*} $(lsblk -nro MOUNTPOINT "$l" | tr '\n' ' ')"; done"""
GUEST_SCRIPT = {"azure": _GUEST_HEAD + _AZURE_DISKS + _GUEST_TAIL, "aws": _GUEST_HEAD + _AWS_DISKS + _GUEST_TAIL}


def say(text: str = "", end: str = "\n") -> None:
    print(text, end=end, flush=True)


# ---------------------------------------------------------------- commands

class Failed(Exception):
    """An expected failure, carried as a short reason."""


class Overloaded(Failed):
    """The pool fits the VM, but cannot carry the measured load."""


_REASONS = (
    ("runcommand/action", "no permission to use Run Command (" + RUN_COMMAND_PERMISSION + ")"),
    ("please run 'az login'", "not signed in to Azure; run az login"),
    ("az login", "Azure sign-in has expired; run az login"),
    ("failed to connect to msi", "Cloud Shell sign-in issue; run az login"),
    ("authorizationfailed", "access denied"),
    ("forbidden", "access denied"),
    ("execution is in progress", "another Run Command is already running on this VM"),
    ("vmagentstatuscommunicationerror", "the VM agent is not reporting"),
    ("vm agent", "the VM agent is not ready"),
    ("deallocat", "the VM is not running"),
    ("vmextensionprovisioning", "Azure could not start its Run Command extension on the VM"),
    ("vmextensionhandler", "Azure could not start its Run Command extension on the VM"),
    ("toomanyrequests", "Azure is throttling requests; try again later"),
    ("subscriptionnotfound", "subscription not found"),
    ("resourcenotfound", "not found"),
    ("ssm:sendcommand", "no permission to use Systems Manager Run Command (ssm:SendCommand)"),
    ("ssm:getcommandinvocation", "no permission to read Run Command results (ssm:GetCommandInvocation)"),
    ("invocationdoesnotexist", "the command has not reached the instance yet"),
    ("invalidinstanceid", "Systems Manager cannot reach this instance"),
    ("unable to locate credentials", "no AWS credentials; run aws configure or aws sso login"),
    ("token has expired", "AWS sign-in has expired; run aws sso login"),
    ("expiredtoken", "AWS credentials have expired"),
    ("accessdenied", "access denied"),
    ("unauthorizedoperation", "access denied"),
    ("optinrequired", "region not enabled for this account"),
    ("requestlimitexceeded", "AWS is throttling requests; try again later"),
    ("timed out", "no answer in time"),
)


def reason(text: str) -> str:
    """Turn tool error output into a short reason, free of user and object names."""
    low = (text or "").lower()
    for needle, short in _REASONS:
        if needle in low:
            return short
    line = next((x.strip() for x in (text or "").splitlines() if x.strip()), "unknown error")
    line = re.sub(r"^(error|ERROR|Error)[:\s]+", "", line)
    line = re.sub(r"\"[^\"]*\"|'[^']*'", "...", line)
    return line[:100]


THROTTLED = re.compile(r"\b429\b|toomanyrequests|throttl|requestlimitexceeded|rate exceeded", re.IGNORECASE)
CMD_SPECIAL = re.compile(r'["&|<>^%()\r\n]')
# On Windows the CLIs run in their own process group, so Ctrl-C stops only the script: a call in flight finishes on
# its own, and the worker waiting on it starts nothing new before the script leaves.
NEW_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
# On Windows, the folder that holds the az arguments cmd.exe would change; main() removes it however the run ends.
ARG_DIR = ""


def decoded(data: bytes) -> str:
    """CLI output as text, with Windows line ends made plain: UTF-8, or on Windows the console code page, which az
    writes in when its output is piped."""
    try:
        out = data.decode("utf-8")
    except UnicodeDecodeError:
        out = data.decode("mbcs" if os.name == "nt" else "utf-8", "replace")
    return out.replace("\r\n", "\n")


def arg_file(value: str, files: List[str]) -> str:
    """On Windows az is az.cmd, whose arguments cmd.exe reads again, so a value it would change goes in a file
    that az reads instead (@path)."""
    fd, path = tempfile.mkstemp(prefix="zoorik-", dir=ARG_DIR or None)
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
        f.write(value)
    files.append(path)
    return "@" + path


def cli(cmd: List[str], timeout: float, env: Dict[str, str]):
    """Run one az or aws command and return its JSON output, or raise Failed. It retries when the cloud throttles."""
    cloud = "Azure" if cmd[0] == "az" else "AWS"
    files: List[str] = []
    if os.name == "nt" and cmd[0] == "az":
        cmd = cmd[:1] + [arg_file(a, files) if CMD_SPECIAL.search(a) else a for a in cmd[1:]]
    try:
        for attempt in range(3):
            try:
                p = subprocess.run([shutil.which(cmd[0]) or cmd[0]] + cmd[1:], stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, env=env,
                                   creationflags=NEW_GROUP)
            except FileNotFoundError:
                raise Failed(f"the {cloud} CLI ({cmd[0]}) is not installed") from None
            except subprocess.TimeoutExpired:
                raise Failed(f"no answer within {max(1, round(timeout / 60))} min") from None
            out = decoded(p.stdout)
            if p.returncode == 0:
                try:
                    return json.loads(out or "null")
                except ValueError:
                    raise Failed(f"unexpected {cmd[0]} output") from None
            error = decoded(p.stderr) or out
            if attempt < 2 and THROTTLED.search(error):
                time.sleep(5 * (attempt + 1))
                continue
            raise Failed(reason(error))
        raise Failed(f"{cloud} is throttling requests; try again later")
    finally:
        for path in files:
            os.remove(path)


def az(args: List[str], timeout: float = 120):
    """One az command. Every Azure call goes through here.

    The Azure CLI's own usage telemetry is switched off for these calls only; your az settings stay as they are."""
    return cli(["az"] + list(args) + ["-o", "json", "--only-show-errors"], timeout,
               dict(os.environ, AZURE_CORE_COLLECT_TELEMETRY="false"))


def aws(args: List[str], region: str, timeout: float = 120):
    """One aws command in one region. Every AWS call goes through here, with the AWS CLI's pager off."""
    return cli(["aws"] + list(args) + ["--output", "json", "--region", region], timeout, AWS_ENV)


def rest(method: str, url: str, body: Optional[dict] = None, timeout: float = 120):
    """One Azure Resource Manager call. A URL that starts with / goes to the signed-in cloud."""
    args = ["rest", "--method", method, "--url", url]
    if body is not None:
        args += ["--body", json.dumps(body)]
    return az(args, timeout)


def http_json(url: str) -> dict:
    """One GET to the public price list, tried three times when the error may pass."""
    req = urllib.request.Request(url, headers={"User-Agent": f"zoorik-disk-assessment/{VERSION}"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code != 429 and e.code < 500:
                break
        except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError):
            pass
        if attempt < 2:
            time.sleep(2 * (attempt + 1))
    raise Failed("the public price list (prices.azure.com) is not reachable")


def parallel(func: Callable, items: list, label: str) -> list:
    """Run func on every item, a few at a time, with a live counter. A failure becomes that item's result.

    On Ctrl-C the items not yet started are dropped, so nothing new starts after it."""
    results: list = [None] * len(items)
    if not items:
        return results

    def guarded(item):
        try:
            return func(item)
        except Failed as e:
            return e
        except Exception as e:  # one odd VM or disk must not stop the whole assessment
            return Failed(f"could not be read ({type(e).__name__})")

    say(f"\r{label}: 0/{len(items)}", end="")
    ex = ThreadPoolExecutor(WORKERS)
    futures = {ex.submit(guarded, item): i for i, item in enumerate(items)}
    pending = set(futures)
    try:
        while pending:
            # Short waits: before Python 3.14, Ctrl-C on Windows reaches no thread that waits without a timeout.
            done, pending = wait(pending, timeout=0.25, return_when=FIRST_COMPLETED)
            for fut in done:
                results[futures[fut]] = fut.result()
            if done:
                say(f"\r{label}: {len(items) - len(pending)}/{len(items)}", end="")
    except BaseException:
        for fut in futures:
            fut.cancel()
        ex.shutdown(wait=False)
        raise
    ex.shutdown()
    say()
    return results


def ask(question: str) -> Optional[bool]:
    """Ask y/N on the terminal. The script may arrive on stdin, so on Linux and macOS the answer is read from
    /dev/tty; on Windows from the console, when stdin is one.

    Keys pressed before the question appears are discarded, so only an answer to it counts.
    Returns None when there is no terminal to ask on."""
    if os.name == "nt":
        if not sys.stdin.isatty():
            return None
        import msvcrt
        while msvcrt.kbhit():
            msvcrt.getwch()
        say(question, end="")
        answer = sys.stdin.readline()
    else:
        try:
            with open("/dev/tty", encoding="utf-8", errors="replace") as tty:
                try:
                    import termios
                    termios.tcflush(tty.fileno(), termios.TCIFLUSH)
                except Exception:
                    pass
                say(question, end="")
                answer = tty.readline()
        except OSError:
            return None
    if not answer.endswith("\n"):
        say()
    return answer.strip().lower() in ("y", "yes")


# ---------------------------------------------------------------- model

@dataclass
class Subscription:
    """What one scan covers: an Azure subscription, or one region of the AWS account (tenant is the account)."""
    id: str
    name: str
    tenant: str
    vms: int = 0
    disks: int = 0
    note: str = ""
    cloud: str = "azure"


# Per hour of the metric window: (IOPS, bytes/s, write IOPS, write bytes/s), reads and writes added.
Hourly = Dict[str, Tuple[float, float, float, float]]


@dataclass
class Disk:
    id: str
    region: str
    sku: str
    size_gb: int
    tier: str
    iops: Optional[int]
    mbps: Optional[int]
    ade: bool
    max_shares: int = 1
    multi_attach: bool = False
    cloud: str = "azure"
    price: Optional[float] = None
    price_note: str = ""
    hourly: Optional[Hourly] = None
    metric_note: str = ""

    @property
    def shared(self) -> bool:
        return self.max_shares > 1 or self.multi_attach


@dataclass
class DataDisk:
    lun: int
    size_gb: int
    disk: Optional[Disk]
    note: str = ""
    device: str = ""


@dataclass
class Guest:
    kernel: str = ""
    btrfs: str = ""
    luns: Dict[int, List[str]] = field(default_factory=dict)
    mounts: Dict[str, int] = field(default_factory=dict)
    volumes: Dict[str, List[str]] = field(default_factory=dict)


@dataclass
class Vm:
    id: str
    name: str
    subscription: str
    resource_group: str
    region: str
    zone: str
    size: str
    os_type: str
    power: str
    data: List[DataDisk]
    cloud: str = "azure"
    slots: Optional[int] = None
    free_slots: Optional[int] = None
    uncached_iops: int = 0
    uncached_mbps: int = 0
    facts_note: str = ""
    pool_v2: Optional[bool] = None
    nvme: Optional[bool] = None
    managed_by: str = ""
    channel: str = ""
    guest: Optional[Guest] = None
    guest_note: str = ""
    reasons: List[str] = field(default_factory=list)
    used_gb: Optional[float] = None
    current: Optional[float] = None
    price_note: str = ""
    pooled: List[DataDisk] = field(default_factory=list)
    today: Optional[float] = None
    pool_gb: Optional[int] = None
    pool_cost: Optional[float] = None
    saving: Optional[float] = None

    @property
    def linux(self) -> bool:
        return self.os_type.lower() == "linux"

    @property
    def running(self) -> bool:
        return self.power == "Running"


@dataclass
class RegionFacts:
    sizes: Dict[str, Tuple[int, int, int]]
    v2_zones: Optional[set]
    v2_region: bool = False


@dataclass
class EbsRate:
    """One EBS type's list prices per month: per GB, per IOPS by band start, per MB/s, above what it includes."""
    gib: float
    iops: Dict[int, float] = field(default_factory=dict)
    included_iops: int = 0
    mbps: Optional[float] = None
    included_mbps: int = 0


@dataclass
class Prices:
    tiers: Dict[str, float] = field(default_factory=dict)
    v2: Dict[str, Dict[float, float]] = field(default_factory=dict)
    ultra: Dict[str, Dict[float, float]] = field(default_factory=dict)
    ebs: Dict[str, EbsRate] = field(default_factory=dict)


@dataclass
class Scan:
    """One cloud's part of the run: the Azure directory and its subscriptions, or the AWS account and its regions."""
    cloud: str
    label: str
    scopes: List[Subscription]
    skipped: List[Subscription] = field(default_factory=list)


def r2(value: float) -> float:
    return round(value + 1e-9, 2)


def iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- inventory

SUBSCRIPTIONS_QUERY = "resourcecontainers | where type =~ 'microsoft.resources/subscriptions' | project subscriptionId"
VM_QUERY = """resources
| where type =~ 'microsoft.compute/virtualmachines'
| project id, name, resourceGroup, subscriptionId, location, zones,
    size = tostring(properties.hardwareProfile.vmSize),
    osType = tostring(properties.storageProfile.osDisk.osType),
    power = tostring(properties.extended.instanceView.powerState.code),
    dataDisks = properties.storageProfile.dataDisks
| order by id asc"""
DISK_QUERY = """resources
| where type =~ 'microsoft.compute/disks'
| project id, subscriptionId, location, sku = tostring(sku.name), tier = tostring(properties.tier),
    sizeGb = toint(properties.diskSizeGB),
    iops = tolong(properties.diskIOPSReadWrite), mbps = tolong(properties.diskMBpsReadWrite),
    ade = tobool(properties.encryptionSettingsCollection.enabled), maxShares = toint(properties.maxShares)
| order by id asc"""
MANAGED_GROUPS_QUERY = """resourcecontainers
| where type =~ 'microsoft.resources/subscriptions/resourcegroups' and isnotempty(managedBy)
| project subscriptionId, name, managedBy
| order by subscriptionId asc, name asc"""


def signed_in() -> Tuple[str, List[Subscription], List[Subscription]]:
    """Return (directory label, subscriptions to scan, subscriptions left out with the reason)."""
    tenant = (az(["account", "show"]) or {}).get("tenantId", "")
    accounts = [a for a in az(["account", "list"]) or [] if a.get("state") == "Enabled"]
    label = tenant
    for a in accounts:
        if a.get("tenantId") == tenant and a.get("tenantDisplayName"):
            domain = a.get("tenantDefaultDomain")
            label = a["tenantDisplayName"] + (f" ({domain})" if domain else "")
    scan, skipped = [], []
    for a in accounts:
        sub = Subscription(a.get("id", ""), a.get("name", ""), a.get("tenantId", ""))
        if sub.tenant == tenant:
            scan.append(sub)
        else:
            sub.note = (f"in another Azure directory; to include it, run az login --tenant {sub.tenant} "
                        "and run this again")
            skipped.append(sub)
    return label, scan, skipped


def graph(query: str, subscriptions: List[str]) -> list:
    """Run one Resource Graph query over the subscriptions, 1,000 at a time, following every page."""
    rows = []
    for i in range(0, len(subscriptions), 1000):
        options = {"$top": 1000, "resultFormat": "objectArray"}
        while True:
            page = rest("post", GRAPH_URL, {"subscriptions": subscriptions[i:i + 1000], "query": query,
                                            "options": options}) or {}
            rows += page.get("data") or []
            token = page.get("$skipToken")
            if not token:
                break
            options = dict(options, **{"$skipToken": token})
    return rows


def make_disk(r: dict) -> Disk:
    return Disk(id=r.get("id") or "", region=(r.get("location") or "").lower(), sku=r.get("sku") or "",
                size_gb=int(r.get("sizeGb") or 0), tier=r.get("tier") or "", iops=r.get("iops"), mbps=r.get("mbps"),
                ade=r.get("ade") is True, max_shares=r.get("maxShares") if isinstance(r.get("maxShares"), int) else 1)


def make_vm(r: dict, disks: Dict[str, Disk]) -> Vm:
    data = []
    for d in r.get("dataDisks") or []:
        if not isinstance(d, dict) or not isinstance(d.get("lun"), int):
            continue
        managed = (d.get("managedDisk") or {}).get("id") or ""
        disk = disks.get(managed.lower()) if managed else None
        note = "" if disk else "unmanaged (VHD) disk; not priced" if not managed else "disk details not readable"
        data.append(DataDisk(lun=d["lun"], size_gb=int((disk.size_gb if disk else d.get("diskSizeGB")) or 0),
                             disk=disk, note=note))
    power = (r.get("power") or "").split("/")[-1]
    return Vm(id=r.get("id") or "", name=r.get("name") or "", subscription=r.get("subscriptionId") or "",
              resource_group=r.get("resourceGroup") or "", region=(r.get("location") or "").lower(),
              zone=",".join(r.get("zones") or []), size=r.get("size") or "", os_type=r.get("osType") or "",
              power=power.capitalize(), data=sorted(data, key=lambda x: x.lun))


def managing_service(managed_by: str) -> str:
    """The resource type that manages a resource group, e.g. Microsoft.Databricks/workspaces, without its name."""
    match = re.search(r"/providers/([^/]+/[^/]+)", managed_by or "")
    return match.group(1) if match else "another Azure service"


def inventory(subs: List[Subscription]) -> List[Vm]:
    """Every VM in the subscriptions with its managed disks, through Azure Resource Graph."""
    if not subs:
        return []
    try:
        seen = {r.get("subscriptionId") for r in graph(SUBSCRIPTIONS_QUERY, [s.id for s in subs])}
    except Failed as e:
        for s in subs:
            s.note = f"Resource Graph: {e}"
        return []
    for s in subs:
        if s.id not in seen:
            s.note = "Resource Graph cannot read it (no Reader access?)"
    readable = [s.id for s in subs if not s.note]
    if not readable:
        return []
    disk_rows = graph(DISK_QUERY, readable)
    disks = {(r.get("id") or "").lower(): make_disk(r) for r in disk_rows}
    vms = [make_vm(r, disks) for r in graph(VM_QUERY, readable)]
    try:
        managed = {(r.get("subscriptionId"), (r.get("name") or "").lower()): managing_service(r.get("managedBy"))
                   for r in graph(MANAGED_GROUPS_QUERY, readable)}
    except Failed:
        managed = {}
    for v in vms:
        v.managed_by = managed.get((v.subscription, v.resource_group.lower()), "")
    for s in subs:
        s.vms = sum(1 for v in vms if v.subscription == s.id)
        s.disks = sum(1 for r in disk_rows if r.get("subscriptionId") == s.id)
    return vms


# ---------------------------------------------------------------- AWS inventory

SLOT_DEVICE = re.compile(r"/dev/(?:sd|xvd)([f-z])")


def aws_home() -> str:
    return os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"


def aws_signed_in() -> Tuple[str, List[Subscription]]:
    """Return (the AWS account, its enabled regions to scan); the home region alone when they cannot be listed."""
    home = aws_home()
    account = (aws(["sts", "get-caller-identity"], home) or {}).get("Account", "")
    try:
        names = aws(["ec2", "describe-regions", "--query", "Regions[].RegionName"], home) or [home]
    except Failed as e:
        say(f"AWS: could not list regions ({e}); reading {home} only")
        names = [home]
    return account, [Subscription(r, r, account, cloud="aws") for r in sorted(names)]


def tags(item: dict) -> Dict[str, str]:
    return {t.get("Key"): t.get("Value") for t in item.get("Tags") or []}


def make_volume(v: dict, region: str) -> Disk:
    kind = v.get("VolumeType") or ""
    return Disk(id=v.get("VolumeId") or "", region=region, sku=kind, size_gb=int(v.get("Size") or 0), tier="",
                iops=v.get("Iops"), mbps=v.get("Throughput") if kind == GP3 else None, ade=False,
                multi_attach=v.get("MultiAttachEnabled") is True, cloud="aws")


def make_instance(i: dict, account: str, region: str, volumes: Dict[str, Disk]) -> Vm:
    """One EC2 instance with its data volumes: every EBS volume attached to it but its root volume."""
    data = []
    for m in sorted(i.get("BlockDeviceMappings") or [], key=lambda m: m.get("DeviceName") or ""):
        volume = (m.get("Ebs") or {}).get("VolumeId")
        if not volume or m.get("DeviceName") == i.get("RootDeviceName"):
            continue
        disk = volumes.get(volume)
        data.append(DataDisk(lun=len(data), size_gb=disk.size_gb if disk else 0, disk=disk,
                             note="" if disk else "volume details not readable", device=m.get("DeviceName") or ""))
    return Vm(id=i.get("InstanceId") or "", name=tags(i).get("Name") or i.get("InstanceId") or "",
              subscription=account, resource_group="", region=region, zone="", size=i.get("InstanceType") or "",
              os_type="Windows" if (i.get("Platform") or "").lower() == "windows" else "Linux",
              power=((i.get("State") or {}).get("Name") or "").capitalize(), data=data, cloud="aws")


def ebs_limits(vm: Vm, instance: dict, itype: dict) -> None:
    """The instance's data-volume slots and EBS limits, as Amoeba counts them: the type's attachment limit less the
    root volume and, where the limit is shared, the network interfaces and NVMe instance-store disks, within the
    device names /dev/sdf to /dev/sdz."""
    ebs = itype.get("EbsInfo") or {}
    limit = ebs.get("MaximumEbsAttachments")
    shared = limit is None or (ebs.get("AttachmentLimitType") or "").lower() == "shared"
    store = itype.get("InstanceStorageInfo") or {}
    store_disks = 0 if store.get("NvmeSupport") == "unsupported" else sum(d.get("Count") or 0
                                                                          for d in store.get("Disks") or [])
    others = 1 + (1 + store_disks if shared else 0)
    type_slots = max(0, min(EBS_DEVICE_NAMES, (NITRO_SHARED_LIMIT if limit is None else limit) - others))
    interfaces = max(0, len(instance.get("NetworkInterfaces") or []) - 1) if shared else 0
    held = [ord(m.group(1)) - ord("f") for m in (SLOT_DEVICE.fullmatch(dd.device) for dd in vm.data) if m]
    vm.free_slots = max(0, min(type_slots - interfaces - len(vm.data), EBS_DEVICE_NAMES - 1 - max(held, default=-1)))
    vm.slots = len(vm.data) + vm.free_slots
    optimized = ebs.get("EbsOptimizedInfo") or {}
    vm.uncached_iops = int(optimized.get("BaselineIops") or 0)
    vm.uncached_mbps = int(optimized.get("BaselineThroughputInMBps") or 0)
    vm.nvme = ebs.get("NvmeSupport") == "required"


def instance_types(names: List[str], region: str) -> Dict[str, dict]:
    types = {}
    for i in range(0, len(names), 100):
        out = aws(["ec2", "describe-instance-types", "--instance-types"] + names[i:i + 100], region) or {}
        types.update({t.get("InstanceType"): t for t in out.get("InstanceTypes") or []})
    return types


def aws_region(scope: Subscription) -> Tuple[List[Vm], int]:
    """One region's instances with their EBS volumes, each instance type's EBS limits, and which instances Systems
    Manager manages; and how many volumes the region holds."""
    region, account = scope.id, scope.tenant
    reservations = (aws(["ec2", "describe-instances", "--filters", LIVE_STATES], region) or {}).get("Reservations")
    instances = [i for r in reservations or [] for i in r.get("Instances") or []]
    volumes = {v.get("VolumeId"): make_volume(v, region)
               for v in (aws(["ec2", "describe-volumes"], region) or {}).get("Volumes") or []}
    vms = [make_instance(i, account, region, volumes) for i in instances]
    try:
        types, missing = instance_types(sorted({v.size for v in vms if v.size and v.data}), region), ""
    except Failed as e:
        types, missing = {}, f"instance type limits not readable: {e}"
    for v, i in zip(vms, instances):
        if v.size in types:
            ebs_limits(v, i, types[v.size])
        elif v.data:
            v.facts_note = missing or f"{v.size or 'instance type'} not listed in {region}"
    if any(v.linux and v.data for v in vms):
        try:
            found = aws(["ssm", "describe-instance-information", "--filters", "Key=PingStatus,Values=Online",
                         "Key=ResourceType,Values=EC2Instance"], region) or {}
            managed, unmanaged = {m.get("InstanceId") for m in found.get("InstanceInformationList") or []}, ""
        except Failed as e:
            managed, unmanaged = set(), f"Systems Manager not readable: {e}"
        for v in vms:
            if v.id not in managed:
                v.channel = unmanaged or "Systems Manager does not manage it (no SSM agent reporting)"
    return vms, len(volumes)


def aws_inventory(regions: List[Subscription]) -> List[Vm]:
    """Every EC2 instance in the regions with its EBS volumes, a few regions at a time; a region that fails has its
    note."""
    vms: List[Vm] = []
    for s, result in zip(regions, parallel(aws_region, regions, "Reading AWS regions")):
        if isinstance(result, Failed):
            s.note = str(result)
            continue
        s.vms, s.disks = len(result[0]), result[1]
        vms += result[0]
    return vms


# ---------------------------------------------------------------- VM sizes and prices

def region_facts(key: Tuple[str, str]) -> RegionFacts:
    """VM size limits in a region, and where in it Premium SSD v2 is offered to this subscription."""
    sub, region = key
    url = (f"/subscriptions/{sub}/providers/Microsoft.Compute/skus?api-version={SKUS_API}&%24filter="
           + urllib.parse.quote(f"location eq '{region}'"))
    facts = RegionFacts(sizes={}, v2_zones=None)
    items = []
    while url:
        page = rest("get", url, timeout=300) or {}
        items += page.get("value") or []
        url = page.get("nextLink") or ""
    for item in items:
        caps = {c.get("name"): str(c.get("value") or "") for c in item.get("capabilities") or []}
        if item.get("resourceType") == "virtualMachines" and caps.get("MaxDataDiskCount", "").isdigit():
            iops, bps = caps.get("UncachedDiskIOPS", ""), caps.get("UncachedDiskBytesPerSecond", "")
            facts.sizes[(item.get("name") or "").lower()] = (int(caps["MaxDataDiskCount"]),
                                                             int(iops) if iops.isdigit() else 0,
                                                             int(bps) // 1000000 if bps.isdigit() else 0)
        elif item.get("resourceType") == "disks" and item.get("name") == "PremiumV2_LRS":
            info = (item.get("locationInfo") or [{}])[0]
            zones, offered = set(info.get("zones") or []), True
            for r in item.get("restrictions") or []:
                if r.get("type") == "Location":
                    zones, offered = set(), False
                elif r.get("type") == "Zone":
                    zones -= set((r.get("restrictionInfo") or {}).get("zones") or [])
            facts.v2_zones = (facts.v2_zones or set()) | zones
            facts.v2_region = facts.v2_region or offered
    return facts


def v2_offered(vm: Vm, facts: RegionFacts) -> bool:
    """Whether the VM can take Premium SSD v2 disks: in its zone, or, without a zone, in its region."""
    if vm.zone:
        return vm.zone in (facts.v2_zones or set())
    return facts.v2_region and vm.region in V2_NONZONAL_REGIONS


def region_prices(region: str) -> Prices:
    """List prices of every managed-disk meter in one region, from the public Azure Retail Prices API."""
    products = " or ".join(f"productName eq '{p}'" for p in TIER_PRODUCTS + tuple(PROVISIONED_PRODUCTS))
    query = f"armRegionName eq '{region}' and priceType eq 'Consumption' and ({products})"
    url = PRICES_URL + "?" + urllib.parse.urlencode({"$filter": query})
    prices = Prices()
    while url:
        page = http_json(url)
        for item in page.get("Items") or []:
            add_price(prices, item)
        url = page.get("NextPageLink")
    return prices


def add_price(prices: Prices, item: dict) -> None:
    product, meter, unit = item.get("productName", ""), item.get("meterName", ""), item.get("unitOfMeasure", "")
    price = item.get("unitPrice")
    if not isinstance(price, (int, float)):
        return
    tier = re.fullmatch(r"([PES]\d+) (LRS|ZRS) Disk", meter)
    if tier and product in TIER_PRODUCTS and unit == "1/Month":
        prices.tiers[f"{tier.group(1)} {tier.group(2)}"] = float(price)
        return
    prefix = PROVISIONED_PRODUCTS.get(product)
    if prefix and meter.startswith(prefix) and unit in ("1 GiB/Hour", "1/Hour"):
        kind = PROVISIONED_METERS.get(meter[len(prefix):])
        if kind:
            store = prices.v2 if product == "Azure Premium SSD v2" else prices.ultra
            store.setdefault(kind, {})[float(item.get("tierMinimumUnits") or 0)] = float(price)


def graduated(units: float, tiers: Dict[float, float]) -> float:
    """Price of units on a tiered meter: each tier's price applies from its first unit to the next tier's."""
    steps = sorted(tiers.items())
    total = 0.0
    for i, (start, price) in enumerate(steps):
        end = steps[i + 1][0] if i + 1 < len(steps) else float("inf")
        if units > start:
            total += (min(units, end) - start) * price
    return total


def provisioned_monthly(meters: Dict[str, Dict[float, float]], gib: float, iops: float, mbps: float) -> Optional[float]:
    """$/month of a disk billed by provisioned GiB, IOPS and MB/s (Premium SSD v2, Ultra Disk)."""
    if not all(meters.get(k) for k in ("capacity", "iops", "mbps")):
        return None
    hourly = (graduated(gib, meters["capacity"]) + graduated(iops, meters["iops"])
              + graduated(mbps, meters["mbps"]))
    return r2(hourly * HOURS_PER_MONTH)


def billed_tier(letter: str, size_gb: int) -> Optional[str]:
    """The tier Azure bills a disk at: the smallest offered size at or above the disk's size."""
    for limit, number in (HDD_TIER_SIZES if letter == "S" else TIER_SIZES):
        if size_gb <= limit:
            return letter + number
    return None


def price_disk(d: Disk, prices: Dict[str, Optional[Prices]]) -> None:
    """Set the disk's list price per month, or the reason it has none."""
    region_price = prices.get(d.region)
    if d.cloud == "aws":
        return price_volume(d, region_price)
    family, redundancy, label = DISK_TYPES.get(d.sku.lower(), ("", "", d.sku))
    if not family:
        d.price_note = f"{d.sku or 'unknown'} disks are not priced"
        return
    if region_price is None:
        d.price_note = "price list not readable for " + d.region
        return
    if family in ("V2", "ULTRA"):
        if d.iops is None or d.mbps is None:
            d.price_note = "provisioned IOPS or MB/s not reported"
            return
        meters = region_price.v2 if family == "V2" else region_price.ultra
        d.price = provisioned_monthly(meters, d.size_gb, d.iops, d.mbps)
        d.price_note = "" if d.price is not None else f"no {label} list price in {d.region}"
        return
    tier = billed_tier(family, d.size_gb)
    names = [t[1] for t in (HDD_TIER_SIZES if family == "S" else TIER_SIZES)]
    if tier and family == "P" and re.fullmatch(r"P\d+", d.tier or "") and d.tier[1:] in names \
            and names.index(d.tier[1:]) > names.index(tier[1:]):
        tier = d.tier
    if not tier:
        d.price_note = f"{d.size_gb:,} GB is above the largest {label} tier"
        return
    monthly = region_price.tiers.get(f"{tier} {redundancy}")
    d.price = None if monthly is None else r2(monthly)
    if d.price is None:
        d.price_note = f"no list price for {tier} {redundancy} in {d.region}"


def ebs_prices(region: str) -> Prices:
    """EBS list prices in one region from the AWS Price List API (on-demand, USD), read as Amoeba reads them."""
    served: Dict[str, dict] = {}
    for family in EBS_PRICE_FAMILIES:
        terms = [{"Type": "TERM_MATCH", "Field": "regionCode", "Value": region},
                 {"Type": "TERM_MATCH", "Field": "productFamily", "Value": family}]
        out = aws(["pricing", "get-products", "--service-code", "AmazonEC2", "--format-version", "aws_v1",
                   "--filters", json.dumps(terms)], PRICE_LIST_REGION, 180) or {}
        for raw in out.get("PriceList") or []:
            add_ebs_price(served, family, json.loads(raw) if isinstance(raw, str) else raw)
    prices = Prices()
    for kind, bills in EBS_BILLS.items():
        rate = ebs_rate(served.get(kind), bills)
        if rate:
            prices.ebs[kind] = rate
    return prices


def add_ebs_price(served: Dict[str, dict], family: str, item: dict) -> None:
    """Add each on-demand USD price of one price-list product to its volume type, by the unit it bills."""
    attributes = (item.get("product") or {}).get("attributes") or {}
    kind = (attributes.get("volumeApiName") or "").lower()
    if not kind:
        return
    found = served.setdefault(kind, {"gib": [], "iops": {}, "mbps": []})
    for term in ((item.get("terms") or {}).get("OnDemand") or {}).values():
        for dim in (term.get("priceDimensions") or {}).values():
            try:
                usd = float((dim.get("pricePerUnit") or {}).get("USD"))
            except (TypeError, ValueError):
                continue
            unit = (dim.get("unit") or "").lower()
            if usd <= 0:
                continue
            if family == "Storage" and unit == "gb-mo":
                found["gib"].append(usd)
            elif family == "System Operation" and unit == "iops-mo":
                found["iops"].setdefault(iops_band(dim, attributes.get("usagetype") or ""), []).append(usd)
            elif family == "Provisioned Throughput" and unit in ("gibps-mo", "mibps-mo"):
                found["mbps"].append(usd / 1024 if unit == "gibps-mo" else usd)


def iops_band(dim: dict, usage: str) -> int:
    """The IOPS a price starts at: its begin range, else its io2 tier's start; -1 for a tier not known."""
    try:
        begin = int(float(dim.get("beginRange") or 0))
    except (TypeError, ValueError):
        begin = 0
    if begin > 0:
        return begin
    tier = re.search(r"\.tier(\d+)$", usage)
    return IO2_TIER_STARTS.get(int(tier.group(1)), -1) if tier else 0


def ebs_rate(found: Optional[dict], bills: Tuple[str, ...]) -> Optional[EbsRate]:
    """The type's rate when the price list gives one price for every unit it bills, else None: two products that
    disagree leave the unit unpriced."""
    def single(values: list) -> Optional[float]:
        return values[0] if values and all(v == values[0] for v in values) else None

    gib = single(found["gib"]) if found else None
    if gib is None:
        return None
    rate = EbsRate(gib=gib)
    if "iops" in bills:
        rate.iops = {start: single(usd) for start, usd in found["iops"].items()}
        if not rate.iops or any(start < 0 or usd is None for start, usd in rate.iops.items()):
            return None
        if "mbps" in bills:
            rate.included_iops, rate.included_mbps, rate.mbps = GP3_BASE_IOPS, GP3_BASE_MBPS, single(found["mbps"])
            if min(rate.iops) > GP3_BASE_IOPS or rate.mbps is None:
                return None
        elif min(rate.iops) != 0:
            return None
    return rate


def ebs_monthly(rate: EbsRate, gib: float, iops: Optional[int], mbps: Optional[int]) -> float:
    """$/month of one EBS volume: its GB, plus each IOPS and MB/s above what its type includes, at its band's rate."""
    usd = rate.gib * gib
    if iops and iops > rate.included_iops:
        bands = sorted(rate.iops.items())
        for i, (start, price) in enumerate(bands):
            low, high = max(start, rate.included_iops), bands[i + 1][0] if i + 1 < len(bands) else float("inf")
            if iops > low:
                usd += (min(iops, high) - low) * price
    if mbps and mbps > rate.included_mbps and rate.mbps is not None:
        usd += (mbps - rate.included_mbps) * rate.mbps
    return r2(usd)


def price_volume(d: Disk, region_price: Optional[Prices]) -> None:
    """Set the EBS volume's list price per month from its type, size, IOPS and (gp3) MB/s, or the reason it has none."""
    if region_price is None:
        d.price_note = "price list not readable for " + d.region
        return
    rate = region_price.ebs.get(d.sku.lower())
    if rate is None:
        d.price_note = ("magnetic (standard) volumes bill each I/O request; not priced" if d.sku == "standard"
                        else f"no {d.sku or 'unknown'} list price in {d.region}")
        return
    d.price = ebs_monthly(rate, d.size_gb, d.iops, d.mbps)


# ---------------------------------------------------------------- measured performance

def metric_window() -> Tuple[datetime, datetime]:
    end = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    return end - timedelta(days=METRIC_DAYS), end


def add_hour(hours: Hourly, stamp: str, ops: bool, write: bool, value: float) -> None:
    """Add one metric's figure to its hour: to the IOPS or the bytes/s, and for writes also to the write figure."""
    h = list(hours.get(stamp, (0.0, 0.0, 0.0, 0.0)))
    i = 0 if ops else 1
    h[i] += value
    if write:
        h[i + 2] += value
    hours[stamp] = (h[0], h[1], h[2], h[3])


def disk_metrics(disk: Disk) -> Hourly:
    """IOPS and bytes/s of one disk for each hour of the last 14 days: the busiest minute of the hour (Maximum),
    reads plus writes, or the hour's average where Azure Monitor gives no maximum."""
    start, end = metric_window()
    query = urllib.parse.urlencode({"api-version": METRICS_API, "metricnames": ",".join(METRICS),
                                    "timespan": f"{iso(start)}/{iso(end)}", "interval": "PT1H",
                                    "aggregation": "Average,Maximum"}, quote_via=urllib.parse.quote)
    data = rest("get", f"{disk.id}/providers/Microsoft.Insights/metrics?{query}") or {}
    hours: Hourly = {}
    for metric in data.get("value") or []:
        name = (metric.get("name") or {}).get("value") or ""
        for series in metric.get("timeseries") or []:
            for point in series.get("data") or []:
                value, stamp = point.get("maximum"), point.get("timeStamp")
                if not isinstance(value, (int, float)):
                    value = point.get("average")
                if not isinstance(value, (int, float)) or not stamp:
                    continue
                add_hour(hours, stamp, "Operations" in name, "Write" in name, value)
    return hours


def volume_metrics(disk: Disk) -> Hourly:
    """IOPS and bytes/s of one EBS volume for each hour of the last 14 days, from CloudWatch: the busiest minute of
    the hour (the Maximum of its 1-minute data points, divided by 60), reads plus writes."""
    start, end = metric_window()
    hours: Hourly = {}
    for metric, ops, write in EBS_METRICS:
        out = aws(["cloudwatch", "get-metric-statistics", "--namespace", "AWS/EBS", "--metric-name", metric,
                   "--dimensions", f"Name=VolumeId,Value={disk.id}", "--start-time", iso(start), "--end-time",
                   iso(end), "--period", "3600", "--statistics", "Maximum"], disk.region) or {}
        for point in out.get("Datapoints") or []:
            value, stamp = point.get("Maximum"), point.get("Timestamp")
            if isinstance(value, (int, float)) and stamp:
                add_hour(hours, stamp, ops, write, value / 60)
    return hours


# ---------------------------------------------------------------- inside the VM

RUN_COMMAND_STARTED: Dict[str, List[str]] = {"azure": [], "aws": []}


def read_guest(vm: Vm) -> Guest:
    """Run the command that only reads in the VM through Azure Run Command and parse what it printed."""
    RUN_COMMAND_STARTED["azure"].append(vm.id)
    out = az(["vm", "run-command", "invoke", "--ids", vm.id, "--command-id", "RunShellScript",
              "--scripts", GUEST_SCRIPT["azure"]], RUN_COMMAND_TIMEOUT) or {}
    message = "".join(str((v or {}).get("message") or "") for v in out.get("value") or [])
    if "[stdout]" in message:
        message = message.split("[stdout]", 1)[1].split("[stderr]", 1)[0]
    return parse_guest(message)


def read_instance(vm: Vm) -> Guest:
    """Run the command that only reads in the instance through AWS Systems Manager Run Command, wait for it, and
    parse what it printed. Its volumes are matched to the instance's data volumes by volume id."""
    RUN_COMMAND_STARTED["aws"].append(vm.id)
    parameters = {"commands": GUEST_SCRIPT["aws"].splitlines(), "executionTimeout": [str(SSM_EXECUTION_SECONDS)]}
    sent = aws(["ssm", "send-command", "--instance-ids", vm.id, "--document-name", SSM_DOCUMENT,
                "--comment", "Zoorik disk assessment: reads disk usage", "--timeout-seconds", str(RUN_COMMAND_TIMEOUT),
                "--parameters", json.dumps(parameters)], vm.region) or {}
    command = (sent.get("Command") or {}).get("CommandId")
    if not command:
        raise Failed("Systems Manager did not start the command")
    deadline = time.monotonic() + RUN_COMMAND_TIMEOUT
    while time.monotonic() < deadline:
        time.sleep(SSM_POLL_SECONDS)
        try:
            out = aws(["ssm", "get-command-invocation", "--command-id", command, "--instance-id", vm.id],
                      vm.region) or {}
        except Failed as e:
            if str(e) == reason("invocationdoesnotexist"):
                continue
            raise
        status = out.get("Status") or ""
        if status == "Success":
            g = parse_guest(out.get("StandardOutputContent") or "")
            g.luns = {dd.lun: g.volumes[dd.disk.id] for dd in vm.data if dd.disk and dd.disk.id in g.volumes}
            return g
        if status not in ("Pending", "InProgress", "Delayed"):
            raise Failed(f"the command ended as {out.get('StatusDetails') or status or 'unknown'} on the instance")
    raise Failed("no answer in time")


def unescape_lsblk(text: str) -> str:
    return re.sub(r"\\x([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), text)


def parse_guest(text: str) -> Guest:
    lines = [line.strip("\r") for line in text.splitlines()]
    if "S" not in lines or "E" not in lines:
        raise Failed("the command's output came back incomplete")
    g = Guest()
    for line in lines:
        tag, _, rest_of_line = line.partition(" ")
        if tag == "K":
            g.kernel = rest_of_line.strip()
        elif tag == "B":
            g.btrfs = rest_of_line.strip()
        elif tag == "L":
            lun, _, mounts = rest_of_line.partition(" ")
            if lun.isdigit():
                g.luns[int(lun)] = [unescape_lsblk(m) for m in mounts.split() if m.startswith("/")]
        elif tag == "V":
            volume, _, mounts = rest_of_line.partition(" ")
            g.volumes[volume] = [unescape_lsblk(m) for m in mounts.split() if m.startswith("/")]
        elif tag == "D":
            used, _, mount = rest_of_line.partition(" ")
            if used.isdigit() and mount:
                g.mounts[mount] = int(used)
    if not g.kernel or g.btrfs not in ("loaded", "module", "absent"):
        raise Failed("the command's output was not readable")
    return g


def kernel_version(kernel: str) -> Optional[Tuple[int, int]]:
    match = re.match(r"(\d+)\.(\d+)", kernel or "")
    return (int(match.group(1)), int(match.group(2))) if match else None


# ---------------------------------------------------------------- pool sizing (as Zoorik Amoeba plans it)

def ceil_div(numerator: int, divisor: int) -> int:
    return numerator if divisor <= 0 else -(-numerator // divisor)


def half_up(value: float) -> int:
    return int(math.floor(value + 0.5))


def pool_size_gib(used_gib: int) -> int:
    """The pool holds today's data plus headroom: 20% of it, at least 50 GB."""
    return math.ceil(used_gib + max(HEADROOM_FRACTION * used_gib, HEADROOM_MIN_GIB))


def tier_up(gib: int, v2: bool) -> int:
    """A Premium SSD v2 disk takes any size; a Premium SSD disk the smallest Premium SSD size at or above it."""
    return gib if v2 else next((t for t in V1_TIER_GIB if t >= gib), V1_TIER_GIB[-1])


def tier_down(gib: int, v2: bool) -> int:
    """A Premium SSD v2 disk takes any size; a Premium SSD disk the largest Premium SSD size at or below it."""
    return gib if v2 else max((t for t in V1_TIER_GIB if t <= gib), default=V1_TIER_GIB[0])


def elastic_floor(v2: bool) -> int:
    return ELASTIC_FLOOR_V2_GIB if v2 else ELASTIC_FLOOR_V1_GIB


def evacuation_cap(uncached_mbps: int) -> Optional[int]:
    """The largest disk the VM can empty in the evacuation window, in GiB; None when its throughput is not known."""
    if uncached_mbps <= 0:
        return None
    return math.floor(EVAC_WINDOW_MINUTES * 60.0 * EVAC_SHARE * uncached_mbps / (2.0 * ELASTIC_FILL_FRACTION * 1024.0))


def elastic_cap_gib(uncached_mbps: int, floor: int) -> int:
    """The largest elastic disk the VM can empty in the evacuation window."""
    cap = evacuation_cap(uncached_mbps)
    return ELASTIC_CAP_GIB if cap is None else max(floor, min(ELASTIC_CAP_GIB, cap))


def clamp_elastics(count: int) -> int:
    return max(MIN_ELASTICS, min(MAX_ELASTICS, count))


def full_geometry(pool: int, used: int, uncached_mbps: int, v2: bool) -> List[int]:
    """Two anchors and three to six equal elastics (data member sizes): the shape when slots allow."""
    floor = elastic_floor(v2)
    quantum = tier_up(max(floor, min(elastic_cap_gib(uncached_mbps, floor), pool // ELASTIC_DIVISOR)), v2)
    if pool < ANCHOR_POOL_THRESHOLD_GIB:
        quantum = max(quantum, tier_up(ceil_div(pool, MAX_ELASTICS), v2))
        return [quantum] * clamp_elastics(ceil_div(pool, quantum))
    by_band = half_up(pool * ANCHOR_CAP_FRACTION)
    joint = min(by_band, used) if used > 0 else by_band
    anchor = tier_down(max(1, min(joint // ANCHOR_COUNT, ANCHOR_MAX_GIB)), v2)
    remaining = max(0, pool - anchor * ANCHOR_COUNT)
    return [anchor] * ANCHOR_COUNT + [quantum] * clamp_elastics(ceil_div(remaining, quantum))


def covering_quantum(span: int, count: int, v2: bool) -> int:
    floor = elastic_floor(v2)
    return floor if span <= 0 else max(floor, tier_up(ceil_div(span, count), v2))


def degraded_geometry(pool: int, uncached_mbps: int, member_slots: int,
                      v2: bool) -> Optional[Tuple[List[int], int]]:
    """One anchor and up to three elastics: the shape when the VM has fewer free slots."""
    small = 1 if v2 else 0
    elastics = min(DEGRADED_MAX_ELASTICS, member_slots - small - DEGRADED_ANCHORS)
    if elastics < 1:
        small = 0
        elastics = min(DEGRADED_MAX_ELASTICS, member_slots - DEGRADED_ANCHORS)
    if elastics < 1:
        return None
    ceiling = tier_down(elastic_cap_gib(uncached_mbps, elastic_floor(v2)), v2)
    anchor = tier_down(min(half_up(pool * ANCHOR_CAP_FRACTION), ANCHOR_MAX_GIB), v2)
    quantum = covering_quantum(pool - anchor, elastics, v2)
    if quantum > ceiling:
        anchor = min(ANCHOR_MAX_GIB, tier_up(max(1, pool - elastics * ceiling), v2))
        quantum = covering_quantum(pool - anchor, elastics, v2)
    if anchor <= 0 or quantum <= 0 or quantum > ceiling or anchor + quantum * elastics < pool:
        return None
    return [anchor] + [quantum] * elastics, small


def pool_members(pool: int, used: int, slots: int, attached: int, uncached_mbps: int,
                 v2: bool) -> Optional[Tuple[List[int], int]]:
    """(data member sizes in GiB, number of small members), or None when the VM cannot host the pool.

    The pool is built beside today's disks, so its members must fit the slots those leave free. Only a
    Premium SSD v2 pool has the small member."""
    full = full_geometry(pool, used, uncached_mbps, v2)
    small = 1 if v2 else 0
    member_slots = max(0, slots - max(0, attached) - RESERVED_SLOTS)
    if len(full) + small <= member_slots:
        return full, small
    if slots < MIN_VM_SLOTS or member_slots < MIN_POOL_SLOTS:
        return None
    return degraded_geometry(pool, uncached_mbps, member_slots, v2)


def member_iops(demand: float, members: int, size: int, vm_iops: int) -> int:
    count = max(1, members)
    iops = max(V2_MIN_IOPS, math.ceil(DEMAND_FACTOR * max(0.0, demand) / count))
    iops = min(iops, max(V2_MIN_IOPS, size * V2_MAX_IOPS_PER_GIB))
    if vm_iops > 0:
        iops = min(iops, max(V2_MIN_IOPS, vm_iops // count))
    return iops


def member_mbps(demand: float, members: int, iops: int, vm_mbps: int) -> int:
    count = max(1, members)
    mbps = max(V2_MIN_MBPS, math.ceil(DEMAND_FACTOR * max(0.0, demand) / count))
    mbps = min(mbps, max(V2_MIN_MBPS, min(V2_MAX_MBPS, int(iops * V2_MBPS_PER_IOPS))))
    if vm_mbps > 0:
        mbps = min(mbps, max(V2_MIN_MBPS, vm_mbps // count))
    return mbps


def hold_gib(used_bytes: int) -> int:
    """The size Amoeba holds a new AWS pool at: its data plus btrfs's 3%, at most 90% full (the band's high edge)
    and below the urgent level (95%), with at least 10 GiB free. One reading has no growth or burst to add."""
    held = math.ceil(used_bytes * (1 + BTRFS_OVERHEAD))
    return max(1, math.ceil(max(held / BAND_HIGH, held / URGENT_USED, held + FREE_FLOOR_GIB * GIB) / GIB))


def gp3_need(peak: Tuple[float, float, float, float], members: int) -> Tuple[int, int]:
    """IOPS and MB/s one gp3 volume needs: the peak x 1.3 shared across the pool, at least the write peak x 1.3,
    at least its baseline, and IOPS enough for its MB/s."""
    def margin(figure: float) -> int:
        return math.ceil(DEMAND_FACTOR * figure)

    iops = max(GP3_BASE_IOPS, margin(peak[2]), ceil_div(margin(peak[0]), members))
    mbps = max(GP3_BASE_MBPS, margin(peak[3]), ceil_div(margin(peak[1]), members))
    return max(iops, math.ceil(mbps / GP3_MBPS_PER_IOPS)), mbps


def gp3_pool(vm: Vm, used_bytes: int, peak: Tuple[float, float, float, float]) -> Optional[Tuple[int, int, int, int]]:
    """Amoeba's gp3 pool for the instance: (volumes, GiB, IOPS and MB/s of each), or None when its free slots, one
    kept spare, cannot hold it. Equal volumes, at least 2, each no larger than the instance can empty in the
    evacuation window unless the slots need them larger. Raises Overloaded when the volumes cannot carry its load."""
    member_slots = (vm.free_slots or 0) - RESERVED_SLOTS
    if member_slots < 1:
        return None
    target = hold_gib(used_bytes)
    count = max(MIN_MEMBERS, ceil_div(target, max(evacuation_cap(vm.uncached_mbps) or 0,
                                                  ceil_div(target, member_slots))))
    if count > member_slots:
        return None
    size = ceil_div(target, count)
    need_iops, need_mbps = gp3_need(peak, count)
    top_iops = max(GP3_BASE_IOPS, min(GP3_MAX_IOPS, size * GP3_IOPS_PER_GIB))
    top_mbps = max(GP3_BASE_MBPS, min(GP3_MAX_MBPS, math.floor(top_iops * GP3_MBPS_PER_IOPS)))
    if size > GP3_MAX_GIB:
        raise Failed("too much data for one pool: not estimated")
    if need_iops > top_iops or need_mbps > top_mbps:
        raise Overloaded("too busy for a pool of this size: not estimated")
    iops = min(need_iops, top_iops)
    if vm.uncached_iops > 0:
        iops = min(iops, max(GP3_BASE_IOPS, vm.uncached_iops // count))
    mbps = min(need_mbps, top_mbps)
    if vm.uncached_mbps > 0:
        mbps = min(mbps, max(GP3_BASE_MBPS, vm.uncached_mbps // count))
    return count, size, iops, min(mbps, max(GP3_BASE_MBPS, math.floor(iops * GP3_MBPS_PER_IOPS)))


# ---------------------------------------------------------------- assessment

def screen(vm: Vm) -> List[str]:
    """Reasons Amoeba cannot take the VM, from what the cloud reports (before looking inside it)."""
    red = []
    if not vm.linux:
        red.append(f"{vm.os_type or 'Non-Linux'} VM: not estimated by this assessment yet")
    if vm.managed_by:
        red.append(f"run by another Azure service ({re.sub(r'^Microsoft[.]', '', vm.managed_by.split('/')[0])}): "
                   "Amoeba leaves it to that service")
    if vm.cloud == "azure" and vm.slots is not None and vm.slots < MIN_VM_SLOTS:
        red.append(f"too few data-disk slots ({vm.slots}): Amoeba needs {MIN_VM_SLOTS} or more")
    if vm.nvme is False:
        red.append(f"{vm.size} is an older instance type: Amoeba needs a Nitro instance type")
    return red


def data_mounts(vm: Vm) -> Dict[str, List[int]]:
    """Mounts that sit on the VM's data disks, with the LUNs under each."""
    mounts: Dict[str, List[int]] = {}
    known = {dd.lun for dd in vm.data}
    for lun, points in (vm.guest.luns if vm.guest else {}).items():
        if lun in known:
            for point in points:
                mounts.setdefault(point, []).append(lun)
    return mounts


def poolable(vm: Vm) -> Dict[str, List[int]]:
    """The data mounts a pool can take: those on no shared disk."""
    shared = {dd.lun for dd in vm.data if dd.disk and dd.disk.shared}
    return {m: luns for m, luns in data_mounts(vm).items() if not shared & set(luns)}


def pooled_peak(pooled: List[DataDisk]) -> Tuple[float, float, float, float]:
    """The measured peak of the disks the pool replaces, summed across them per hour: (IOPS, MB/s, write IOPS,
    write MB/s)."""
    hours: Hourly = {}
    for dd in pooled:
        for stamp in dd.disk.hourly:
            add = dd.disk.hourly[stamp]
            total = hours.get(stamp, (0.0, 0.0, 0.0, 0.0))
            hours[stamp] = (total[0] + add[0], total[1] + add[1], total[2] + add[2], total[3] + add[3])
    mb = MB[pooled[0].disk.cloud]
    return (max(v[0] for v in hours.values()), max(v[1] for v in hours.values()) / mb,
            max(v[2] for v in hours.values()), max(v[3] for v in hours.values()) / mb)


def pool_cost_v2(region_price: Prices, vm: Vm, members: List[int], peak_iops: float, peak_mbps: float) -> float:
    """$/month of a Premium SSD v2 pool: each disk's size, plus IOPS and MB/s above the included baseline
    where the measured peak needs them."""
    cost = 0.0
    for size in members:
        iops = member_iops(peak_iops, len(members), size, vm.uncached_iops)
        price = provisioned_monthly(region_price.v2, size, iops,
                                    member_mbps(peak_mbps, len(members), iops, vm.uncached_mbps))
        if price is None:
            raise Failed(f"no Premium SSD v2 list price in {vm.region}")
        cost += price
    return cost


def pool_cost_v1(region_price: Prices, vm: Vm, members: List[int]) -> float:
    """$/month of a Premium SSD pool: each disk at the list price of its Premium SSD tier."""
    cost = 0.0
    for size in members:
        tier = billed_tier("P", size)
        monthly = region_price.tiers.get(f"{tier} LRS")
        if monthly is None:
            raise Failed(f"no Premium SSD list price for {tier} LRS in {vm.region}")
        cost += r2(monthly)
    return cost


def v1_shortfall(vm: Vm, members: List[int], peak_iops: float, peak_mbps: float) -> str:
    """Why a Premium SSD pool cannot carry the measured peak x 1.3 (or what the VM can drive, if less); else ''."""
    need_iops, need_mbps = DEMAND_FACTOR * peak_iops, DEMAND_FACTOR * peak_mbps
    if vm.uncached_iops > 0:
        need_iops = min(need_iops, vm.uncached_iops)
    if vm.uncached_mbps > 0:
        need_mbps = min(need_mbps, vm.uncached_mbps)
    has_iops = sum(V1_TIER_PERFORMANCE[tier_up(size, False)][0] for size in members)
    has_mbps = sum(V1_TIER_PERFORMANCE[tier_up(size, False)][1] for size in members)
    if need_iops <= has_iops and need_mbps <= has_mbps:
        return ""
    return "too busy for a pool of this size: not estimated"


def azure_pool(vm: Vm, g: Guest, mounts: Dict[str, List[int]], peak: Tuple[float, float, float, float],
               region_price: Prices) -> Optional[Tuple[int, float]]:
    """(GB, $/month) of the Premium SSD v2 or Premium SSD pool Amoeba builds on Azure; None when the free slots
    cannot hold it. Raises Failed (Overloaded when it cannot carry the load) with the reason when it is not
    estimated."""
    used = sum(max(1, math.ceil(g.mounts[m] / GIB)) for m in mounts)
    pool = pool_size_gib(used)
    layout = pool_members(pool, used, vm.slots, len(vm.data), vm.uncached_mbps, vm.pool_v2)
    if layout is None:
        return None
    data_members, small = layout
    if sum(data_members) < pool:
        raise Failed("too much data for one pool: not estimated")
    members = data_members + [SMALL_MEMBER_GIB] * small
    if not vm.pool_v2:
        short = v1_shortfall(vm, members, peak[0], peak[1])
        if short:
            raise Overloaded(short)
    cost = (pool_cost_v2(region_price, vm, members, peak[0], peak[1]) if vm.pool_v2
            else pool_cost_v1(region_price, vm, members))
    return sum(members), cost


def aws_pool(vm: Vm, g: Guest, mounts: Dict[str, List[int]], peak: Tuple[float, float, float, float],
             region_price: Prices) -> Optional[Tuple[int, float]]:
    """(GB, $/month) of the gp3 pool Amoeba builds on AWS; None when the free slots cannot hold it. Raises Failed
    (Overloaded when it cannot carry the load) with the reason when it is not estimated."""
    layout = gp3_pool(vm, sum(g.mounts[m] for m in mounts), peak)
    if layout is None:
        return None
    rate = region_price.ebs.get(GP3)
    if rate is None:
        raise Failed(f"no gp3 list price in {vm.region}")
    count, size, iops, mbps = layout
    return count * size, count * ebs_monthly(rate, size, iops, mbps)


def under(vm: Vm, mounts: Dict[str, List[int]]) -> List[DataDisk]:
    """The VM's data disks under the mounts."""
    luns = {lun for points in mounts.values() for lun in points}
    return [dd for dd in vm.data if dd.lun in luns]


def measure(vm: Vm, g: Guest, mounts: Dict[str, List[int]]) -> None:
    """Used GB on the mounts the pool takes."""
    vm.used_gb = sum(g.mounts[m] for m in mounts) / GIB


def plan_pool(vm: Vm, g: Guest, mounts: Dict[str, List[int]],
              region_price: Prices) -> Tuple[Optional[Tuple[int, float]], Dict[str, List[int]]]:
    """The pool and the mounts it takes. As Amoeba plans, while the pool cannot carry the load, the most demanding
    mount (by peak IOPS, then MB/s) stays on its disks; when none is left, the first reason stands."""
    size = aws_pool if vm.cloud == "aws" else azure_pool
    kept = dict(mounts)
    first: Optional[Overloaded] = None
    while True:
        try:
            return size(vm, g, kept, pooled_peak(under(vm, kept)), region_price), kept
        except Overloaded as e:
            first = first or e
            if len(kept) == 1:
                raise first from None
            del kept[max(kept, key=lambda m: pooled_peak(under(vm, {m: kept[m]}))[:2])]


def assess(vm: Vm, prices: Dict[str, Optional[Prices]]) -> None:
    """Used space and the saving for one VM, or the reasons it has none."""
    red, amber, notes = screen(vm), [], []
    g, unread = vm.guest, vm.guest_note
    aws_vm = vm.cloud == "aws"
    if g and not g.luns:
        g = None
        unread = vm.guest_note = ("its volumes could not be matched inside the instance" if aws_vm
                                  else "its data disks could not be matched inside the VM")
    on_data, mounts = (data_mounts(vm), poolable(vm)) if g else ({}, {})
    if g:
        version = kernel_version(g.kernel)
        if g.btrfs not in ("loaded", "module"):
            red.append(f"no btrfs in its Linux kernel ({g.kernel}): Amoeba needs btrfs")
        if version and version < KERNEL_FLOOR:
            amber.append(f"needs work first: upgrade its Linux kernel ({g.kernel}) to 5.14 or later")
        if not mounts:
            red.append("its data disks are shared with other VMs: Amoeba leaves them as they are" if on_data
                       else "no data disk is mounted inside the VM")
    if any(dd.disk and dd.disk.ade for dd in vm.data):
        amber.append("needs work first: move from Azure Disk Encryption to server-side encryption")

    unpriced = [dd for dd in vm.data if not dd.disk or dd.disk.price is None]
    vm.current = None if unpriced else r2(sum(dd.disk.price for dd in vm.data))
    if unpriced:
        vm.price_note = unpriced[0].note or unpriced[0].disk.price_note

    pooled = sorted({lun for luns in mounts.values() for lun in luns})
    pooled_disks = [dd for dd in vm.data if dd.lun in pooled]
    missing = [m for m in mounts if m not in g.mounts] if g else []
    if g and mounts and not missing:
        measure(vm, g, mounts)
    elif missing:
        notes.append(f"used space on {missing[0]} not reported")

    def finish(why: str = "") -> None:
        vm.reasons = red + amber + ([why] if why else []) + notes

    if red:
        return finish()
    if not g:
        return finish(f"disk usage not read: {unread}" if unread else "")
    if missing or vm.used_gb is None:
        return finish()
    if vm.slots is None:
        return finish(f"VM size limits not readable: {vm.facts_note or 'size not listed'}")
    if any(dd.disk is None or dd.disk.price is None for dd in pooled_disks):
        return finish(NO_PRICE)
    if any(dd.disk.hourly is None for dd in pooled_disks):
        bad = next(dd.disk for dd in pooled_disks if dd.disk.hourly is None)
        return finish(f"disk activity not read: {bad.metric_note or 'no metric data'}")

    try:
        sized, kept = plan_pool(vm, g, mounts, prices.get(vm.region) or Prices())
    except Failed as e:
        return finish(str(e))
    if sized is None:
        return finish("too few free data-disk slots to add the pool beside today's disks")
    if len(kept) < len(mounts):
        measure(vm, g, kept)
        pooled_disks = under(vm, kept)
    vm.pool_gb, vm.pool_cost = sized[0], r2(sized[1])
    today = r2(sum(dd.disk.price for dd in pooled_disks))
    if today - vm.pool_cost <= 0:
        return finish(f"no saving: the pool would cost {usd(half_up(vm.pool_cost))} a month, no less than these "
                      f"disks ({usd(half_up(today))})")
    vm.pooled, vm.today, vm.saving = pooled_disks, today, r2(today - vm.pool_cost)
    finish()


# ---------------------------------------------------------------- report

def plural(n: int, word: str) -> str:
    return f"{n:,} {word}" + ("" if n == 1 else "s")


def usd(v: int) -> str:
    return f"${v:,}"


def less(v: int) -> str:
    """A saving in whole dollars: minus and the dollars, or $0."""
    return "\u2212" + usd(v) if v > 0 else "$0"


def percent(part: int, whole: int) -> int:
    return half_up(100 * part / whole) if whole > 0 else 0


def space(gib: float) -> str:
    """Disk space as the report writes it: GB below 1,000, then TB with one decimal."""
    gib = half_up(gib)
    return f"{gib / 1000:.1f} TB" if gib >= 1000 else f"{gib} GB"


def vm_size(vm: Vm) -> str:
    """The VM size as people say it: Standard_E8s_v5 is E8s v5."""
    return vm.size if vm.cloud == "aws" else re.sub(r"^Standard_", "", vm.size).replace("_", " ")


def disk_sizes(disks: List[DataDisk]) -> str:
    """The disks by size, largest first: 4 x 1.0 TB, or 2 x 512 GB + 1 x 128 GB."""
    sizes: Dict[int, int] = {}
    for dd in disks:
        sizes[dd.size_gb] = sizes.get(dd.size_gb, 0) + 1
    return " + ".join(f"{n} \xd7 {space(size)}" for size, n in sorted(sizes.items(), reverse=True))


def space_bar(paid: float, used: float, after: float):
    """Art: disk space paid for today (grey track), used (blue) and the dashed outline of the size after."""
    def art(pdf, x, y, w):
        if pdf:
            top = max(paid, after)
            pdf.rect(x, y, w * paid / top, 7.5, RULE, r=3.75)
            pdf.rect(x, y, w * used / top, 7.5, BLUE, r=3.75)
            pdf.dashed(x, y - 1.5, w * after / top, 10.5, 4.5)
        return 7.5
    return art


def staying(v: Vm) -> List[DataDisk]:
    """The VM's other priced data disks, which stay as they are and cost the same today and after. A shared disk
    belongs to more than one VM and is left out."""
    pooled = {dd.lun for dd in v.pooled}
    return [dd for dd in v.data if dd.lun not in pooled and dd.disk and not dd.disk.shared and dd.disk.price is not None]


@dataclass
class Tally:
    """Money in whole dollars and disk space in GB, of all the data disks of VMs in the saving: each VM's today and
    after cost is rounded first, so every sum adds up."""
    today: int = 0
    after: int = 0
    paid: int = 0
    pool: int = 0
    disks: int = 0

    @property
    def saving(self) -> int:
        return self.today - self.after


def tally(vms: List[Vm]) -> Tally:
    t = Tally()
    for v in vms:
        stay = staying(v)
        same = half_up(sum(dd.disk.price for dd in stay))
        t.today += half_up(v.today) + same
        t.after += half_up(v.pool_cost) + same
        t.paid += sum(dd.size_gb for dd in v.pooled + stay)
        t.pool += v.pool_gb + sum(dd.size_gb for dd in stay)
        t.disks += len(v.pooled) + len(stay)
    return t


def where(vms: List[Vm]) -> str:
    """The VMs' region, or how many regions they are in."""
    regions = {v.region for v in vms}
    return regions.pop() if len(regions) == 1 else f"{len(regions)} regions"


def why(vm: Vm, money: bool) -> str:
    """The VM's reasons as one line, capitalized unless it opens with a name such as t2.large. A report with list
    prices says why a VM's cost is unknown; one without names the reason once, above the table."""
    reasons = [r for r in vm.reasons if not money or r != NO_PRICE]
    if money and vm.price_note:
        reasons.append(f"today's cost unknown: {vm.price_note}")
    text = "; ".join(reasons)
    return text[:1].upper() + text[1:] if re.match(r"[a-z']+ ", text) else text


def report(scans: List[Scan], vms: List[Vm], now: datetime) -> Tuple[bytes, str]:
    """The PDF report on the VMs with data disks, and the terminal's line about its saving."""
    names = {s.id: s.name for scan in scans if scan.cloud == "azure" for s in scan.scopes}
    accounts: Dict[Tuple[str, str], List[Vm]] = {}
    for v in vms:
        accounts.setdefault((v.cloud, v.subscription), []).append(v)
    clouds = [c for c in CLOUD_NAMES if any(cloud == c for cloud, _ in accounts)]
    date = f"{now.day} {now:%B %Y}"
    rep = Report("Block storage cost assessment", f"{' and '.join(CLOUD_NAMES[c] for c in clouds)} \xb7 "
                 f"{plural(len(accounts), 'account')} \xb7 {date}", date,
                 f"Zoorik cost assessment \xb7 {plural(len(accounts), 'account')} \xb7 {date}")
    in_saving = sorted((v for v in vms if v.saving), key=lambda v: (-tally([v]).saving, v.name))
    money = any(v.current is not None or v.saving for v in vms)
    left = sorted((v for v in vms if not v.saving), key=lambda v: (v.current is None, -(v.current or 0), v.name))
    section = 1
    if in_saving:
        total = tally(in_saving)
        pct = percent(total.saving, total.today)
        groups: Dict[Tuple[str, str], List[Vm]] = {}
        for v in in_saving:
            groups.setdefault((v.cloud, v.subscription), []).append(v)
        rows = sorted(((names.get(sub, sub), cloud, group, tally(group)) for (cloud, sub), group in groups.items()),
                      key=lambda r: (-r[3].saving, r[0]))
        tb = total.paid >= 1000
        counts = [sum(cloud == c for cloud, _ in accounts) for c in clouds]
        split = " \xb7 ".join(f"{n} {CLOUD_NAMES[c]}" if len(clouds) > 1 and max(counts) > 1 else CLOUD_NAMES[c]
                              for c, n in zip(clouds, counts))
        rep.add(headline(["Reduce your disk bill by ", (f"{pct}%", True, GREEN_INK)]))
        rep.add(lede([f"{plural(len(in_saving), 'VM')}, {plural(total.disks, 'data disk')}: ",
                      (usd(total.today) + " a month", True), " today, ", (usd(total.after), True),
                      " after Amoeba rightsizes them."]), 3 * MM)
        rep.add(Kpis([(usd(total.saving), f"saved every month,\nabout {usd(12 * total.saving)} a year", GREEN_INK),
                      (f"\u2212{pct}%" if pct else "0%", "disk bill,\nthese VMs", GREEN_INK),
                      (f"{total.paid / 1000:.1f} \u2192 {total.pool / 1000:.1f}" if tb
                       else f"{total.paid:,} \u2192 {total.pool:,}", f"disk space you\npay for, {'TB' if tb else 'GB'}",
                       INK),
                      (str(len(accounts)), ("accounts" if len(accounts) > 1 else "account") + "\n" + split, INK)]),
                6 * MM)
        if len(rows) > 1:
            top = max(t.today for _, _, _, t in rows)
            # Today and After widen to fit the total, the widest figure, and the Cost column gives up the room.
            cols = [.2382, .1934, .2767, .0924, .0736, .1257]
            for k, figure in ((3, total.today), (4, total.after)):
                extra = max(0.0, (text_width(usd(figure), 9, True) + 9) / (WIDTH - 22.5) - cols[k])
                cols[k], cols[2] = cols[k] + extra, cols[2] - extra
            rep.add(Card(list(zip(cols, "lrlrrr")), ["Account", "Disk space", "Cost", "Today", "After", "Saving"],
                         [[Cell(name, f"{CLOUD_NAMES[cloud]} \xb7 {where(group)} \xb7 {plural(len(group), 'VM')}",
                                bold=True), f"{space(t.paid)} \u2192 {space(t.pool)}",
                           Cell(art=pair_bars(t.today, t.after, top)), usd(t.today), usd(t.after),
                           Cell(less(t.saving), f"{percent(t.saving, t.today)}% of its bill", True, GREEN_INK)]
                          for name, cloud, group, t in rows],
                         [f"All {len(rows)} accounts" if len(rows) == len(accounts) else "Total",
                          f"{space(total.paid)} \u2192 {space(total.pool)}", "", usd(total.today), usd(total.after),
                          Cell(less(total.saving), color=GREEN_INK)],
                         title="Savings by account", aside="a month, at list prices",
                         legend=[("Today", SLATE), ("After Amoeba", BLUE)]), GAP)
        rep.add(Section(section, "Savings by account and VM"), GAP, keep=True)
        rep.add(Legend([("Disk space you pay for today", RULE), ("Used", BLUE),
                        ("After Amoeba rightsizes", MINT, 10.5, 5.25, True)]), SEC_GAP, keep=True)
        for i, (name, cloud, group, t) in enumerate(rows):
            lines = []
            for v in group:
                vt = tally([v])
                lines.append([Cell(v.name, f"{vm_size(v)} \xb7 {disk_sizes(v.pooled + staying(v))}", bold=True),
                              Cell(art=space_bar(vt.paid, v.used_gb, vt.pool)),
                              Cell(space(v.used_gb) + " used", f"{space(vt.paid)} \u2192 {space(vt.pool)}"),
                              usd(vt.today), usd(vt.after),
                              Cell(less(vt.saving), bold=True, color=GREEN_INK) if vt.saving else "$0"])
            meta = f"{CLOUD_NAMES[cloud]} \xb7 {where(group)} \xb7 {plural(len(group), 'VM')} \xb7 {space(t.paid)} " \
                   f"\u2192 {space(t.pool)}"
            rep.add(Card([(.24, "l"), (.26, "l"), (.16, "r"), (.11, "r"), (.11, "r"), (.12, "r")],
                         ["VM", "Disk space", "Used\npaid for \u2192 after", "Today", "After", "Saving"], lines,
                         name=name, meta=meta, pill=f"{less(t.saving)} a month \xb7 {percent(t.saving, t.today)}%"),
                    CARD_GAP if i else SEC_GAP)
        section += 1
        line = f"{usd(total.saving)} a month on {plural(len(in_saving), 'VM')}, {pct}% of their disk bill"
    else:
        disks = list({dd.disk.id if dd.disk else (v.id, dd.lun): dd for v in vms for dd in v.data}.values())
        known = all(dd.disk and dd.disk.price is not None for dd in disks)
        rep.add(headline("No saving estimated yet"))
        rep.add(lede([f"{plural(len(vms), 'VM')}, {plural(len(disks), 'data disk')}: ",
                      (usd(half_up(sum(dd.disk.price for dd in disks))) + " a month", True) if known
                      else (space(sum(dd.size_gb for dd in disks)), True),
                      (" today." if known else " of disk space you pay for.") + " The reasons are below."]), 3 * MM)
        if not money:
            rep.add(Text("No list prices: " + "; ".join(sorted({v.price_note for v in vms if v.price_note})) + ".",
                         8.25, MUTED))
        line = "none yet; the report gives the reason for each VM"
    if left:
        rep.add(Section(section, "Not in the saving yet"), GAP, keep=True)
        rep.add(Card([(.2115, "l"), (.6883, "l"), (.1002, "r")] if money else [(.2115, "l"), (.7885, "l")],
                     ["VM", "Why", "Today"] if money else ["VM", "Why"],
                     [[Cell(v.name, names.get(v.subscription, v.subscription), bold=True), why(v, money)]
                      + ([Cell("not priced", color=MUTED) if v.current is None else usd(half_up(v.current))] if money
                         else []) for v in left]), SEC_GAP)
    rep.add(NextSteps("Next steps", [
        "1. Email this PDF to ", (SEND_TO, True, WHITE, "mailto:" + SEND_TO),
        ".  2. Book 30 minutes: ", ("cal.com/zoorik/talk", True, WHITE, "https://cal.com/zoorik/talk"),
        ".   3. Connect Amoeba to your accounts and see these numbers live in the Amoeba console."]), GAP, keep=True)
    rep.add(fine(("Data disks only, at public list prices. An estimate, not a quote." if money else "Data disks only.")
                 + " The script read your accounts and sent nothing."), SEC_GAP)
    return rep.pdf(), line


# ---------------------------------------------------------------- pdf writer

MM, PX = 72 / 25.4, 0.75
PAGE_W, PAGE_H, SIDE = 210 * MM, 297 * MM, 16 * MM
WIDTH = PAGE_W - 2 * SIDE
GAP, CARD_GAP, SEC_GAP = 7 * MM, 4 * MM, 3 * MM
INK, BODY, MUTED, RULE, RULE2, PANEL = "#0F1222", "#3A3F55", "#6B7086", "#E4E7F2", "#EFF1F8", "#F6F7FB"
NAVY, DEEP, BONE, LILAC, HAZE, UNDERLINE = "#050B3D", "#070F52", "#FAF7F3", "#D9D6EE", "#D6DAF5", "#8E98EB"
BLUE, GREEN, GREEN_INK, MINT, SLATE, WHITE = "#3B5BF5", "#12A97A", "#117D60", "#E3F5EF", "#5B6396", "#FFFFFF"

# Adobe AFM advance widths of WinAnsi codes 32-255, in 1/1000 em
_HELVETICA = [int(w) for w in (
    "278 278 355 556 556 889 667 191 333 333 389 584 278 333 278 278 556 556 556 556 556 556 556 556 556 556 "
    "278 278 584 584 584 556 1015 667 667 722 722 667 611 778 722 278 500 667 556 833 722 778 667 778 722 667 "
    "611 722 667 944 667 667 611 278 278 278 469 556 333 556 556 500 556 556 278 556 556 222 222 500 222 833 "
    "556 556 556 556 333 500 278 556 500 722 500 500 500 334 260 334 584 350 556 350 222 556 333 1000 556 556 "
    "333 1000 667 333 1000 350 611 350 350 222 222 333 333 350 556 1000 333 1000 500 333 944 350 500 667 278 "
    "333 556 556 556 556 260 556 333 737 370 556 584 333 737 333 400 584 333 333 333 556 537 278 333 333 365 "
    "556 834 834 834 611 667 667 667 667 667 667 1000 722 667 667 667 667 278 278 278 278 722 722 778 778 778 "
    "778 778 584 778 722 722 722 722 667 667 611 556 556 556 556 556 556 889 500 556 556 556 556 278 278 278 "
    "278 556 556 556 556 556 556 556 584 611 556 556 556 556 500 556 500").split()]
_HELVETICA_BOLD = [int(w) for w in (
    "278 333 474 556 556 889 722 238 333 333 389 584 278 333 278 278 556 556 556 556 556 556 556 556 556 556 "
    "333 333 584 584 584 611 975 722 722 722 722 667 611 778 722 278 556 722 611 833 722 778 667 778 722 667 "
    "611 722 667 944 667 667 611 333 278 333 584 556 333 556 611 556 611 556 333 611 611 278 278 556 278 889 "
    "611 611 611 611 389 556 333 611 556 778 556 556 500 389 280 389 584 350 556 350 278 556 500 1000 556 556 "
    "333 1000 667 333 1000 350 611 350 350 278 278 500 500 350 556 1000 333 1000 556 333 944 350 500 667 278 "
    "333 556 556 556 556 280 556 333 737 370 556 584 333 737 333 400 584 333 333 333 611 556 278 333 333 365 "
    "556 834 834 834 611 722 722 722 722 722 722 1000 722 667 667 667 667 278 278 278 278 722 722 778 778 778 "
    "778 778 584 778 722 722 722 722 667 667 611 556 556 556 556 556 556 889 556 556 556 556 556 278 278 278 "
    "278 611 611 611 611 611 611 611 584 611 611 611 611 611 556 611 556").split()]
# The bone logo as PDF paths, in its SVG's 1725 x 430 units, y up
_LOGO = (
    "180 380 m 205 404 l 229 380 l 205 355 l h 78 329 m 84 370 l 126 363 l 119 322 l h 30 235 m 15 266 l 46 282 "
    "l 62 251 l h 47 122 m 10 141 l 29 178 l 66 159 l h 122 48 m 87 43 l 82 77 l 116 83 l h 234 29 m 205 0 l "
    "175 29 l 205 59 l h 328 77 m 322 43 l 288 48 l 293 83 l h 380 178 m 399 141 l 362 122 l 343 159 l h 364 "
    "282 m 395 266 l 379 235 l 348 251 l h 284 363 m 325 370 l 332 329 l 290 322 l h 185 325 m 205 345 l 225 "
    "325 l 205 305 l h 84 222 m 71 248 l 96 261 l 109 235 l h 150 95 m 122 91 l 118 119 l 146 123 l h 292 119 m "
    "287 91 l 259 95 l 264 123 l h 313 261 m 338 248 l 326 222 l 300 235 l h 291 204 m 291 156 253 118 205 118 "
    "c 157 118 118 156 118 204 c 118 252 157 291 205 291 c 253 291 291 252 291 204 c h 718 316 m 718 277 l 609 "
    "135 l 720 135 l 714 92 l 547 92 l 547 131 l 656 274 l 556 274 l 556 316 l h 953 205 m 953 181 949 160 940 "
    "142 c 932 124 920 111 904 101 c 888 91 869 86 848 86 c 815 86 789 96 770 117 c 751 138 742 168 742 205 c "
    "742 228 746 249 755 266 c 763 284 776 298 792 308 c 807 318 826 323 848 323 c 881 323 907 312 925 291 c "
    "944 270 953 241 953 205 c h 799 205 m 799 178 803 158 811 146 c 819 133 832 127 848 127 c 864 127 876 133 "
    "884 146 c 892 159 896 178 896 205 c 896 231 892 250 884 263 c 876 275 864 282 848 282 c 816 282 799 256 "
    "799 205 c h 1199 205 m 1199 181 1194 160 1186 142 c 1177 124 1165 111 1149 101 c 1134 91 1115 86 1093 86 c "
    "1060 86 1034 96 1016 117 c 997 138 988 168 988 205 c 988 228 992 249 1000 266 c 1009 284 1021 298 1037 308 "
    "c 1053 318 1072 323 1094 323 c 1127 323 1152 312 1171 291 c 1189 270 1199 241 1199 205 c h 1045 205 m 1045 "
    "178 1049 158 1057 146 c 1065 133 1077 127 1093 127 c 1109 127 1121 133 1129 146 c 1137 159 1141 178 1141 "
    "205 c 1141 231 1137 250 1129 263 c 1121 275 1109 282 1094 282 c 1061 282 1045 256 1045 205 c h 1380 319 m "
    "1371 267 l 1364 269 1358 269 1351 269 c 1338 269 1327 265 1320 255 c 1312 246 1306 231 1302 212 c 1302 92 "
    "l 1247 92 l 1247 316 l 1294 316 l 1299 272 l 1305 288 1313 301 1323 309 c 1333 318 1345 322 1358 322 c "
    "1366 322 1374 321 1380 319 c h 1466 316 m 1466 92 l 1412 92 l 1412 316 l h 1473 397 m 1473 388 1470 380 "
    "1464 374 c 1457 367 1449 364 1439 364 c 1429 364 1421 367 1415 374 c 1408 380 1405 388 1405 397 c 1405 406 "
    "1408 414 1415 420 c 1421 427 1429 430 1439 430 c 1449 430 1457 427 1464 420 c 1470 414 1473 406 1473 397 c "
    "h 1584 412 m 1584 92 l 1530 92 l 1530 406 l h 1718 316 m 1644 218 l 1725 92 l 1663 92 l 1586 215 l 1661 "
    "316 l h f")

_DRAWN = {"\u2192": ("\xae", 987, 900), "\u2212": ("-", 549, 584)}  # Symbol code and width, drawn width (1/1000 em)


def _win(s: str) -> str:
    """s in WinAnsi code points: the nearest letter or ? for a character it lacks; the arrow and minus stay."""
    out = []
    for ch in s:
        if " " <= ch <= "~" or ch in _DRAWN:
            out.append(ch)
            continue
        for c in (ch, unicodedata.normalize("NFKD", ch)[:1]):
            try:
                out.append(" " if c < " " or c == "\x7f" else c.encode("cp1252").decode("latin-1"))
                break
            except UnicodeEncodeError:
                pass
        else:
            out.append("?")
    return "".join(out)


def _advance(t: str, size: float, bold: bool, spacing: float) -> float:
    widths = _HELVETICA_BOLD if bold else _HELVETICA
    return sum(_DRAWN[c][2] if c in _DRAWN else widths[ord(c) - 32] for c in t) * size / 1000 + spacing * len(t)


def text_width(s: str, size: float, bold: bool = False, spacing: float = 0.0) -> float:
    return _advance(_win(s), size, bold, spacing)


def _metrics(size: float, lh: float = None) -> tuple:
    """Line pitch and baseline offset in points, as the design's browser lays a line out (lh None: normal)."""
    a, d = int(size / PX * 0.96875 + 0.5) * PX, int(size / PX * 0.2421875 + 0.5) * PX
    box = a + d if lh is None else int(size * lh / PX) * PX
    return box, (box - a - d) / 2 + a


def _f(v: float, digits: int = 2) -> str:
    return ("%.*f" % (digits, v)).rstrip("0").rstrip(".")


def _rgb(c: str) -> str:
    return " ".join(_f(int(c[i:i + 2], 16) / 255, 3) for i in (1, 3, 5))


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _path(x: float, y: float, w: float, h: float, r: float) -> str:
    """A rectangle path from its bottom-left in page coordinates (y up), its corners rounded by r."""
    r = max(0.0, min(r, w / 2, h / 2))
    if not r:
        return "%s %s %s %s re" % (_f(x), _f(y), _f(w), _f(h))
    k, X, Y = r * 0.4477, x + w, y + h
    pts = [(x + r, y), (X - r, y), (X - k, y, X, y + k, X, y + r), (X, Y - r), (X, Y - k, X - k, Y, X - r, Y),
           (x + r, Y), (x + k, Y, x, Y - k, x, Y - r), (x, y + r), (x, y + k, x + k, y, x + r, y)]
    return " ".join(" ".join(map(_f, p)) + (" m", " l", " c")[min(i, 1) + (len(p) > 2)] for i, p in enumerate(pts))


class Pdf:
    """A4 pages of drawing operators. x runs from the left edge and y down from the top edge, in points."""

    def __init__(self):
        self.pages = []

    def page(self) -> None:
        self.ops, self.annots = [], []
        self.pages.append((self.ops, self.annots))

    def text(self, x: float, y: float, s: str, size: float, color: str = INK, bold: bool = False, align: str = "l",
             spacing: float = 0.0) -> float:
        """One line with its baseline at y, aligned l, r or c on x. Returns its width."""
        t = _win(s)
        w = _advance(t, size, bold, spacing)
        x -= {"l": 0, "r": w, "c": w / 2}[align]
        ops, art, b = ["BT %s rg %s Tc" % (_rgb(color), _f(spacing))], [], PAGE_H - y
        for run in re.findall("[\u2192\u2212]|[^\u2192\u2212]+", t):
            if run in _DRAWN:
                code, sym, drawn = _DRAWN[run]
                ops.append("1 0 0 1 %s %s Tm 3 Tr %s Tz /F3 %s Tf (%s) Tj 0 Tr 100 Tz"
                           % (_f(x), _f(b), _f(100 * drawn / sym), _f(size), code))
                art.append(self._drawn(run, x, b, size, color, bold))
            else:
                ops.append("1 0 0 1 %s %s Tm /F%d %s Tf (%s) Tj"
                           % (_f(x), _f(b), 2 if bold else 1, _f(size), _esc(run)))
            x += _advance(run, size, bold, spacing)
        if t:
            self.ops.append(" ".join(ops) + " ET" + "".join(art))
        return w

    @staticmethod
    def _drawn(c: str, x: float, b: float, size: float, color: str, bold: bool) -> str:
        """Helvetica's own minus, or an arrow of the same weight across the middle of the figures."""
        if c == "\u2212":
            x0, y0, x1, y1 = (40, 197, 544, 309) if bold else (39, 216, 545, 289)
            return " %s rg %s %s %s %s re f" % (_rgb(color), _f(x + x0 * size / 1000), _f(b + y0 * size / 1000),
                                               _f((x1 - x0) * size / 1000), _f((y1 - y0) * size / 1000))
        t, y, head = (0.112 if bold else 0.073) * size, b + 0.34 * size, 0.29 * size
        tip = x + 0.82 * size - t * 0.7071
        return " q %s RG %s w 0 J 0 j %s %s m %s %s l S %s %s m %s %s l %s %s l S Q" % (
            _rgb(color), _f(t), _f(x + 0.08 * size), _f(y), _f(tip), _f(y), _f(tip - head), _f(y + head), _f(tip),
            _f(y), _f(tip - head), _f(y - head))

    def rect(self, x: float, y: float, w: float, h: float, fill: str = None, stroke: str = None, lw: float = PX,
             r: float = 0.0) -> None:
        """A rectangle from its top-left (x, y), filled and/or stroked on its edge, its corners rounded by r."""
        self.ops.append("q %s%s%s %s Q" % ("%s rg " % _rgb(fill) if fill else "",
                                            "%s RG %s w " % (_rgb(stroke), _f(lw)) if stroke else "",
                                            _path(x, PAGE_H - y - h, w, h, r), "B" if fill and stroke else "f" if fill
                                            else "S"))

    def dashed(self, x: float, y: float, w: float, h: float, r: float, color: str = GREEN) -> None:
        """A 1.5px dashed outline just inside the box, its dashes spread evenly and centred on each side."""
        lw = 1.125
        w, h, r = w - lw, h - lw, max(0.0, min(r - lw / 2, (w - lw) / 2, (h - lw) / 2))
        around = 2 * (w + h) - (8 - 2 * math.pi) * r
        period = around / max(8, 2 * round(around / (7.2 * lw)))
        on = period * 0.62
        self.ops.append("q %s RG %s w [%s %s] %s d %s S Q" % (
            _rgb(color), _f(lw), _f(on), _f(period - on), _f(period - (w / 2 - r - on / 2) % period),
            _path(x + lw / 2, PAGE_H - y - h - lw / 2, w, h, r)))

    def line(self, x1: float, y1: float, x2: float, y2: float, color: str = RULE, lw: float = PX) -> None:
        self.ops.append("q %s RG %s w %s %s m %s %s l S Q" % (_rgb(color), _f(lw), _f(x1), _f(PAGE_H - y1), _f(x2),
                                                              _f(PAGE_H - y2)))

    def link(self, x: float, y: float, w: float, h: float, uri: str) -> None:
        self.annots.append("<< /Type /Annot /Subtype /Link /Rect [%s %s %s %s] /Border [0 0 0] /A << /S /URI /URI (%s) "
                           ">> >>" % (_f(x), _f(PAGE_H - y - h), _f(x + w), _f(PAGE_H - y), _esc(uri)))

    def logo(self, x: float, y: float, w: float) -> None:
        """The bone Zoorik logo, w points wide, from its top-left (x, y)."""
        s = w / 1725
        self.ops.append("q %s rg %s 0 0 %s %s %s cm %s Q" % (_rgb(BONE), _f(s), _f(s), _f(x), _f(PAGE_H - y - 430 * s),
                                                             _LOGO))

    def save(self, title: str) -> bytes:
        """The document: catalog, page tree, info, the three standard fonts, then each page and its content."""
        objs = [b"<< /Type /Catalog /Pages 2 0 R /ViewerPreferences << /DisplayDocTitle true >> >>",
                b"<< /Type /Pages /Kids [%s] /Count %d >>" % (
                    " ".join("%d 0 R" % (7 + 2 * i) for i in range(len(self.pages))).encode(), len(self.pages)),
                b"<< /Title <FEFF%s> /Producer (Zoorik) >>" % title.encode("utf-16-be").hex().upper().encode()]
        objs += [b"<< /Type /Font /Subtype /Type1 /BaseFont /%s >>" % f for f in (
            b"Helvetica /Encoding /WinAnsiEncoding", b"Helvetica-Bold /Encoding /WinAnsiEncoding", b"Symbol")]
        for ops, annots in self.pages:
            data = zlib.compress("\n".join(ops).encode("latin-1"))
            page = ("<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %s %s] /Contents %d 0 R /Resources << /Font << "
                    "/F1 4 0 R /F2 5 0 R /F3 6 0 R >> >>" % (_f(PAGE_W), _f(PAGE_H), len(objs) + 2))
            objs.append((page + (" /Annots [%s]" % " ".join(annots) if annots else "") + " >>").encode())
            objs.append(b"<< /Length %d /Filter /FlateDecode >>\nstream\n%s\nendstream" % (len(data), data))
        out, offsets = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"), []
        for i, body in enumerate(objs, 1):
            offsets.append(len(out))
            out += b"%d 0 obj\n%s\nendobj\n" % (i, body)
        xref = len(out)
        out += b"xref\n0 %d\n0000000000 65535 f \n%s" % (len(objs) + 1, b"".join(b"%010d 00000 n \n" % o
                                                                              for o in offsets))
        out += b"trailer\n<< /Size %d /Root 1 0 R /Info 3 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
        return bytes(out)


def _span(sp, bold: bool, color: str) -> tuple:
    if isinstance(sp, str):
        return sp, bold, color, None
    t, b, c, u = (tuple(sp) + (None, None, None))[:4]
    return t, bold if b is None else b, c or color, u


def wrap(spans, size: float, width: float, bold: bool = False, color: str = INK, spacing: float = 0.0) -> list:
    """Spans in lines no wider than width, broken at spaces and newlines, after a hyphen only in a word wider than
    the line, and inside a word only when the part alone is wider; a separator dot at either end of a line is left
    out. A span is a str or (text, bold, color, url). Returns [(width, [(x, text, bold, color, url)])]."""
    units, space, glue = [], 0, False
    for sp in [spans] if isinstance(spans, str) else spans:
        t, b, c, u = _span(sp, bold, color)
        for tok in re.findall(r"\n| +|[^ \n-]*-|[^ \n-]+", t):
            if tok == "\n":
                units.append(None)
                space, glue = 0, False
            elif tok[0] == " ":
                space, glue = space + len(tok), False
            else:
                if glue:
                    units[-1][1].append((tok, b, c, u))
                else:
                    units.append((space, [(tok, b, c, u)]))
                space, glue = 0, not tok.endswith("-")
    lines, line, x, sw = [], [], 0.0, text_width(" ", size, False, spacing)
    sized = [unit and [(p, text_width(p[0], size, p[1], spacing)) for p in unit[1]] for unit in units]
    for i, unit in enumerate(units):
        if unit is None:
            lines.append((x, line))
            line, x = [], 0.0
            continue
        pieces = sized[i]
        uw, gap = sum(w for _, w in pieces), unit[0] * sw if line else 0.0
        word, j = uw, i + 1
        while (unit[0] or not i or units[i - 1] is None) and j < len(units) and units[j] and not units[j][0]:
            word, j = word + sum(w for _, w in sized[j]), j + 1
        if line and x + gap + (word if word <= width else uw) > width + 0.01:
            lines.append((x, line))
            line, x, gap = [], 0.0, 0.0
        x += gap
        for (t, b, c, u), w in pieces:
            if uw <= width:
                line.append((x, t, b, c, u))
                x += w
                continue
            run = None
            for ch in t:
                cw = text_width(ch, size, b, spacing)
                if line and x + cw > width + 0.01:
                    lines.append((x, line))
                    line, x, run = [], 0.0, None
                if run is None:
                    run = len(line)
                    line.append((x, ch, b, c, u))
                else:
                    line[run] = line[run][:1] + (line[run][1] + ch,) + line[run][2:]
                x += cw
    lines.append((x, line))
    for k, (w, ln) in enumerate(lines):
        if len(ln) > 1 and ln[-1][1] == "\xb7":
            ln = ln[:-1]
            w = ln[-1][0] + text_width(ln[-1][1], size, ln[-1][2], spacing)
        if len(ln) > 1 and ln[0][1] == "\xb7":
            w, ln = w - ln[1][0], [(p[0] - ln[1][0],) + p[1:] for p in ln[1:]]
        lines[k] = (w, ln)
    return lines


def _draw(pdf: Pdf, lines: list, x: float, top: float, width: float, size: float, metrics: tuple, align: str = "l",
          spacing: float = 0.0) -> None:
    """Lines from wrap() in a box at x, its top at top, metrics = (pitch, baseline offset)."""
    y = top + metrics[1]
    for w, pieces in lines:
        x0 = x + {"l": 0, "r": width - w, "c": (width - w) / 2}[align]
        for px, t, b, c, u in pieces:
            pw = pdf.text(x0 + px, y, t, size, c, b, spacing=spacing)
            if u:
                pdf.line(x0 + px, y + size * 0.27 + 0.4, x0 + px + pw, y + size * 0.27 + 0.4, UNDERLINE)
                pdf.link(x0 + px, y - size, pw, size * 1.3, u)
        y += metrics[0]


class Text:
    """A paragraph of spans wrapped to the content width; lh is the line height as a multiple of size (None: normal)."""

    def __init__(self, spans, size: float, color: str = INK, bold: bool = False, lh: float = None,
                 spacing: float = 0.0):
        self.lines, self.size, self.spacing = wrap(spans, size, WIDTH, bold, color, spacing), size, spacing
        self.m = _metrics(size, lh)
        self.h = len(self.lines) * self.m[0]

    def draw(self, pdf: Pdf, x: float, y: float) -> None:
        _draw(pdf, self.lines, x, y, WIDTH, self.size, self.m, spacing=self.spacing)


def headline(spans) -> Text:
    return Text(spans, 27, INK, True, 1.12, -0.675)


def lede(spans) -> Text:
    return Text(spans, 10.5, BODY, False, 1.6)


def fine(spans) -> Text:
    return Text(spans, 7.125, MUTED, False, 1.5)


class Kpis:
    """A row of KPI cards from [(value, label, value colour)]; a newline in a label starts its second line."""

    def __init__(self, items: list):
        self.items, self.w = items, (WIDTH - 6 * (len(items) - 1)) / len(items)
        self.labels = [wrap(label, 7.875, self.w - 19.5, color=MUTED) for _, label, _ in items]
        self.h = 42 + max(len(lines) for lines in self.labels) * _metrics(7.875, 1.4)[0]

    def draw(self, pdf: Pdf, x: float, y: float) -> None:
        for i, ((value, _, color), lines) in enumerate(zip(self.items, self.labels)):
            cx = x + i * (self.w + 6)
            pdf.rect(cx + PX / 2, y + PX / 2, self.w - PX, self.h - PX, PANEL, RULE, r=8.625)
            pdf.text(cx + 9.75, y + 25.5, value, 16.5, color, True, spacing=-0.33)
            _draw(pdf, lines, cx + 9.75, y + 33, self.w, 7.875, _metrics(7.875, 1.4))


class Section:
    """A numbered section heading."""

    def __init__(self, n: int, title: str):
        self.n, self.title, self.h = n, title, 19.5

    def draw(self, pdf: Pdf, x: float, y: float) -> None:
        pdf.rect(x, y, 19.5, 19.5, BLUE, r=9.75)
        pdf.text(x + 9.75, y + 13.5, str(self.n), 9.75, WHITE, True, "c")
        pdf.text(x + 27, y + 15.375, self.title, 15.75, INK, True, spacing=-0.236)


class Legend:
    """One line of keys: (label, fill) with a 10.5 x 5.25 swatch, or (label, fill, w, h, dashed)."""

    def __init__(self, items: list):
        self.items, self.h = items, 8.25

    def draw(self, pdf: Pdf, x: float, y: float) -> None:
        for item in self.items:
            label, fill, w, h, dashed = tuple(item) + (10.5, 5.25, False)[len(item) - 2:]
            pdf.rect(x, y + 6.75 - h, w, h, fill, r=1.5)
            if dashed:
                pdf.dashed(x, y + 6.75 - h, w, h, 1.5)
            x += w + 3.75
            x += pdf.text(x, y + 6.75, label, 7.125, MUTED) + 10.5


def pair_bars(today: float, after: float, top: float):
    """Art: the today bar (slate) over the after bar (blue), on one scale where top fills 112.5pt."""
    def art(pdf, x, y, w):
        for i, (v, color) in enumerate(((today, SLATE), (after, BLUE))):
            if pdf and v > 0:
                pdf.rect(x, y + 1.5 + 6.75 * i, min(112.5, w) * v / top, 5.25, color, r=2.625)
        return 15.0
    return art


class Cell:
    """A table cell: text wrapped to its column, a muted sub-line under it, and art under both. bold and color default
    to the row's. art is a function (pdf, x, y, w) that draws from the cell's top-left within width w and returns its
    height; with pdf None it only returns the height."""

    def __init__(self, text: str = "", sub: str = "", bold: bool = None, color: str = None, art=None):
        self.text, self.sub, self.bold, self.color, self.art = text, sub, bold, color, art


class Card:
    """A bordered card holding one table. cols are [(fraction of the width, "l" or "r")], head the column titles (a
    newline breaks one), rows lists of Cell or str, total an optional bold last row under a rule. The card opens with
    a title and a muted aside, or with a name, a muted meta line and a green pill; legend is Legend items. A card
    taller than the space left splits between rows, and each part repeats the column titles."""

    def __init__(self, cols: list, head: list, rows: list, total: list = None, title: str = "", aside: str = "",
                 name: str = "", meta: str = "", pill: str = "", legend: list = None):
        self.pad = (7.5, 9.0, 3.0) if name else (9.0, 10.5, 9.0)
        self.inner = WIDTH - 2 * (PX + self.pad[1])
        edges = [self.inner * sum(f for f, _ in cols[:i]) for i in range(len(cols) + 1)]
        self.cols = [(x0, x1 - x0, a) for (_, a), x0, x1 in zip(cols, edges, edges[1:])]
        self.title, self.aside, self.name, self.meta, self.pill = title, aside, name, meta, pill
        self.legend, self.lines, self.first = Legend(legend) if legend else None, None, True
        self.top_h = 16.5 if title else 0.0
        if name:
            room = self.inner - (text_width(pill, 8.25, True) + 27 if pill else 0)
            if text_width(name, 10.5, True) + 6 + text_width(meta, 7.5) > room:
                self.lines = wrap(name, 10.5, room, True), wrap(meta, 7.5, room, color=MUTED)
            self.top_h = 3 + (max(16.5, 1.5 + 12.75 * len(self.lines[0]) + 9 * len(self.lines[1])) if self.lines
                              else 16.5)
        self.top_h += 16.75 if legend else 0
        self.head = [wrap(t, 7.125, w - 9, color=MUTED) for t, (_, w, _) in zip(head, self.cols)]
        self.head_h = 8.25 * max(len(lines) for lines in self.head) + 8.25
        self.rows = [self._row(r, False) for r in rows] + ([self._row(total, True)] if total else [])
        self._size()

    def _row(self, cells: list, total: bool) -> tuple:
        size, laid = 9.0 if total else 8.25, []
        for cell, (_, w, _) in zip(cells, self.cols):
            c = cell if isinstance(cell, Cell) else Cell(cell)
            main = wrap(c.text, size, w - 9, total if c.bold is None else c.bold, c.color or INK) if c.text else []
            sub = wrap(c.sub, 7.125, w - 9, color=MUTED) if c.sub else []
            art = c.art(None, 0, 0, w - 9) if c.art else 0.0
            laid.append((c, main, sub, len(main) * _metrics(size)[0] + 8.25 * len(sub) + art))
        return 12 + max(cell[3] for cell in laid), total, size, laid

    def _size(self) -> None:
        self.h = 2 * PX + self.pad[0] + self.pad[2] + self.top_h * self.first + self.head_h + sum(
            r[0] for r in self.rows) + PX * (len(self.rows) - 1)
        self.min_h = self.h - sum(r[0] + PX for r in self.rows[1:])

    def split(self, room: float):
        """The rows that fit in room and the rest, as two cards; None if not one row fits."""
        used, k = self.h - sum(r[0] + PX for r in self.rows) + PX, 0
        while k < len(self.rows) and used + self.rows[k][0] + PX * bool(k) <= room:
            used, k = used + self.rows[k][0] + PX * bool(k), k + 1
        if k == len(self.rows) - 1 and k > 1 and self.rows[-1][1]:
            k -= 1
        if not k or k == len(self.rows):
            return None
        return self._part(self.rows[:k], self.first), self._part(self.rows[k:], False)

    def _part(self, rows: list, first: bool):
        part = object.__new__(Card)
        part.__dict__.update(self.__dict__, rows=rows, first=first)
        part._size()
        return part

    def draw(self, pdf: Pdf, x: float, y: float) -> None:
        pdf.rect(x + PX / 2, y + PX / 2, WIDTH - PX, self.h - PX, WHITE, RULE, r=9 - PX / 2)
        x, y, right = x + PX + self.pad[1], y + PX + self.pad[0], x + WIDTH - PX - self.pad[1]
        if self.first:
            if self.title:
                pdf.text(x, y + 8.25, self.title, 8.25, INK, True)
                pdf.text(right, y + 8.25, self.aside, 8.25, MUTED, align="r")
            if self.name:
                if self.pill:
                    w = text_width(self.pill, 8.25, True) + 15
                    pdf.rect(right - w, y + 1.5, w, 15, MINT, r=7.5)
                    pdf.text(right - 7.5, y + 12, self.pill, 8.25, GREEN_INK, True, "r")
                if self.lines:
                    _draw(pdf, self.lines[0], x, y + 1.5, self.inner, 10.5, (12.75, 10.5))
                    _draw(pdf, self.lines[1], x, y + 1.5 + 12.75 * len(self.lines[0]), self.inner, 7.5, (9, 7.5))
                else:
                    pdf.text(x + pdf.text(x, y + 12, self.name, 10.5, INK, True) + 6, y + 12, self.meta, 7.5, MUTED)
            y += self.top_h - (16.75 if self.legend else 0)
            if self.legend:
                self.legend.draw(pdf, x, y)
                y += 16.75
        n = max(len(lines) for lines in self.head)
        for (cx, w, align), lines in zip(self.cols, self.head):
            _draw(pdf, lines, x + cx + 4.5, y + 3.75 + 8.25 * (n - len(lines)), w - 9, 7.125, (8.25, 6.75), align)
        y += self.head_h
        pdf.line(x, y - PX / 2, right, y - PX / 2, RULE)
        for i, (h, total, size, laid) in enumerate(self.rows):
            for (c, main, sub, ch), (cx, w, align) in zip(laid, self.cols):
                top = y + 6 + int((h - 12 - ch) / 2 / PX) * PX
                _draw(pdf, main, x + cx + 4.5, top, w - 9, size, _metrics(size), align)
                top += len(main) * _metrics(size)[0]
                _draw(pdf, sub, x + cx + 4.5, top, w - 9, 7.125, (8.25, 6.75), align)
                if c.art:
                    c.art(pdf, x + cx + 4.5, top + 8.25 * len(sub), w - 9)
            y += h + PX
            if i + 1 < len(self.rows):
                pdf.line(x, y - PX / 2, right, y - PX / 2, RULE if self.rows[i + 1][1] else RULE2)


class NextSteps:
    """The navy Next steps card: a title over spans; a link is (text, True, WHITE, url)."""

    def __init__(self, title: str, spans: list):
        self.title, self.lines = title, wrap(spans, 8.25, WIDTH - 27, color=HAZE)
        self.h = 36 + 15.75 * len(self.lines)

    def draw(self, pdf: Pdf, x: float, y: float) -> None:
        pdf.rect(x, y, WIDTH, self.h, DEEP, r=10.5)
        pdf.text(x + 13.5, y + 21, self.title, 10.5, WHITE, True)
        _draw(pdf, self.lines, x + 13.5, y + 25.5, WIDTH - 27, 8.25, (15.75, 11.75))


_BANNER = 25.5 * MM + 57.75  # page 1's navy banner: padding, logo, title and subtitle lines


class Report:
    """The flowing A4 document. Page 1 opens with the navy banner, later pages with the slim bar, and every page ends
    with the footer. add() blocks in order: gap is the space above a block, dropped at the top of a page, and keep
    holds a block on the page where the next one starts. A block has h and draw(pdf, x, y); a Card also splits."""

    def __init__(self, title: str, sub: str, date: str, footer: str):
        self.title, self.sub, self.date, self.footer, self.blocks = title, sub, date, footer, []

    def add(self, block, gap: float = 0.0, keep: bool = False) -> None:
        self.blocks.append((block, gap, keep))

    def _pages(self) -> list:
        top, bottom = 24 * MM, PAGE_H - 16 * MM
        items, pages, y, i = list(self.blocks), [[]], _BANNER + GAP, 0

        def splits(b, room: float) -> bool:
            """A card splits when it is taller than a page, or when a third of a page or more is left for it."""
            return isinstance(b, Card) and (b.h > bottom - top or room >= (bottom - top) / 3)

        def need(j: int, at: float) -> float:
            b, gap, keep = items[j]
            h = b.min_h if splits(b, bottom - at - gap) else b.h
            return gap + h + (need(j + 1, at + gap + h) if keep and j + 1 < len(items) else 0)

        while i < len(items):
            b, gap, keep = items[i]
            fresh = not pages[-1]
            g = 0 if fresh else gap
            if y + g + b.h + (need(i + 1, y + g + b.h) if keep and i + 1 < len(items) else 0) <= bottom or fresh and (
                    not isinstance(b, Card) or b.h <= bottom - top):
                pages[-1].append((b, y + g))
                y, i = y + g + b.h, i + 1
                continue
            part = b.split(bottom - y - g) if splits(b, bottom - y - g) else None
            if part:
                pages[-1].append((part[0], y + g))
                items[i] = (part[1], 0, keep)
            elif fresh:
                pages[-1].append((b, y))
                i += 1
            pages.append([])
            y = top
        return pages

    def pdf(self) -> bytes:
        pages, pdf = self._pages(), Pdf()
        for n, blocks in enumerate(pages, 1):
            pdf.page()
            if n == 1:
                pdf.rect(0, 0, PAGE_W, _BANNER, NAVY)
                pdf.logo(SIDE, 10 * MM, 96)
                pdf.text(SIDE, 88.11, self.title, 19.5, BONE, True, spacing=-0.39)
                pdf.text(SIDE, 105.11, self.sub, 8.25, LILAC)
            else:
                pdf.rect(0, 0, PAGE_W, 16 * MM, NAVY)
                pdf.logo(SIDE, 8 * MM - 9, 72)
                x = PAGE_W - SIDE - text_width(self.title + "\xb7" + self.date, 7.125, False, 0.1425) - 9
                for t in (self.title, "\xb7", self.date):
                    x += pdf.text(x, 25.5, t, 7.125, LILAC, spacing=0.1425) + 4.5
            for block, y in blocks:
                block.draw(pdf, SIDE, y)
            pdf.text(SIDE, PAGE_H - 14.7, self.footer, 6.375, MUTED)
            pdf.text(PAGE_W - SIDE, PAGE_H - 14.7, "%d / %d" % (n, len(pages)), 6.375, MUTED, align="r")
        return pdf.save("Zoorik " + self.title)


# ---------------------------------------------------------------- end of pdf writer


# ---------------------------------------------------------------- main

def cloud_shell() -> str:
    """'azure' in Azure Cloud Shell, 'aws' in AWS CloudShell, else ''."""
    if os.environ.get("ACC_CLOUD") or os.environ.get("AZUREPS_HOST_ENVIRONMENT", "").startswith("cloud-shell"):
        return "azure"
    return "aws" if os.environ.get("AWS_EXECUTION_ENV", "") == "CloudShell" else ""


LISTED_NAMES = 20


def prompt_text(cloud: str, candidates: List[Vm], names: Dict[str, str]) -> str:
    count = len(candidates)
    minutes = max(1, math.ceil(math.ceil(count / WORKERS) * RUN_COMMAND_SECONDS / 60))
    script = "\n".join("    " + line for line in GUEST_SCRIPT[cloud].splitlines())
    groups: Dict[str, List[str]] = {}
    for v in candidates:
        groups.setdefault(v.region if cloud == "aws" else names.get(v.subscription, v.subscription), []).append(v.name)
    listed, left, rest, rest_groups = [], LISTED_NAMES, 0, 0
    for group in sorted(groups):
        vm_names = sorted(groups[group])
        if left <= 0:
            rest, rest_groups = rest + len(vm_names), rest_groups + 1
            continue
        shown = vm_names[:left]
        left -= len(shown)
        more = len(vm_names) - len(shown)
        listed.append(f"    {group}: {', '.join(shown)}" + (f" and {more} more" if more else ""))
    if rest:
        listed.append(f"    and {rest} more in {rest_groups} other {'region' if cloud == 'aws' else 'subscription'}(s)")
    idle = ("  Cloud Shell closes a session after 20 minutes without a key press. Press Enter every few\n"
            "  minutes until the report is written.\n\n") if cloud_shell() and minutes >= 10 else ""
    if cloud == "aws":
        return (f"\nDisk usage\n"
                f"  {count} running Linux instance(s) have data volumes:\n" + "\n".join(listed) + "\n\n"
                f"  Their saving needs the used space on those volumes, which only the instance itself can report.\n"
                f"  With your yes, this command, which only reads, runs in each of them through AWS Systems\n"
                f"  Manager Run Command ({SSM_DOCUMENT}), {WORKERS} instances at a time (about {minutes} min):\n\n"
                f"{script}\n\n"
                f"  On its side, Systems Manager keeps the command and its output in its Run Command history,\n"
                f"  its agent keeps a copy under /var/lib/amazon/ssm on the instance, and CloudTrail records\n"
                f"  each call. It needs the permissions {SSM_PERMISSIONS};\n"
                f"  an instance without them shows no saving, with the reason.\n\n"
                f"{idle}"
                f"Read disk usage inside these {count} instance(s) with Systems Manager Run Command? [y/N] ")
    return (f"\nDisk usage\n"
            f"  {count} running Linux VM(s) have data disks:\n" + "\n".join(listed) + "\n\n"
            f"  Their saving needs the used space on those disks, which only the VM itself can report.\n"
            f"  With your yes, this command, which only reads, runs in each of them through Azure Run\n"
            f"  Command, {WORKERS} VMs at a time (about {minutes} min):\n\n"
            f"{script}\n\n"
            f"  On its side, Azure adds its Run Command extension (RunCommandLinux) to a VM that does not\n"
            f"  have it yet, keeps a copy of the command and its output under /var/lib/waagent on the VM,\n"
            f"  and records each run in the activity log. It needs the permission\n"
            f"  {RUN_COMMAND_PERMISSION} (Virtual Machine Contributor has it);\n"
            f"  a VM without it shows no saving, with the reason.\n\n"
            f"{idle}"
            f"Read disk usage inside these {count} VM(s) with Azure Run Command? [y/N] ")


def fetch_prices(func: Callable, regions: List[str], label: str) -> Dict[str, Optional[Prices]]:
    fetched = parallel(func, regions, label)
    errors = sorted({str(p) for p in fetched if not isinstance(p, Prices)})
    if errors:
        say(f"List prices: {errors[0]}")
    return {r: p if isinstance(p, Prices) else None for r, p in zip(regions, fetched)}


def collect_facts(vms: List[Vm]) -> Dict[str, Optional[Prices]]:
    """Azure's VM size limits per subscription and region, and the list prices of the VMs' data disks per region,
    read in parallel. AWS's instance limits come with its inventory."""
    azure_vms = [v for v in vms if v.cloud == "azure"]
    keys = sorted({(v.subscription, v.region) for v in azure_vms if re.fullmatch(r"[a-z0-9]+", v.region)})
    disks = list({dd.disk.id: dd.disk for v in vms for dd in v.data if dd.disk}.values())
    regions = sorted({k[1] for k in keys} | {d.region for d in disks
                                             if d.cloud == "azure" and re.fullmatch(r"[a-z0-9]+", d.region)})
    facts = dict(zip(keys, parallel(region_facts, keys, "Reading VM size limits, subscription-regions")))
    prices = fetch_prices(region_prices, regions, "Reading list prices, regions")
    aws_regions = sorted({d.region for d in disks if d.cloud == "aws" and re.fullmatch(r"[a-z0-9-]+", d.region)})
    prices.update(fetch_prices(ebs_prices, aws_regions, "Reading AWS list prices, regions"))
    for v in azure_vms:
        f = facts.get((v.subscription, v.region))
        if isinstance(f, RegionFacts):
            limits = f.sizes.get(v.size.lower())
            if limits:
                v.slots, v.uncached_iops, v.uncached_mbps = limits
            else:
                v.facts_note = f"{v.size} not listed in {v.region}"
            v.pool_v2 = v2_offered(v, f)
        else:
            v.facts_note = str(f) if f else "region unknown"
    for d in disks:
        price_disk(d, prices)
    return prices


def disk_activity(disk: Disk) -> Hourly:
    return volume_metrics(disk) if disk.cloud == "aws" else disk_metrics(disk)


def unused_path(folder: str, stem: str) -> str:
    """A report path in the folder that no earlier report holds, so nothing is overwritten."""
    path, n = os.path.join(folder, stem + ".pdf"), 1
    while os.path.exists(path):
        n += 1
        path = os.path.join(folder, f"{stem}-{n}.pdf")
    return path


def sign_in(shell: str) -> Tuple[List[Scan], List[Tuple[str, Failed]]]:
    """The clouds to assess: the Cloud Shell's own, else each whose CLI is signed in; and why the others are not."""
    scans: List[Scan] = []
    problems: List[Tuple[str, Failed]] = []
    if shell != "aws":
        try:
            directory, subs, skipped = signed_in()
            if not subs:
                raise Failed("no enabled subscription is visible to this sign-in; run az login")
            scans.append(Scan("azure", directory, subs, skipped))
        except Failed as e:
            problems.append(("Azure", e))
    if shell != "azure":
        try:
            account, regions = aws_signed_in()
            scans.append(Scan("aws", account, regions))
        except Failed as e:
            problems.append(("AWS", e))
    return scans, problems


def grouped(scopes: List[Subscription]) -> Dict[str, List[str]]:
    """Scopes by the reason they were not scanned: one line for every region an account policy refuses."""
    out: Dict[str, List[str]] = {}
    for s in scopes:
        out.setdefault(s.note, []).append(s.name)
    return out


def read_scopes(scan: Scan) -> List[Vm]:
    """Read one cloud's inventory and say what each subscription or region held."""
    if scan.cloud == "azure":
        say(f"Directory: {scan.label}")
        for s in scan.skipped:
            say(f"Not scanned: {s.name} ({s.id}), {s.note}")
        found = inventory(scan.scopes)
        label, kinds = "Subscription", ("VMs", "disks")
    else:
        say(f"AWS account: {scan.label}")
        found = aws_inventory(scan.scopes)
        label, kinds = "Region", ("instances", "volumes")
    for note, names in grouped([s for s in scan.scopes if s.note]).items():
        say(f"{label} {', '.join(names)}: not scanned, {note}")
    for s in scan.scopes:
        if not s.note and (s.vms or s.disks):
            say(f"{label} {s.name}: {s.vms} {kinds[0]}, {s.disks} {kinds[1]}")
    empty = sum(1 for s in scan.scopes if not s.note and not s.vms and not s.disks)
    if empty:
        say(f"{empty} {label.lower()}(s) with no {kinds[0]} or {kinds[1]}.")
    return found


def assessment() -> int:
    say(f"Zoorik Amoeba: disk assessment {VERSION}")
    for started in RUN_COMMAND_STARTED.values():
        started.clear()
    shell = cloud_shell()
    scans, problems = sign_in(shell)
    signed_out = [(name, e) for name, e in problems if "is not installed" not in str(e)]
    if not scans:
        if not signed_out:
            say("This needs the Azure CLI (az) or the AWS CLI (aws), signed in. Open Azure Cloud Shell "
                "(shell.azure.com) or AWS CloudShell and run it there.")
        for name, e in signed_out:
            say(f"{name}: {e}, then run this again.")
        return 1
    for name, e in signed_out:
        say(f"{name}: not assessed, {e}")
    vms: List[Vm] = []
    for scan in scans:
        vms += read_scopes(scan)
    listed = [v for v in vms if v.data]
    if not listed:
        if all(s.note for scan in scans for s in scan.scopes):
            say("Nothing could be read, so there is nothing to report. Fix the reason above, then run this again.")
            return 1
        say("No VM with data disks in what was scanned, so there is nothing to report.")
        return 0
    prices = collect_facts(listed)

    candidates = []
    for v in listed:
        if not v.linux or screen(v):
            continue
        if not v.running:
            v.guest_note = "VM is not running"
        elif not any(dd.disk for dd in v.data):
            v.guest_note = "no managed data disk" if v.cloud == "azure" else "data volumes not readable"
        elif v.channel:
            v.guest_note = v.channel
        else:
            candidates.append(v)
    names = {s.id: s.name for scan in scans for s in scan.scopes}
    for scan in scans:
        group = [v for v in candidates if v.cloud == scan.cloud]
        if not group:
            continue
        answer = ask(prompt_text(scan.cloud, group, names))
        if answer is None:
            say(f"\nNo terminal to ask on, so disk usage inside {CLOUD_NAMES[scan.cloud]} VMs is not read.")
        if answer:
            read = read_guest if scan.cloud == "azure" else read_instance
            for v, result in zip(group, parallel(read, group, "Reading disk usage, VMs")):
                if isinstance(result, Guest):
                    v.guest = result
                else:
                    v.guest_note = str(result)
        else:
            for v in group:
                v.guest_note = "declined at the prompt" if answer is False else "no terminal to ask on"

    measured = list({dd.disk.id: dd.disk for v in listed if v.guest
                     for dd in under(v, poolable(v)) if dd.disk}.values())
    for d, result in zip(measured, parallel(disk_activity, measured, f"Reading {METRIC_DAYS} days of disk metrics")):
        if isinstance(result, dict) and result:
            d.hourly = result
        else:
            d.metric_note = f"metrics not readable: {result}" if isinstance(result, Failed) else \
                f"no metric data in the last {METRIC_DAYS} days"
    for v in listed:
        try:
            assess(v, prices)
        except Exception as e:  # one VM with unexpected data must not stop the report
            v.reasons = [f"not assessed: unexpected data ({type(e).__name__})"]

    now = datetime.now(timezone.utc)
    data, saving = report(scans, listed, now)
    stem = f"zoorik-amoeba-assessment-{now:%Y%m%d-%H%M}"
    path = ""
    for folder in (os.getcwd(), os.path.expanduser("~")):
        try:
            path = unused_path(folder, stem)
            with open(path, "wb") as f:
                f.write(data)
            break
        except OSError:
            path = ""
    if not path:
        say(f"Could not write {stem}.pdf here or in your home folder.")
        return 1

    say()
    say(f"VMs:               {len(vms)} scanned, {len(listed)} with data disks")
    say(f"Estimated saving:  {saving}")
    say()
    say(f"Report: {path}")
    if shell == "azure":
        say(f"Download it: Cloud Shell toolbar > Manage files > Download, then enter {path}")
    elif shell == "aws":
        say(f"Download it: Actions > Download file, then enter {path}")
        say("(Download needs the full CloudShell page; if Actions is missing, open CloudShell in a new tab.)")
    say(f"Please e-mail it to {SEND_TO}")
    return 0


def stop_note() -> str:
    """What the run had changed in your cloud when it stopped."""
    started = []
    if RUN_COMMAND_STARTED["azure"]:
        started.append(f"Azure Run Command had already started in {len(RUN_COMMAND_STARTED['azure'])} VM(s) "
                       "(see the activity log)")
    if RUN_COMMAND_STARTED["aws"]:
        started.append(f"Systems Manager Run Command had already started in {len(RUN_COMMAND_STARTED['aws'])} "
                       "instance(s) (see its Run Command history)")
    if not started:
        return "Nothing in your cloud was changed."
    return "; ".join(started) + "; nothing else in your cloud was changed."


def main(argv: Optional[List[str]] = None) -> int:
    global ARG_DIR
    args = sys.argv[1:] if argv is None else list(argv)
    if args in (["-h"], ["--help"]):
        print(__doc__)
        return 0
    if args == ["--version"]:
        print(f"Zoorik Amoeba: disk assessment {VERSION}")
        return 0
    if args:
        sys.stderr.write(f"This assessment has no options yet. Run it on its own: {ONE_LINER}\n")
        return 2
    sys.stdout.reconfigure(errors="replace")
    if os.name == "nt":
        ARG_DIR = tempfile.mkdtemp(prefix="zoorik-disks-")
    try:
        return assessment()
    except KeyboardInterrupt:
        say(f"\nStopped. {stop_note()}")
        if ARG_DIR:
            shutil.rmtree(ARG_DIR, ignore_errors=True)
        os._exit(130)  # leave at once: never wait for calls still in flight
    except Failed as e:
        say(f"\nStopped: {e}. {stop_note()}")
        return 1
    except Exception as e:  # a prospect sees one plain line, never a traceback
        say(f"\nStopped by an unexpected error ({type(e).__name__}). {stop_note()} "
            f"Please send this line to {SEND_TO}.")
        return 1
    finally:
        if ARG_DIR:
            shutil.rmtree(ARG_DIR, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
