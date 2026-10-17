#!/usr/bin/env python3
"""Zoorik Amoeba: disk assessment.

Reads the VMs and disks you can see in Azure and AWS, estimates what Zoorik
Amoeba (Zoorik block storage) could save by rightsizing the data disks of
your Linux VMs, and writes one Excel file for you to e-mail to Zoorik.

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
  - Azure Monitor: 14 days of IOPS and throughput for each data disk of a
    Linux VM.
  - prices.azure.com: the public list prices of managed disks.
  AWS, in every enabled region of the account you are signed in to:
  - EC2: your instances, EBS volumes, and each instance type's EBS limits.
  - Systems Manager: which instances its agent manages.
  - CloudWatch: 14 days of IOPS and throughput for each data volume of a
    Linux instance.
  - the AWS Price List API: EBS list prices.
  Only if you answer y to its question: one command that only reads, inside
  each running Linux VM with data disks, through Azure Run Command or AWS
  Systems Manager Run Command. It reads used space (df, lsblk), the kernel
  version, the Linux distribution and whether btrfs is available. On its
  side, Azure adds its Run Command extension to a VM that does not have it
  yet, keeps a copy of the command and its output under /var/lib/waagent on
  the VM, and records each run in the activity log. Systems Manager keeps the
  command and its output in its Run Command history, its agent keeps a copy
  under /var/lib/amazon/ssm on the instance, and CloudTrail records each call.
It writes one file: the Excel report, in the current directory. On Windows,
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

if sys.version_info < (3, 8):
    print("This needs Python 3.8 or later. Open Azure Cloud Shell or AWS CloudShell and run it there.")
    sys.exit(1)

import http.client
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Tuple
from xml.sax.saxutils import escape

# ---------------------------------------------------------------- config

VERSION = "1.1.0"
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
# MB/s in the report: 10^6 bytes per second on Azure; on AWS MiB/s, the unit gp3 throughput is set in.
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

# Premium SSD v2 limits, from Azure's documentation.
V2_MIN_IOPS = 3000
V2_MIN_MBPS = 125
V2_MAX_IOPS_PER_GIB = 500
V2_MBPS_PER_IOPS = 0.25
V2_MAX_MBPS = 2000

# How Zoorik Amoeba lays out a new pool on AWS, from its current planner: the hold size, then equal gp3 volumes.
GP3 = "gp3"
# The band every VM starts with: Amoeba keeps its pool 80-90% full.
BAND_LOW = 0.80
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
POOL_V2 = "Premium SSD v2"
POOL_V1 = "Premium SSD"
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
    "gp3": ("EBS", "", "gp3"),
    "gp2": ("EBS", "", "gp2"),
    "io1": ("EBS", "", "io1"),
    "io2": ("EBS", "", "io2"),
    "st1": ("EBS", "", "st1"),
    "sc1": ("EBS", "", "sc1"),
    "standard": ("EBS", "", "magnetic (standard)"),
}
TIER_PRODUCTS = ("Premium SSD Managed Disks", "Standard SSD Managed Disks", "Standard HDD Managed Disks")
PROVISIONED_PRODUCTS = {"Azure Premium SSD v2": "Premium LRS ", "Ultra Disks": "Ultra LRS "}
PROVISIONED_METERS = {"Provisioned Capacity": "capacity", "Provisioned IOPS": "iops",
                      "Provisioned Throughput (MBps)": "mbps"}
RHEL_FAMILY = ("rhel", "centos", "rocky", "almalinux", "ol")
# A distribution whose ID_LIKE names one of these is RHEL-family too, as Amoeba's agent reads it: Amazon Linux is.
RHEL_LIKE = ("rhel", "centos", "fedora")
OS_NAMES = {"ubuntu": "Ubuntu", "debian": "Debian", "rhel": "RHEL", "centos": "CentOS", "rocky": "Rocky Linux",
            "almalinux": "AlmaLinux", "ol": "Oracle Linux", "sles": "SLES", "sles_sap": "SLES for SAP",
            "opensuse-leap": "openSUSE Leap", "azurelinux": "Azure Linux", "mariner": "CBL-Mariner",
            "fedora": "Fedora", "flatcar": "Flatcar", "amzn": "Amazon Linux"}

# The one command that runs inside a VM, and only after a yes. It only reads. The two clouds differ only in how a
# data disk shows in the VM: by LUN on Azure, by volume id (an NVMe serial) on AWS.
_GUEST_HEAD = r"""echo S
echo "K $(uname -r)"
grep -E '^(ID|ID_LIKE|VERSION_ID)=' /etc/os-release
if grep -qw btrfs /proc/filesystems; then echo "B loaded"; elif modinfo -n btrfs >/dev/null 2>&1; then echo "B module"; else echo "B absent"; fi
"""
_GUEST_TAIL = r"""
timeout 30 df -P -B1 -T -l | awk '$1 ~ /^\/dev\// && $1 !~ /^\/dev\/loop/ {m=$7; for (i=8; i<=NF; i++) m=m" "$i; print "D", $2, $3, $4, m}'
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
    name: str
    subscription: str
    resource_group: str
    region: str
    sku: str
    size_gb: int
    tier: str
    state: str
    iops: Optional[int]
    mbps: Optional[int]
    ade: bool
    created: str
    detached: str
    max_shares: int = 1
    multi_attach: bool = False
    cloud: str = "azure"
    price: Optional[float] = None
    price_tier: str = ""
    price_note: str = ""
    hourly: Optional[Hourly] = None
    metric_note: str = ""

    @property
    def kind(self) -> str:
        return DISK_TYPES.get(self.sku.lower(), ("", "", self.sku or "Unknown"))[2]

    @property
    def shared(self) -> bool:
        return self.max_shares > 1 or self.multi_attach

    def peak(self) -> Tuple[Optional[float], Optional[float]]:
        """Peak IOPS and MB/s over the metric window."""
        if not self.hourly:
            return None, None
        return max(v[0] for v in self.hourly.values()), max(v[1] for v in self.hourly.values()) / MB[self.cloud]


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
    os_id: str = ""
    os_like: str = ""
    os_version: str = ""
    btrfs: str = ""
    luns: Dict[int, List[str]] = field(default_factory=dict)
    mounts: Dict[str, Tuple[str, int]] = field(default_factory=dict)
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
    os_name: str
    os_version: str
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
    lane: str = ""
    reasons: List[str] = field(default_factory=list)
    used_gb: Optional[float] = None
    util: Optional[float] = None
    current: Optional[float] = None
    pool_gb: Optional[int] = None
    pool_cost: Optional[float] = None
    saving: Optional[float] = None
    no_saving: bool = False

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
    guest_answer: str = "no running Linux VM with data disks to read"


def r2(value: float) -> float:
    return round(value + 1e-9, 2)


def iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(text: str) -> Optional[datetime]:
    match = re.match(r"(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})", text or "")
    if not match:
        return None
    return datetime.strptime(match.group(1) + "T" + match.group(2), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------- inventory

SUBSCRIPTIONS_QUERY = "resourcecontainers | where type =~ 'microsoft.resources/subscriptions' | project subscriptionId"
VM_QUERY = """resources
| where type =~ 'microsoft.compute/virtualmachines'
| project id, name, resourceGroup, subscriptionId, location, zones,
    size = tostring(properties.hardwareProfile.vmSize),
    osType = tostring(properties.storageProfile.osDisk.osType),
    power = tostring(properties.extended.instanceView.powerState.code),
    osName = tostring(properties.extended.instanceView.osName),
    osVersion = tostring(properties.extended.instanceView.osVersion),
    dataDisks = properties.storageProfile.dataDisks
| order by id asc"""
DISK_QUERY = """resources
| where type =~ 'microsoft.compute/disks'
| project id, name, resourceGroup, subscriptionId, location,
    sku = tostring(sku.name), tier = tostring(properties.tier),
    sizeGb = toint(properties.diskSizeGB), state = tostring(properties.diskState),
    iops = tolong(properties.diskIOPSReadWrite), mbps = tolong(properties.diskMBpsReadWrite),
    ade = tobool(properties.encryptionSettingsCollection.enabled), maxShares = toint(properties.maxShares),
    created = tostring(properties.timeCreated),
    detached = coalesce(tostring(properties.LastOwnershipUpdateTime), tostring(properties.lastOwnershipUpdateTime))
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
    return Disk(id=r.get("id") or "", name=r.get("name") or "", subscription=r.get("subscriptionId") or "",
                resource_group=r.get("resourceGroup") or "", region=(r.get("location") or "").lower(),
                sku=r.get("sku") or "", size_gb=int(r.get("sizeGb") or 0), tier=r.get("tier") or "",
                state=r.get("state") or "", iops=r.get("iops"), mbps=r.get("mbps"), ade=r.get("ade") is True,
                created=r.get("created") or "", detached=r.get("detached") or "",
                max_shares=r.get("maxShares") if isinstance(r.get("maxShares"), int) else 1)


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
              power=power.capitalize(), os_name=r.get("osName") or "", os_version=r.get("osVersion") or "",
              data=sorted(data, key=lambda x: x.lun))


def managing_service(managed_by: str) -> str:
    """The resource type that manages a resource group, e.g. Microsoft.Databricks/workspaces, without its name."""
    match = re.search(r"/providers/([^/]+/[^/]+)", managed_by or "")
    return match.group(1) if match else "another Azure service"


def inventory(subs: List[Subscription]) -> Tuple[List[Vm], List[Disk]]:
    """Every VM and managed disk in the subscriptions, through Azure Resource Graph."""
    if not subs:
        return [], []
    try:
        seen = {r.get("subscriptionId") for r in graph(SUBSCRIPTIONS_QUERY, [s.id for s in subs])}
    except Failed as e:
        for s in subs:
            s.note = f"Resource Graph: {e}"
        return [], []
    for s in subs:
        if s.id not in seen:
            s.note = "Resource Graph cannot read it (no Reader access?)"
    readable = [s.id for s in subs if not s.note]
    if not readable:
        return [], []
    disk_list = [make_disk(r) for r in graph(DISK_QUERY, readable)]
    disks = {d.id.lower(): d for d in disk_list}
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
        s.disks = sum(1 for d in disk_list if d.subscription == s.id)
    return vms, disk_list


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


def make_volume(v: dict, account: str, region: str) -> Disk:
    kind = v.get("VolumeType") or ""
    return Disk(id=v.get("VolumeId") or "", name=tags(v).get("Name") or v.get("VolumeId") or "", subscription=account,
                resource_group="", region=region, sku=kind, size_gb=int(v.get("Size") or 0), tier="",
                state="Unattached" if v.get("State") == "available" else (v.get("State") or "").capitalize(),
                iops=v.get("Iops"), mbps=v.get("Throughput") if kind == GP3 else None, ade=False,
                created=v.get("CreateTime") or "", detached="", multi_attach=v.get("MultiAttachEnabled") is True,
                cloud="aws")


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
              subscription=account, resource_group="", region=region,
              zone=(i.get("Placement") or {}).get("AvailabilityZone") or "", size=i.get("InstanceType") or "",
              os_type="Windows" if (i.get("Platform") or "").lower() == "windows" else "Linux",
              power=((i.get("State") or {}).get("Name") or "").capitalize(), os_name=i.get("PlatformDetails") or "",
              os_version="", data=data, cloud="aws")


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


def aws_region(scope: Subscription) -> Tuple[List[Vm], List[Disk]]:
    """One region's instances and EBS volumes, each instance type's EBS limits, and which instances Systems Manager
    manages."""
    region, account = scope.id, scope.tenant
    reservations = (aws(["ec2", "describe-instances", "--filters", LIVE_STATES], region) or {}).get("Reservations")
    instances = [i for r in reservations or [] for i in r.get("Instances") or []]
    volumes = {v.get("VolumeId"): make_volume(v, account, region)
               for v in (aws(["ec2", "describe-volumes"], region) or {}).get("Volumes") or []}
    vms = [make_instance(i, account, region, volumes) for i in instances]
    try:
        types, missing = instance_types(sorted({v.size for v in vms if v.size}), region), ""
    except Failed as e:
        types, missing = {}, f"instance type limits not readable: {e}"
    for v, i in zip(vms, instances):
        if v.size in types:
            ebs_limits(v, i, types[v.size])
        else:
            v.facts_note = missing or f"{v.size or 'instance type'} not listed in {region}"
    if any(v.linux and v.data for v in vms):
        try:
            found = aws(["ssm", "describe-instance-information", "--filters", "Key=PingStatus,Values=Online",
                         "Key=ResourceType,Values=EC2Instance"], region) or {}
            managed, unmanaged = {m.get("InstanceId"): m for m in found.get("InstanceInformationList") or []}, ""
        except Failed as e:
            managed, unmanaged = {}, f"Systems Manager not readable: {e}"
        for v in vms:
            info = managed.get(v.id)
            if info:
                v.os_name, v.os_version = info.get("PlatformName") or v.os_name, info.get("PlatformVersion") or ""
            else:
                v.channel = unmanaged or "Systems Manager does not manage it (no SSM agent reporting)"
    return vms, list(volumes.values())


def aws_inventory(regions: List[Subscription]) -> Tuple[List[Vm], List[Disk]]:
    """Every EC2 instance and EBS volume in the regions, a few regions at a time; a region that fails has its note."""
    vms: List[Vm] = []
    disks: List[Disk] = []
    for s, result in zip(regions, parallel(aws_region, regions, "Reading AWS regions")):
        if isinstance(result, Failed):
            s.note = str(result)
            continue
        s.vms, s.disks = len(result[0]), len(result[1])
        vms += result[0]
        disks += result[1]
    return vms, disks


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
    family, redundancy, label = DISK_TYPES.get(d.sku.lower(), ("", "", d.sku))
    region_price = prices.get(d.region)
    if d.cloud == "aws":
        return price_volume(d, region_price)
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
    d.price_tier = tier
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
# The instances where Systems Manager took the command and gave back its id; a refused send-command ran nothing.
RUN_COMMAND_SENT: List[str] = []


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
    RUN_COMMAND_SENT.append(vm.id)
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
        key, _, value = line.partition("=")
        if key in ("ID", "ID_LIKE", "VERSION_ID") and value:
            value = value.strip().strip("\"'")
            if key == "ID":
                g.os_id = value.lower()
            elif key == "ID_LIKE":
                g.os_like = value.lower()
            else:
                g.os_version = value
            continue
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
            parts = rest_of_line.split(" ", 3)
            if len(parts) == 4 and parts[1].isdigit() and parts[2].isdigit():
                g.mounts[parts[3]] = (parts[0], int(parts[2]))
    if not g.kernel or g.btrfs not in ("loaded", "module", "absent"):
        raise Failed("the command's output was not readable")
    return g


def kernel_version(kernel: str) -> Optional[Tuple[int, int]]:
    match = re.match(r"(\d+)\.(\d+)", kernel or "")
    return (int(match.group(1)), int(match.group(2))) if match else None


def rhel_family(vm: Vm) -> bool:
    g = vm.guest
    if g and g.os_id:
        return g.os_id in RHEL_FAMILY or any(word in RHEL_LIKE for word in g.os_like.split())
    name = vm.os_name.lower()
    return any(word in name for word in ("rhel", "red hat", "redhat", "centos", "rocky", "alma", "oracle",
                                         "amazon linux"))


def os_label(vm: Vm) -> str:
    g = vm.guest
    if g and g.os_id:
        return f"{OS_NAMES.get(g.os_id, g.os_id.capitalize())} {g.os_version}".strip()
    if vm.os_name:
        return f"{OS_NAMES.get(vm.os_name.lower(), vm.os_name)} {vm.os_version}".strip()
    return vm.os_type.capitalize()


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
        raise Failed(f"not estimated: {count} gp3 volumes of {size:,} GB, larger than gp3's {GP3_MAX_GIB:,} GB")
    if need_iops > top_iops or need_mbps > top_mbps:
        raise Overloaded(f"not estimated: {count} gp3 volumes of {size:,} GB cannot carry the measured peak x 1.3")
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
        red.append(f"{vm.os_type or 'Non-Linux'} VM; Amoeba supports Linux")
    if vm.managed_by:
        red.append(f"in a resource group managed by {vm.managed_by}; Amoeba does not take VMs another Azure "
                   "service runs")
    if vm.cloud == "azure" and vm.slots is not None and vm.slots < MIN_VM_SLOTS:
        red.append(f"{vm.size} has {vm.slots} data-disk slots; Amoeba needs {MIN_VM_SLOTS} or more")
    if vm.nvme is False:
        red.append(f"{vm.size} is a Xen instance type, whose volumes are not NVMe devices named by volume id; "
                   "Amoeba needs a Nitro type")
    if vm.linux and rhel_family(vm):
        red.append("RHEL-family Linux; its stock kernel has no btrfs")
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
    return (f"not estimated: a Premium SSD pool of this size carries {has_iops:,} IOPS and {has_mbps:,} MB/s, "
            f"less than the measured peak x 1.3 ({math.ceil(need_iops):,} IOPS, {math.ceil(need_mbps):,} MB/s)")


def azure_pool(vm: Vm, g: Guest, mounts: Dict[str, List[int]], peak: Tuple[float, float, float, float],
               region_price: Prices) -> Optional[Tuple[int, float]]:
    """(GB, $/month) of the Premium SSD v2 or Premium SSD pool Amoeba builds on Azure; None when the free slots
    cannot hold it. Raises Failed (Overloaded when it cannot carry the load) with the reason when it is not
    estimated."""
    used = sum(max(1, math.ceil(g.mounts[m][1] / GIB)) for m in mounts)
    pool = pool_size_gib(used)
    layout = pool_members(pool, used, vm.slots, len(vm.data), vm.uncached_mbps, vm.pool_v2)
    if layout is None:
        return None
    data_members, small = layout
    if sum(data_members) < pool:
        raise Failed(f"{used:,} GB used is more than one pool holds; not estimated")
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
    layout = gp3_pool(vm, sum(g.mounts[m][1] for m in mounts), peak)
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
    """Used GB on the mounts the pool takes, and how full it makes the disks under them."""
    vm.used_gb = sum(g.mounts[m][1] for m in mounts) / GIB
    provisioned = sum(dd.size_gb for dd in under(vm, mounts))
    vm.util = vm.used_gb / provisioned if provisioned else None


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
    """Lane, used space and the saving for one VM; every blank figure gets its reason."""
    red, amber, notes = screen(vm), [], []
    g, unread = vm.guest, vm.guest_note
    aws_vm = vm.cloud == "aws"
    if g and not g.luns:
        g = None
        unread = vm.guest_note = ("the instance shows no EBS NVMe links (/dev/disk/by-id) to match its volumes"
                                  if aws_vm else "the VM shows no /dev/disk/azure/scsi1 links to match its data disks")
    shared_kind = "Multi-Attach volume" if aws_vm else "shared disk"
    shared = {dd.lun for dd in vm.data if dd.disk and dd.disk.shared}
    on_data = data_mounts(vm) if g else {}
    mounts = {m: luns for m, luns in on_data.items() if not shared & set(luns)}
    if g:
        version = kernel_version(g.kernel)
        if not rhel_family(vm) and g.btrfs not in ("loaded", "module"):
            red.append("btrfs is not available in this kernel")
        if version and version < KERNEL_FLOOR:
            amber.append(f"kernel {g.kernel} is below 5.14; upgrade the kernel first (Ubuntu 20.04: the HWE kernel)")
        if not mounts:
            red.append(f"its mounted data disks are {shared_kind}s, which Amoeba does not pool" if on_data
                       else "no mounted data disk found inside the VM")
    if any(dd.disk and dd.disk.ade for dd in vm.data):
        amber.append("Azure Disk Encryption is on; move to server-side encryption first")

    unpriced = [dd for dd in vm.data if not dd.disk or dd.disk.price is None]
    vm.current = None if unpriced else r2(sum(dd.disk.price for dd in vm.data))
    if unpriced:
        first = unpriced[0]
        notes.append(f"today's cost unknown: {first.note or first.disk.price_note}")

    pooled = sorted({lun for luns in mounts.values() for lun in luns})
    pooled_disks = [dd for dd in vm.data if dd.lun in pooled]
    missing = [m for m in mounts if m not in g.mounts] if g else []
    if g and mounts and not missing:
        measure(vm, g, mounts)
    elif missing:
        notes.append(f"df did not report {missing[0]}")
    if g and mounts:
        if shared:
            notes.append(f"{len(shared)} {shared_kind}(s) stay as they are; Amoeba does not pool a {shared_kind}")
        left = len([dd for dd in vm.data if dd.lun not in pooled and dd.lun not in shared])
        if left:
            notes.append(f"{left} data disk(s) without a mounted file system stay as they are")

    def finish(lane: str, why: str = "") -> None:
        vm.lane = lane
        vm.reasons = red + amber + ([why] if why else []) + notes

    if red:
        return finish("Not supported")
    if not g:
        return finish("Not checked", f"disk usage not read: {unread}" if unread else "")
    lane = "Needs work" if amber else "Ready"
    if missing or vm.used_gb is None:
        return finish(lane)
    if vm.slots is None:
        return finish(lane, f"VM size limits not readable: {vm.facts_note or 'size not listed'}")
    if any(dd.disk is None or dd.disk.price is None for dd in pooled_disks):
        return finish(lane)
    if any(dd.disk.hourly is None for dd in pooled_disks):
        bad = next(dd.disk for dd in pooled_disks if dd.disk.hourly is None)
        return finish(lane, f"IOPS not measured: {bad.metric_note or 'no metric data'}")

    try:
        sized, kept = plan_pool(vm, g, mounts, prices.get(vm.region) or Prices())
    except Failed as e:
        return finish(lane, str(e))
    if sized is None:
        amber.append("not enough free data-disk slots to build the pool beside today's disks")
        return finish("Needs work")
    if len(kept) < len(mounts):
        measure(vm, g, kept)
        pooled_disks = under(vm, kept)
        notes += [f"{m} stays on its {'volumes' if aws_vm else 'disks'}, as Amoeba leaves a mount whose load the "
                  "pool cannot carry" for m in mounts if m not in kept]
    vm.pool_gb, vm.pool_cost = sized[0], r2(sized[1])
    today = r2(sum(dd.disk.price for dd in pooled_disks))
    if today - vm.pool_cost <= 0:
        vm.no_saving = True
        return finish(lane, f"the pool (${vm.pool_cost:,.2f}/mo) would cost no less than these disks "
                            f"(${today:,.2f}/mo)")
    vm.saving = r2(today - vm.pool_cost)
    if any(dd.disk.sku.lower().endswith("_zrs") for dd in pooled_disks):
        notes.append("today's disks are zone-redundant (ZRS); the pool is locally redundant (LRS), and part of "
                     "the saving is that difference")
    finish(lane)


# ---------------------------------------------------------------- report

def money(v: Optional[float]) -> str:
    return "not available" if v is None else f"${v:,.2f}"


def gb(v: Optional[float]) -> Optional[int]:
    return None if v is None else int(round(v))


def attached_once(vms: List[Vm]) -> List[DataDisk]:
    """The VMs' data disks, each once: a shared disk shows on every VM it is attached to but is billed once."""
    seen, out = set(), []
    for v in vms:
        for dd in v.data:
            key = dd.disk.id.lower() if dd.disk else (v.id, dd.lun)
            if key not in seen:
                seen.add(key)
                out.append(dd)
    return out


def priced(disks: List[DataDisk]) -> List[float]:
    return [dd.disk.price for dd in disks if dd.disk and dd.disk.price is not None]


def no_saving_text(vms: List[Vm]) -> str:
    """What the saving figure says when no VM has a saving."""
    read = [v for v in vms if v.data and v.linux and v.guest and v.guest.luns]
    estimated = [v for v in vms if v.saving or v.no_saving]
    if not read:
        return "not estimated: no VM had its disk usage read"
    if estimated:
        return f"none found on the {len(estimated)} VM(s) estimated"
    return "not estimated on the VMs that were read; see the Reason column on the VMs sheet"


VM_COLS = [("Subscription", "text"), ("Resource group", "text"), ("VM", "text"), ("Region", "text"),
           ("Zone", "text"), ("Size", "text"), ("OS", "text"), ("Power state", "text"),
           ("Data-disk slots", "int"), ("Data disks", "int"), ("Provisioned GB", "int"), ("Used GB", "int"),
           ("Mounted-disk utilization %", "pct"), ("Current $/mo", "usd"), ("Pool disks", "text"),
           ("Pool GB", "int"), ("Pool $/mo", "usd"), ("Saving $/mo", "usd"), ("Lane", "text"), ("Reason", "text")]
DISK_COLS = [("VM", "text"), ("Resource group", "text"), ("Disk", "text"), ("LUN", "int"), ("Device", "text"),
             ("Disk type", "text"), ("Tier", "text"), ("GB", "int"), ("Provisioned IOPS", "int"),
             ("Provisioned MB/s", "int"), ("Peak IOPS (14 days)", "int"), ("Peak MB/s (14 days)", "int"),
             ("Mount", "text"), ("Used GB", "int"), ("$/mo", "usd"), ("Note", "text")]
UNATTACHED_COLS = [("Subscription", "text"), ("Resource group", "text"), ("Disk", "text"), ("Region", "text"),
                   ("Disk type", "text"), ("Tier", "text"), ("GB", "int"), ("$/mo", "usd"),
                   ("Days since detached", "int"), ("Note", "text")]
# Columns only one cloud fills. A run without that cloud leaves them out.
AZURE_COLS = {"Resource group", "LUN", "Tier", "Days since detached"}
AWS_COLS = {"Device"}


def fit(columns: list, rows: list, total: Optional[list], clouds: List[str]) -> Tuple[list, list, Optional[list]]:
    """The columns that apply to the clouds in the run, each row trimmed to them; the scope column named for them."""
    drop = (set() if "azure" in clouds else AZURE_COLS) | (set() if "aws" in clouds else AWS_COLS)
    keep = [i for i, (name, _) in enumerate(columns) if name not in drop]
    scope = {("azure",): "Subscription", ("aws",): "Account"}.get(tuple(clouds), "Subscription or account")
    cols = [(scope if columns[i][0] == "Subscription" else columns[i][0], columns[i][1]) for i in keep]
    return cols, [[r[i] for i in keep] for r in rows], [total[i] for i in keep] if total else None


def pool_disks(vm: Vm) -> str:
    """The disk type the VM's pool would use; blank where Amoeba cannot take the VM or it is not known."""
    if not vm.linux or vm.lane in ("", "Not supported"):
        return ""
    if vm.cloud == "aws":
        return GP3
    return "" if vm.pool_v2 is None else POOL_V2 if vm.pool_v2 else POOL_V1


def vm_rows(vms: List[Vm], names: Dict[str, str]) -> Tuple[list, list]:
    listed = sorted((v for v in vms if v.data), key=lambda v: (-(v.saving or 0), -(v.current or 0), v.name))
    rows = []
    for v in listed:
        saving = "No saving" if v.no_saving else v.saving
        rows.append([names.get(v.subscription, v.subscription), v.resource_group, v.name, v.region, v.zone, v.size,
                     os_label(v), v.power, v.slots, len(v.data), sum(dd.size_gb for dd in v.data), gb(v.used_gb),
                     v.util, v.current, pool_disks(v), v.pool_gb, v.pool_cost, saving, v.lane, "; ".join(v.reasons)])
    once = attached_once(listed)
    spend = priced(once)
    notes = []
    if spend and any(v.current is None for v in listed):
        notes.append("Current $/mo adds every priced disk, also on VMs whose own cell is blank")
    if len(once) < sum(len(v.data) for v in listed):
        notes.append("a shared disk counts once")
    total = ["TOTAL", "", "", "", "", "", "", "", None, len(once), sum(dd.size_gb for dd in once), None, None,
             r2(sum(spend)) if spend else None, "", None, None,
             r2(sum(v.saving for v in listed if v.saving)) if any(v.saving for v in listed) else None, "",
             "; ".join(notes)]
    return rows, total


def disk_rows(vms: List[Vm]) -> list:
    rows = []
    for v in sorted(vms, key=lambda v: (v.name, v.resource_group)):
        mounts = data_mounts(v)
        for dd in v.data:
            d = dd.disk
            if d is None:
                continue
            on_disk = sorted(m for m, luns in mounts.items() if dd.lun in luns)
            used, notes = None, [d.price_note] if d.price_note else []
            if d.shared:
                notes.append(("Multi-Attach volume" if d.cloud == "aws" else "shared disk") + "; not pooled")
            if on_disk and v.guest and all(m in v.guest.mounts for m in on_disk):
                if all(len(mounts[m]) == 1 for m in on_disk):
                    used = gb(sum(v.guest.mounts[m][1] for m in on_disk) / GIB)
                else:
                    notes.append("mount spans several disks; used space is on the VM row")
            peak_iops, peak_mbps = d.peak()
            if d.hourly is None:
                notes.append(d.metric_note)
            rows.append([v.name, v.resource_group, d.name, dd.lun if v.cloud == "azure" else None, dd.device, d.kind,
                         d.price_tier, d.size_gb, d.iops, d.mbps, gb(peak_iops), gb(peak_mbps), ", ".join(on_disk),
                         used, d.price, "; ".join(n for n in notes if n)])
    return rows


def unattached_rows(disks: List[Disk], names: Dict[str, str], now: datetime, clouds: List[str]) -> Tuple[list, list]:
    rows = []
    for d in sorted((d for d in disks if d.state.lower() == "unattached"), key=lambda d: (-(d.price or 0), d.name)):
        detached, created, note = parse_time(d.detached), parse_time(d.created), d.price_note
        days = (now - detached).days if detached else None
        if days is None:
            why = "" if "azure" not in clouds else ("AWS does not record when a volume was detached" if d.cloud == "aws"
                                                    else "Azure did not record when it was detached")
            when = f"created {created:%Y-%m-%d}" if created else ""
            note = "; ".join(x for x in (note, why, when) if x)
        rows.append([names.get(d.subscription, d.subscription), d.resource_group, d.name, d.region, d.kind,
                     d.price_tier, d.size_gb, d.price, days, note])
    known = [r[7] for r in rows if r[7] is not None]
    unknown = len(rows) - len(known)
    total = ["TOTAL", "", "", "", "", "", sum(r[6] for r in rows), r2(sum(known)) if known else None, None,
             f"{unknown} disk(s) not priced" if unknown else ""]
    return rows, total


def summary_rows(run: dict, vms: List[Vm], disks: List[Disk]) -> Tuple[list, set]:
    """Two columns, Item and Value, in titled sections."""
    rows, bold = [], set()

    def section(title: str) -> None:
        bold.add(len(rows))
        rows.append([title, ""])

    def item(label: str, value, fmt: str = "text") -> None:
        rows.append([label, (value, fmt)])

    with_data = [v for v in vms if v.data]
    linux = [v for v in with_data if v.linux]
    attached = attached_once(with_data)
    spend = priced(attached)
    linux_spend = priced(attached_once(linux))
    savings = [v for v in with_data if v.saving]
    total_saving = r2(sum(v.saving for v in savings)) if savings else None
    unestimated = [v for v in linux if v.saving is None and not v.no_saving and v.lane != "Not supported"]

    section("Run")
    for scan in run["scans"]:
        scanned = [s for s in scan.scopes if not s.note]
        noted = [s for s in scan.scopes if s.note] + scan.skipped
        if scan.cloud == "azure":
            item("Directory (tenant)", scan.label)
            item("Subscriptions scanned", len(scanned), "int")
            item("Subscriptions with VMs or disks", ", ".join(s.name for s in scanned if s.vms or s.disks) or "none")
            for s in noted:
                item(f"Not scanned: {s.name} ({s.id})", s.note)
        else:
            item("AWS account", scan.label)
            item("Regions scanned", len(scanned), "int")
            item("Regions with VMs or disks", ", ".join(s.name for s in scanned if s.vms or s.disks) or "none")
            for note, names in grouped(noted).items():
                item(f"Not scanned: {', '.join(names)}", note)
    item("Run on", run["when"])
    item("Script version", VERSION)

    section("Estate")
    item("VMs", len(vms), "int")
    item("VMs running", sum(1 for v in vms if v.running), "int")
    item("VMs with data disks", len(with_data), "int")
    item("Data disks attached", len(attached), "int")
    item("Data-disk capacity, GB", sum(dd.size_gb for dd in attached), "int")
    item("Data-disk spend today, $/mo", r2(sum(spend)) if spend else None, "usd")
    item("of which on Linux VMs, $/mo", r2(sum(linux_spend)) if linux_spend else None, "usd")
    unpriced = len(attached) - len(spend)
    if unpriced:
        item("Data disks not priced", f"{unpriced} (see the Reason column on the VMs sheet)")

    section("Zoorik Amoeba")
    if total_saving is None:
        item("Estimated saving, $/mo", no_saving_text(vms))
    else:
        item("Estimated saving, $/mo", total_saving, "usd")
        item("VMs with a saving", len(savings), "int")
        item("Saving as share of data-disk spend", total_saving / sum(spend) if spend else None, "pct")
    unestimated_spend = priced(attached_once([v for v in unestimated if v.current is not None]))
    if unestimated_spend:
        item("Spend on Linux VMs not yet estimated, $/mo", r2(sum(unestimated_spend)), "usd")
    for lane in ("Ready", "Needs work", "Not supported", "Not checked"):
        count = sum(1 for v in with_data if v.lane == lane)
        if count:
            item(f"Lane: {lane}", count, "int")
    item("VMs with disk usage read", sum(1 for v in linux if v.guest and v.guest.luns), "int")
    why_not: Dict[str, int] = {}
    for v in linux:
        if not (v.guest and v.guest.luns) and v.guest_note:
            why_not[v.guest_note] = why_not.get(v.guest_note, 0) + 1
    for note, count in sorted(why_not.items(), key=lambda kv: -kv[1]):
        item(f"Not read: {note}", count, "int")

    section("Unattached disks (not part of the Amoeba saving)")
    loose = [d for d in disks if d.state.lower() == "unattached"]
    loose_spend = [d.price for d in loose if d.price is not None]
    item("Unattached disks", len(loose), "int")
    item("Unattached capacity, GB", sum(d.size_gb for d in loose), "int")
    item("Unattached spend, $/mo", r2(sum(loose_spend)) if loose_spend or not loose else None, "usd")
    if len(loose) > len(loose_spend):
        item("Unattached disks not priced", f"{len(loose) - len(loose_spend)} (see the Unattached disks sheet)")

    section("Next step")
    item("Please e-mail this file to", SEND_TO)
    return rows, bold


def grouped(scopes: List[Subscription]) -> Dict[str, List[str]]:
    """Scopes by the reason they were not scanned: one line for every region an account policy refuses."""
    out: Dict[str, List[str]] = {}
    for s in scopes:
        out.setdefault(s.note, []).append(s.name)
    return out


BAND_TEXT = (f"After the move, Amoeba keeps the pool {BAND_LOW * 100:.0f}-{BAND_HIGH:.0%} full as the data grows "
             "and shrinks.")


def azure_about() -> Dict[str, str]:
    return {
        "The saving": "For each Linux VM: used space is read inside the VM (df). Zoorik Amoeba moves the "
                      "VM's data mounts onto one btrfs pool sized to the data: pool size "
                      f"= used + 20% (at least {HEADROOM_MIN_GIB} GB), rounded up, laid out as Amoeba plans "
                      "it (2 anchor disks and 3 to 6 equal elastic disks; on VMs with few free data-disk slots, "
                      "1 anchor and up to 3 elastics). A Premium SSD v2 pool adds one 8 GB disk. In a Premium SSD "
                      f"pool every disk is a Premium SSD size and elastics are at least {ELASTIC_FLOOR_V1_GIB} GB. "
                      "Saving = today's list price of the disks the pool replaces - the pool's list price. "
                      + BAND_TEXT,
        "Pool disks": "Premium SSD v2 wherever the VM's region and zone offer it (as Azure lists it for your "
                      "subscription; a VM without a zone also needs a region where Microsoft supports nonzonal "
                      "Premium SSD v2), otherwise Premium SSD. Pool disks are locally redundant (LRS).",
        "Performance": "Measured peak = the highest hourly figure of the last 14 days in Azure Monitor, where each "
                       "hour's figure is its busiest minute (Maximum) of Composite Disk Read/Write Operations/sec "
                       "and Bytes/sec, reads and writes added and summed over the disks the pool replaces. Each "
                       "Premium SSD v2 disk includes 3,000 IOPS and 125 MB/s; more is priced in only when the "
                       "measured peak x 1.3, shared across the pool's disks, needs it, and never more than the VM "
                       "can drive. A Premium SSD disk has the fixed IOPS and MB/s of its size (bursting not "
                       "counted). Where a Premium SSD pool cannot carry the measured peak x 1.3, the mount with the "
                       "highest peak IOPS (then MB/s) stays on its disks, as Amoeba leaves it, and the pool is "
                       "sized again for the others. If no mount is left, the VM is not estimated.",
        "Prices": "Public Azure Retail Prices API (prices.azure.com): pay-as-you-go list prices in USD for each "
                  f"disk's region, read at run time. A month is {HOURS_PER_MONTH} hours. Premium SSD, Standard SSD "
                  "and Standard HDD are priced by tier (a Premium SSD set to a higher performance tier at that "
                  "tier); Premium SSD v2 and Ultra Disk by provisioned GB, IOPS and MB/s.",
        "Not included": "Negotiated discounts, reservations, savings plans and credits; transaction charges, "
                        "bursting and snapshots; OS disks; unmanaged (VHD) disks; scale-set (uniform) instances; "
                        "subscriptions in other Azure directories.",
        "Lanes": "Ready: Amoeba can take the VM as it is. Needs work: a fix first (kernel 5.14 or later, "
                 "Azure Disk Encryption, free data-disk slots). Not supported: Windows; VMs in a resource group "
                 "another Azure service manages (such as Azure Databricks or an AKS node resource group); VM sizes "
                 f"with fewer than {MIN_VM_SLOTS} data-disk slots; RHEL-family Linux; no btrfs in the kernel; no "
                 "mounted data disk other than shared disks, which stay as they are. Not checked: disk usage was "
                 "not read.",
        "Units": "GB means GiB (2^30 bytes), the unit Azure sizes disks in. MB/s means 10^6 bytes per second.",
    }


def aws_about() -> Dict[str, str]:
    return {
        "The saving": "For each Linux instance: used space is read inside the instance (df). Zoorik Amoeba moves "
                      "the instance's data mounts onto one btrfs pool of gp3 volumes, laid out as Amoeba plans a new "
                      "pool on AWS. Pool size = used x 1.03 (btrfs's own space), held at most 90% full and with at "
                      f"least {FREE_FLOOR_GIB} GB free, rounded up to whole GB; one reading has no growth or burst "
                      f"to add. The pool is {MIN_MEMBERS} or more equal gp3 volumes, as few as the free volume slots "
                      "allow (one kept spare), each no larger than the instance can empty in 2 hours when it is 80% "
                      "full, reading and writing that data at 30% of the instance's EBS throughput, unless the slots "
                      "need larger ones. Saving = today's list price of the volumes the pool replaces - the pool's "
                      "list price. " + BAND_TEXT,
        "Pool disks": "gp3, the EBS volume type Amoeba creates, in the instance's Availability Zone.",
        "Performance": "Measured peak = the highest hourly figure of the last 14 days in CloudWatch (AWS/EBS), where "
                       "each hour's figure is its busiest minute: the Maximum of the 1-minute VolumeReadOps, "
                       "VolumeWriteOps, VolumeReadBytes and VolumeWriteBytes, divided by 60, reads and writes added "
                       "and summed over the volumes the pool replaces. Amoeba itself plans from its own readings "
                       "inside the instance; this estimate has CloudWatch's instead, the way the Azure estimate has "
                       "Azure Monitor's. Each gp3 volume includes 3,000 IOPS and 125 MB/s. As Amoeba sets them, each "
                       "volume gets the measured peak x 1.3 shared across the pool's volumes, and at least the write "
                       "peak x 1.3, within what a gp3 volume of its size takes (500 IOPS per GB, up to 80,000; "
                       "0.25 MB/s per IOPS, up to 2,000) and its share of what the instance can drive. Where the "
                       "pool's volumes cannot carry that, the mount with the highest peak IOPS (then MB/s) stays on "
                       "its volumes, as Amoeba leaves it, and the pool is sized again for the others. If no mount "
                       "is left, the instance is not estimated.",
        "Prices": "AWS Price List API (aws pricing get-products): on-demand list prices in USD for each volume's "
                  "region, read at run time. EBS bills by the month: gp3 its GB, the IOPS above 3,000 and the MB/s "
                  "above 125; io1 and io2 their GB and every provisioned IOPS (io2 in bands); gp2, st1 and sc1 "
                  "their GB. Magnetic (standard) volumes bill each I/O request and are not priced.",
        "Not included": "Negotiated discounts and credits; snapshots and I/O request charges; root volumes; "
                        "instance store; Multi-Attach volumes, which stay as they are; regions not enabled for the "
                        "account.",
        "Lanes": "Ready: Amoeba can take the instance as it is. Needs work: a fix first (kernel 5.14 or later, "
                 "free volume slots). Not supported: Windows; Xen instance types (Amoeba needs a Nitro type, where "
                 "each EBS volume is an NVMe device named by its volume id); RHEL-family Linux, Amazon Linux "
                 "included; no btrfs in the kernel; no mounted data volume other than Multi-Attach volumes, which "
                 "stay as they are. Not checked: disk usage was not read, for example because Systems Manager "
                 "does not manage the instance.",
        "Units": "GB means GiB (2^30 bytes), the unit AWS sizes volumes in. MB/s means MiB/s (2^20 bytes per "
                 "second), the unit gp3 throughput is set in.",
    }


READS = {"azure": "Azure Resource Graph (VMs, disks and resource groups), Azure Compute (VM size limits), Azure "
                  "Monitor (disk metrics) and the public price list",
         "aws": "EC2 (instances, volumes and instance types), Systems Manager (which instances it manages), "
                "CloudWatch (volume metrics) and the AWS Price List API"}


def ran_rows(cloud: str, ran_in: int, answer: str) -> List[Tuple[str, str]]:
    """What ran in one cloud, and inside its VMs."""
    name = CLOUD_NAMES[cloud]
    machine, machines = ("VM", "VMs") if cloud == "azure" else ("instance", "instances")
    inside = f"Inside the {machines}"
    if not ran_in:
        return [("What ran", f"Read calls only: {READS[cloud]}. Nothing in your {name} was changed, and nothing "
                             "was sent anywhere."),
                (inside, f"Nothing ran inside any {machine} ({answer}).")]
    if cloud == "azure":
        tool, how = "Azure Run Command", (
            "through Azure Run Command (RunShellScript). On its side, Azure added its Run Command extension "
            "(RunCommandLinux) to VMs that did not have it, kept a copy of the command and its output under "
            "/var/lib/waagent on each VM, and logged each run in the activity log.")
    else:
        tool, how = "Systems Manager Run Command", (
            f"through AWS Systems Manager Run Command ({SSM_DOCUMENT}). On its side, Systems Manager kept the "
            "command and its output in its Run Command history, its agent kept a copy under /var/lib/amazon/ssm "
            "on each instance, and CloudTrail logged each call.")
    return [("What ran", f"Read calls: {READS[cloud]}; plus {tool} in {ran_in} {machine}(s), described under "
                         f"{inside}. Nothing else in your {name} was changed, and nothing was sent anywhere."),
            (inside, f"With your yes, this command, which only reads, ran in each of those {machines} {how}\n"
                     + GUEST_SCRIPT[cloud])]


def about_rows(run: dict) -> list:
    scans = run["scans"]
    both = len(scans) > 1
    names = " and ".join(CLOUD_NAMES[s.cloud] for s in scans)
    rows = [("What this is", f"An estimate, not a quote. The figures come from your {names} and from list prices "
                             "at the time of the run.")]
    texts = {s.cloud: azure_about() if s.cloud == "azure" else aws_about() for s in scans}
    for topic in ("The saving", "Pool disks", "Performance", "Prices", "Not included", "Lanes"):
        rows += [(f"{topic} ({CLOUD_NAMES[c]})" if both else topic, t[topic]) for c, t in texts.items()]
    rows += [("Used space", "Used GB is the space used on the data-disk mounts the pool would take, from df inside "
                            "the VM. Mounted-disk utilization % = Used GB / the size of the data disks under those "
                            "mounts; Provisioned GB counts every data disk."),
             ("Blank cells", "A blank figure could not be known; the Reason or Note column says why.")]
    rows += [(f"Units ({CLOUD_NAMES[c]})" if both else "Units", t["Units"]) for c, t in texts.items()]
    for s in scans:
        ran_in = len(RUN_COMMAND_SENT if s.cloud == "aws" else RUN_COMMAND_STARTED["azure"])
        rows += [(f"{topic} ({CLOUD_NAMES[s.cloud]})" if both else topic, detail)
                 for topic, detail in ran_rows(s.cloud, ran_in, s.guest_answer)]
    rows.append(("Prepared by", f"Zoorik Amoeba: disk assessment {VERSION}. Run it again: {ONE_LINER}  "
                                f"Questions: {SEND_TO}"))
    return [list(r) for r in rows]


def build_sheets(run: dict, vms: List[Vm], disks: List[Disk]) -> list:
    clouds = [s.cloud for s in run["scans"]]
    names: Dict[str, str] = {}
    for scan in run["scans"]:
        prefix = f"{CLOUD_NAMES[scan.cloud]}: " if len(clouds) > 1 else ""
        scoped = {s.id: s.name for s in scan.scopes} if scan.cloud == "azure" else {scan.label: scan.label}
        names.update({k: prefix + v for k, v in scoped.items()})
    summary, bold = summary_rows(run, vms, disks)
    vm_cols, vm_list, vm_total = fit(VM_COLS, *vm_rows(vms, names), clouds)
    disk_cols, disk_list, _ = fit(DISK_COLS, disk_rows(vms), None, clouds)
    loose_cols, loose, loose_total = fit(UNATTACHED_COLS, *unattached_rows(disks, names, run["now"], clouds), clouds)
    return [
        Sheet("Summary", [("Item", "text"), ("Value", "text")], summary, bold_rows=bold, widths=[44, 90],
              autofilter=False, preamble=[("Zoorik Amoeba: disk assessment", "title"), ("", "")]),
        Sheet("VMs", vm_cols, vm_list, total=vm_total),
        Sheet("Disks", disk_cols, disk_list),
        Sheet("Unattached disks", loose_cols, loose, total=loose_total),
        Sheet("About", [("Topic", "text"), ("Detail", "text")], about_rows(run), widths=[28, 120],
              autofilter=False),
    ]


# ---------------------------------------------------------------- xlsx writer

_FORMATS = {"text": 0, "int": 3, "usd": 164, "pct": 165}


def _cell_styles() -> tuple:
    """(numFmt, font, fill, border, wrap) per style, and the index of each style."""
    xfs = [(0, 0, 0, 0, False), (0, 1, 2, 1, False), (0, 2, 0, 0, False), (0, 0, 0, 0, True)]
    index = {"header": 1, "title": 2, "wrap": 3}
    for bold in (False, True):
        for fmt, num in _FORMATS.items():
            index[(fmt, bold)] = len(xfs)
            xfs.append((num, 1 if bold else 0, 0, 0, False))
    return xfs, index


_XFS, STYLE = _cell_styles()

_NS = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
_RNS = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
_BAD_XML = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f]")


@dataclass
class Sheet:
    name: str
    columns: list
    rows: list
    preamble: list = field(default_factory=list)
    total: Optional[list] = None
    widths: Optional[list] = None
    autofilter: bool = True
    bold_rows: set = field(default_factory=set)


def col_letter(i: int) -> str:
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def cell_xml(ref: str, value, style: int) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value != value or value in (float("inf"), float("-inf")):
            return ""
        return f'<c r="{ref}" s="{style}"><v>{value!r}</v></c>'
    text = escape(_BAD_XML.sub("", str(value))[:32767])
    return f'<c r="{ref}" s="{style}" t="inlineStr"><is><t xml:space="preserve">{text}</t></is></c>'


def shown_length(value, fmt: str) -> int:
    if isinstance(value, tuple):
        value, fmt = value
    if value is None:
        return 0
    if isinstance(value, str):
        return len(value)
    if fmt == "int":
        return len(f"{value:,.0f}")
    if fmt == "pct":
        return len(f"{value * 100:.1f}%")
    if fmt == "usd":
        return len(f"${value:,.2f}")
    return len(str(value))


def sheet_xml(s: Sheet) -> tuple:
    head = len(s.preamble) + 1
    last_col = col_letter(len(s.columns) - 1)
    rows = s.rows + ([s.total] if s.total else [])
    widths = s.widths or [min(60, max([len(name)] + [shown_length(r[i], fmt) for r in rows]) + 2)
                          for i, (name, fmt) in enumerate(s.columns)]
    out = [f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<worksheet {_NS} {_RNS}>',
           '<sheetViews><sheetView workbookViewId="0" showGridLines="0">'
           f'<pane ySplit="{head}" topLeftCell="A{head + 1}" activePane="bottomLeft" state="frozen"/>'
           '<selection pane="bottomLeft"/></sheetView></sheetViews>',
           '<sheetFormatPr defaultRowHeight="15"/><cols>']
    out += [f'<col min="{i + 1}" max="{i + 1}" width="{w}" customWidth="1"/>' for i, w in enumerate(widths)]
    out.append("</cols><sheetData>")
    for r, (text, style) in enumerate(s.preamble, 1):
        out.append(f'<row r="{r}">{cell_xml(f"A{r}", text, STYLE[style] if style else 0)}</row>')
    out.append(f'<row r="{head}">' + "".join(cell_xml(f"{col_letter(i)}{head}", name, STYLE["header"])
                                             for i, (name, _) in enumerate(s.columns)) + "</row>")
    body = [(row, k in s.bold_rows) for k, row in enumerate(s.rows)] + ([(s.total, True)] if s.total else [])
    for n, (row, bold) in enumerate(body, head + 1):
        cells = []
        for i, (value, (_, fmt)) in enumerate(zip(row, s.columns)):
            if isinstance(value, tuple):
                value, fmt = value
            last = i == len(s.columns) - 1
            style = STYLE["wrap"] if s.widths and fmt == "text" and last and not bold else STYLE[(fmt, bold)]
            cells.append(cell_xml(f"{col_letter(i)}{n}", value, style))
        out.append(f'<row r="{n}">{"".join(cells)}</row>')
    out.append("</sheetData>")
    ref = f"A{head}:{last_col}{head + len(s.rows)}" if s.autofilter and s.rows else ""
    if ref:
        out.append(f'<autoFilter ref="{ref}"/>')
    out.append("</worksheet>")
    return "".join(out), ref


def styles_xml() -> str:
    xfs = "".join(
        f'<xf numFmtId="{num}" fontId="{font}" fillId="{fill}" borderId="{border}" xfId="0" applyNumberFormat="1" '
        'applyFont="1" applyFill="1" applyBorder="1"'
        + ('><alignment wrapText="1" vertical="top"/></xf>' if wrap else "/>")
        for num, font, fill, border, wrap in _XFS)
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            f'<styleSheet {_NS}><numFmts count="2"><numFmt numFmtId="164" formatCode="&quot;$&quot;#,##0.00"/>'
            '<numFmt numFmtId="165" formatCode="0.0%"/></numFmts>'
            '<fonts count="3"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/>'
            '<name val="Calibri"/></font><font><b/><sz val="14"/><name val="Calibri"/></font></fonts>'
            '<fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/>'
            '</fill><fill><patternFill patternType="solid"><fgColor rgb="FFE8EEF7"/><bgColor indexed="64"/>'
            '</patternFill></fill></fills><borders count="2"><border><left/><right/><top/><bottom/><diagonal/></border>'
            '<border><left/><right/><top/><bottom style="thin"><color rgb="FF9AA5B1"/></bottom><diagonal/></border>'
            '</borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
            f'<cellXfs count="{len(_XFS)}">{xfs}</cellXfs><cellStyles count="1">'
            '<cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>')


def write_xlsx(path: str, sheets: list) -> None:
    """Write the sheets as a minimal .xlsx with the standard library only."""
    sheets = [s for s in sheets if s.rows]
    parts, names = {}, []
    for i, s in enumerate(sheets, 1):
        xml, ref = sheet_xml(s)
        parts[f"xl/worksheets/sheet{i}.xml"] = xml
        if ref:
            absolute = re.sub(r"([A-Z]+)(\d+)", r"$\1$\2", ref)
            names.append(f'<definedName name="_xlnm._FilterDatabase" localSheetId="{i - 1}" hidden="1">'
                         f"'{s.name}'!{absolute}</definedName>")
    sheet_list = "".join(f'<sheet name="{escape(s.name)}" sheetId="{i}" r:id="rId{i}"/>'
                         for i, s in enumerate(sheets, 1))
    parts["xl/workbook.xml"] = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<workbook {_NS} {_RNS}>'
                                f'<bookViews><workbookView/></bookViews><sheets>{sheet_list}</sheets>'
                                + (f"<definedNames>{''.join(names)}</definedNames>" if names else "") + "</workbook>")
    rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    parts["xl/_rels/workbook.xml.rels"] = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        + "".join(f'<Relationship Id="rId{i}" Type="{rel}/worksheet" Target="worksheets/sheet{i}.xml"/>'
                  for i in range(1, len(sheets) + 1))
        + f'<Relationship Id="rId{len(sheets) + 1}" Type="{rel}/styles" Target="styles.xml"/></Relationships>')
    parts["xl/styles.xml"] = styles_xml()
    parts["_rels/.rels"] = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                            f'<Relationship Id="rId1" Type="{rel}/officeDocument" Target="xl/workbook.xml"/>'
                            '</Relationships>')
    ct = "application/vnd.openxmlformats-officedocument.spreadsheetml"
    parts["[Content_Types].xml"] = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        f'<Override PartName="/xl/workbook.xml" ContentType="{ct}.sheet.main+xml"/>'
        f'<Override PartName="/xl/styles.xml" ContentType="{ct}.styles+xml"/>'
        + "".join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="{ct}.worksheet+xml"/>'
                  for i in range(1, len(sheets) + 1)) + "</Types>")
    order = ["[Content_Types].xml", "_rels/.rels", "xl/workbook.xml", "xl/_rels/workbook.xml.rels", "xl/styles.xml"]
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name in order + sorted(k for k in parts if k not in order):
            z.writestr(name, parts[name])


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


def collect_facts(vms: List[Vm], disks: List[Disk]) -> Dict[str, Optional[Prices]]:
    """Azure's VM size limits per subscription and region, and list prices per region, read in parallel. AWS's
    instance limits come with its inventory."""
    azure_vms = [v for v in vms if v.cloud == "azure"]
    keys = sorted({(v.subscription, v.region) for v in azure_vms if re.fullmatch(r"[a-z0-9]+", v.region)})
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
    path, n = os.path.join(folder, stem + ".xlsx"), 1
    while os.path.exists(path):
        n += 1
        path = os.path.join(folder, f"{stem}-{n}.xlsx")
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


def read_scopes(scan: Scan) -> Tuple[List[Vm], List[Disk]]:
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
    RUN_COMMAND_SENT.clear()
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
    disks: List[Disk] = []
    for scan in scans:
        found = read_scopes(scan)
        vms += found[0]
        disks += found[1]
    if not vms and not disks:
        if all(s.note for scan in scans for s in scan.scopes):
            say("Nothing could be read, so there is nothing to report. Fix the reason above, then run this again.")
            return 1
        say("No VMs or disks in what was scanned, so there is nothing to report.")
        return 0
    prices = collect_facts(vms, disks)

    candidates = []
    for v in vms:
        if not v.data or not v.linux or screen(v):
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
        scan.guest_answer = {True: "", False: "declined at the prompt", None: "no terminal to ask on"}[answer]
        if answer:
            read = read_guest if scan.cloud == "azure" else read_instance
            for v, result in zip(group, parallel(read, group, "Reading disk usage, VMs")):
                if isinstance(result, Guest):
                    v.guest = result
                else:
                    v.guest_note = str(result)
        else:
            for v in group:
                v.guest_note = scan.guest_answer

    measured = []
    for v in vms:
        for dd in v.data:
            if dd.disk and not v.linux:
                dd.disk.metric_note = "not measured (Windows VM)"
            elif dd.disk and v.nvme is False:
                dd.disk.metric_note = "not measured (Xen instance type)"
            elif dd.disk:
                measured.append(dd.disk)
    for d, result in zip(measured, parallel(disk_activity, measured, f"Reading {METRIC_DAYS} days of disk metrics")):
        if isinstance(result, dict) and result:
            d.hourly = result
        else:
            d.metric_note = f"metrics not readable: {result}" if isinstance(result, Failed) else \
                f"no metric data in the last {METRIC_DAYS} days"
    for v in vms:
        try:
            assess(v, prices)
        except Exception as e:  # one VM with unexpected data must not stop the report
            v.lane, v.reasons = "", [f"not assessed: unexpected data ({type(e).__name__})"]

    now = datetime.now(timezone.utc)
    run = {"scans": scans, "now": now, "when": now.strftime("%Y-%m-%d %H:%M UTC")}
    sheets = build_sheets(run, vms, disks)
    stem = f"zoorik-disk-assessment-{now:%Y%m%d-%H%M}"
    path = ""
    for folder in (os.getcwd(), os.path.expanduser("~")):
        try:
            path = unused_path(folder, stem)
            write_xlsx(path, sheets)
            break
        except OSError:
            path = ""
    if not path:
        say(f"Could not write {stem}.xlsx here or in your home folder.")
        return 1

    with_data = [v for v in vms if v.data]
    attached = attached_once(with_data)
    spend = priced(attached)
    unpriced = len(attached) - len(spend)
    savings = [v.saving for v in with_data if v.saving]
    loose = [d for d in disks if d.state.lower() == "unattached"]
    loose_spend = [d.price for d in loose if d.price is not None]
    total_spend = r2(sum(spend)) if spend else None
    say()
    say(f"VMs:               {len(vms)} scanned, {len(with_data)} with data disks")
    say(f"Data-disk spend:   {money(total_spend)}" + (" per month at list prices" if spend else "")
        + (f" ({unpriced} data disk(s) not priced)" if unpriced else ""))
    if savings:
        share = f" ({sum(savings) / total_spend:.0%} of data-disk spend)" if total_spend else ""
        say(f"Estimated saving:  {money(r2(sum(savings)))} per month on {len(savings)} VM(s){share}")
    else:
        say(f"Estimated saving:  {no_saving_text(vms)}")
    say(f"Unattached disks:  {len(loose)}" + (f", {money(r2(sum(loose_spend)))} per month" if loose_spend else "")
        + (f" ({len(loose) - len(loose_spend)} not priced)" if len(loose) > len(loose_spend) else ""))
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
