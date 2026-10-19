# Zoorik assessments

Zoorik's free cloud cost assessment for Coral (Kubernetes) and Amoeba (block storage). One command estimates how much you can reduce your cloud bill. It sends nothing anywhere: it writes one PDF report for you to email to founders@zoorik.com.

```bash
npx zoorik assess coral     # Kubernetes: AKS, EKS, or any cluster kubectl can reach
npx zoorik assess amoeba    # block storage: the data disks of your VMs
```

Or with pip: `pip install zoorik`, then `zoorik assess coral` or `zoorik assess amoeba`. Either way it needs Python 3.8 or later. It runs in your terminal on Linux, macOS or Windows (PowerShell or Command Prompt), or in Azure Cloud Shell or AWS CloudShell, which already have everything it calls.

Where there is no `pip` command (macOS's own Python has only `pip3`), run `python3 -m pip install zoorik`, or on Windows `py -m pip install zoorik`. Where pip stops with `externally-managed-environment` (Homebrew Python, Debian 12, Ubuntu 23.04 and later), run `pipx install zoorik` instead, or use `npx`. Where pip installs it but your shell can't find `zoorik`, run `python3 -m zoorik assess coral` (on Windows, `py -m zoorik assess coral`).

- [Kubernetes: `k8s.py`](#kubernetes-k8spy)
- [Disks: `disks.py`](#disks-diskspy)
- [Where the figures come from](#where-the-figures-come-from)
- [Contributing](#contributing)

Each assessment is one Python file in this repo, using only the standard library. The npm and PyPI packages run these same files, and every release of both is built from a release tag of this repo by its [release workflow](https://github.com/getzoorik/assessment/blob/main/.github/workflows/release.yaml). The `zoorik.com/assess/...` one-liners below redirect to the files here, so the code you read is the code that runs.

---

## Kubernetes: k8s.py

```bash
zoorik assess coral
curl -fsSL https://zoorik.com/assess/k8s | python3 -
```

The `curl` line is for bash or zsh: Linux, macOS and both Cloud Shells. On Windows, run `npx zoorik assess coral`, or `zoorik assess coral` after `py -m pip install zoorik`, in PowerShell or Command Prompt.

### Where to run it

- **Azure Cloud Shell (Bash):** it assesses every AKS cluster in the current subscription.
- **AWS CloudShell:** it assesses every EKS cluster in every enabled region.
- **Any other shell:**
  - it assesses the AKS clusters in the current subscription if `az` is signed in;
  - it assesses the EKS clusters if `aws` is signed in;
  - if neither finds a cluster, it assesses the current kubectl context.

  Outside a Cloud Shell, on Linux, macOS or Windows, it needs Python 3.8 or later, `kubectl`, and the `az` or `aws` CLI for that cloud.

A run takes a little over 3 minutes. It reads each cluster, then samples usage for 3 minutes. Each private AKS cluster adds about a minute, and many or large clusters take longer.

Both Cloud Shells close a session after about 20 minutes without a key press, even while the script runs. On a long run, press Enter now and then until the report path appears. The script warns you when a run may be long.

### What it reads

- **In each cluster, with `kubectl get`:** nodes, pods, ReplicaSets and Jobs.
- **With `kubectl top`:** container usage, every 30 seconds. It keeps the peak.
- **What it keeps:**
  - names, owners, requests and limits;
  - one pod label, `pod-template-hash`, used only to match pods to their Deployment;
  - each node's machine type, node pool, region, operating system and allocatable CPU, memory and pods, and whether it is Spot or on-demand.

  It never reads Secrets or ConfigMaps. It never keeps env values, annotations or pod IPs.
- **With `az` or `aws`:** the signed-in account, its clusters, node pools and regions. It also writes each cluster's credentials to a temporary kubeconfig.
- **Prices:**
  - for AKS, the public Azure Retail Prices API (prices.azure.com);
  - for EKS, the AWS Price List API and EC2 Spot price history, through `aws`.

### Beyond reads: one short-lived pod, only in AKS clusters it can't read directly

The script first tries kubectl. Some AKS clusters can't be read that way from where you run it:

- a private AKS cluster, from outside its network;
- an AKS cluster that kubectl can't reach from there;
- an AKS cluster kubectl can't sign in to, because kubelogin is not installed.

For these, the script uses `az aks command invoke`. Azure starts a short-lived pod of its own in the cluster's `aks-command` namespace, and the same kubectl reads run in that pod.

- Azure documents this pod as requesting 200m CPU and 500 MiB of memory. The report leaves it out.
- Azure records each call in the activity log.
- Azure caps each call's output at 512 KB.
- A cluster read this way gives one usage snapshot instead of 3 minutes of samples.

`--no-invoke` turns this off. kubectl then has to reach those clusters directly, and any it can't reach are listed as not assessed, with the reason.

Apart from this pod, the script only reads, and it writes only the files below.

### What it sends

Nothing. There is no upload and no telemetry, and you send the report yourself. It switches off the Azure CLI's own usage telemetry for its calls, without changing your az settings. Its only calls go to:

- your clusters;
- your cloud's own APIs;
- the public Azure price list, which receives a region and a machine type for each lookup.

### What it writes

- **One PDF report,** `zoorik-coral-assessment-YYYYMMDD-HHMM.pdf` (UTC).
  - In a Cloud Shell it goes in your home folder. Elsewhere it goes in the current directory, or in your home folder if the current directory can't be written. `--out` chooses another place.
  - It shows what you can save across your clusters, then by cluster and node pool, the recommended sizes for your workloads, and any cluster it could not assess, with the reason.
  - It names your clusters, their regions and node pools, and your namespaces and workloads. `--anonymize` replaces namespace and workload names, and `--anonymize-clusters` cluster names too.
- **Working files:** kubectl's cache and the temporary kubeconfig, and on Windows any `az` argument that `cmd.exe` would change (`az` is a batch file there). They go in a temporary folder that is deleted when the script exits. Your own kubeconfig is left as it is.

### Access it needs

**Azure:**
- read access to the subscription's AKS clusters;
- the *Azure Kubernetes Service Cluster User Role* on each cluster;
- for `command invoke`, also `Microsoft.ContainerService/managedClusters/runcommand/action` and `Microsoft.ContainerService/managedClusters/commandResults/read`.

**AWS:**
- `eks:ListClusters`, `eks:DescribeCluster`, `eks:ListNodegroups` and `eks:DescribeNodegroup`;
- `ec2:DescribeRegions` and `ec2:DescribeSpotPriceHistory`;
- `pricing:GetProducts`;
- an EKS access entry for the identity.

The identity that created the cluster usually has an access entry already. For any other identity, choose one of these:
- Associate `AmazonEKSAdminViewPolicy` at cluster scope. This policy can also read Secrets, but the script never asks for them.
- Put a Kubernetes group in the access entry, and bind that group to the role below, which allows only `get` and `list`.

`AmazonEKSViewPolicy` is not enough: it can't list nodes or read metrics.

**Inside each cluster:** the identity needs `get` and `list`, cluster-wide, on the resources the script reads. If it doesn't have them, a cluster admin can create this role and bind it. Replace `GROUP` with the Kubernetes group from the EKS access entry, or with the Entra ID group's object ID on AKS:

```bash
kubectl create clusterrole zoorik-assess-read --verb=get,list --resource=nodes,pods,replicasets.apps,jobs.batch,pods.metrics.k8s.io
kubectl create clusterrolebinding zoorik-assess-read --clusterrole=zoorik-assess-read --group=GROUP
```

When the script can't read a cluster, the run goes on to the next one, and the report lists it under Not in the saving yet, with the reason.

### Options

Put options after `zoorik assess coral`, or after the dash of the `curl` line:

```bash
npx zoorik assess coral --anonymize
curl -fsSL https://zoorik.com/assess/k8s | python3 - --anonymize
```

| Option | Effect |
|---|---|
| `--anonymize` | Replace namespace and workload names with short hashes, salted for this run. Node pool names stay. |
| `--anonymize-clusters` | Also replace cluster names. |
| `--sample-minutes N` | Sample usage for N minutes (default 3; 0 takes one sample). |
| `--subscription ID` | Search this Azure subscription instead of the current one. |
| `--region NAME` | Search this AWS region; repeat it for more (default: every enabled region). |
| `--discover azure`, `--discover aws` | Find clusters through `az` or `aws`. |
| `--context NAME` | Assess this kube context; repeat it for more. |
| `--all-contexts` | Assess every context in your kubeconfig. A cluster reached through several contexts is counted once. |
| `--no-invoke` | Never use `az aks command invoke`. |
| `--no-prices` | Skip the price lookups. The report then counts machines, with no cost or savings in dollars. |
| `--out PATH` | Write the report to this file or folder. A path ending in `/` is a folder, created if needed; a file name gets `.pdf` added if it lacks it. |

`--subscription`, `--region`, `--discover`, `--context` and `--all-contexts` replace the automatic choice of clusters: only what you name is assessed. `--help` lists every option.

### Download the report and send it

When the script finishes, it prints the report's full path.

- **Azure Cloud Shell:** on the toolbar, choose **Manage files > Download**, then enter that path.
- **AWS CloudShell:** choose **Actions > Download file**, then enter that path. This works only from the full CloudShell page. If **Actions** is missing, open CloudShell in a new browser tab.

Then e-mail the report to **founders@zoorik.com**.

### How the estimate works

**1. Right-size the requests.** For each container:
- the recommended request is the peak sampled usage × 1.3;
- it is at least 10m CPU and 32 MiB of memory;
- it is never above today's request, unless today's request is zero.

Exceptions:
- with no usage data, today's request stays;
- init containers keep their requests, except native sidecars (`restartPolicy: Always`), which are right-sized like the other running containers;
- pods with pod-level requests keep them as they are.

**2. Count the nodes needed.** This is worked out per node pool and machine type. DaemonSet and static pods count as per-node overhead. Nodes needed is the largest of these:
- ceil(recommended CPU / ((allocatable CPU per node − overhead per node) × 0.85));
- the same for memory;
- ceil(workload pods / ((allocatable pods per node − DaemonSet and static pods per node) × 0.85)).

It is never more than today's count. Each pool keeps at least its autoscaler minimum, and at least 1 node, across its machine types. Where the cloud API gives no minimum for a pool (a cluster read through a kube context, or a node group the API doesn't list), the floor is 1 node.

**3. Price the difference.**

Savings per month = (nodes today − nodes needed) × the node's hourly list price × 730 hours.

**Prices** are USD list prices:
- **Azure:** Linux pay-as-you-go, or the Spot meter for Spot nodes;
- **AWS:** Linux on-demand with shared tenancy, or for Spot nodes the current Spot price averaged across zones.

They leave out discounts, reservations, savings plans and credits. Nodes whose labels don't say Spot or on-demand, such as self-managed EKS node groups, are not priced.

**Usage:**
- Three minutes of samples can miss peaks. Use `--sample-minutes` for a longer window.
- Without metrics-server there is no usage. Requests then stay as they are, and only surplus nodes count.

**Not included:**
- Further savings: moving to Spot, moving to cheaper or newer machine types, and packing across pools.
- Other costs: storage, network, load balancers, control-plane fees and licences. Windows nodes are not priced.
- Pending pods.

**Limits:**
- **Private EKS:** a private EKS API can't be reached from CloudShell. Run the line from a machine inside the VPC instead, such as an EC2 instance. A CloudShell VPC environment won't do: it can't download files. Run from CloudShell, the report lists that cluster under Not in the saving yet, with the reason and, where its node groups have list prices, today's cost.
- **Large clusters through `command invoke`:** these are read one resource kind at a time. If the nodes or pods are still over 512 KB, the report lists the cluster under Not in the saving yet, with the reason.

---

## Disks: disks.py

```bash
zoorik assess amoeba
curl -fsSL https://zoorik.com/assess/disks | python3 -
```

The `curl` line is for bash or zsh: Linux, macOS and both Cloud Shells. On Windows, run `npx zoorik assess amoeba`, or `zoorik assess amoeba` after `py -m pip install zoorik`, in PowerShell or Command Prompt. Piped into Python there, the script could not ask its y/N question.

It assesses the data disks of your Linux VMs on Azure and of your Linux EC2 instances on AWS.

### Where to run it

- **Azure Cloud Shell (Bash):** it assesses Azure.
- **AWS CloudShell:** it assesses AWS.
- **Any other terminal, on Linux, macOS or Windows:** it assesses each cloud whose CLI is signed in: Azure if `az` is, AWS if `aws` is, both if both are. It needs Python 3.8 or later and the `az` or `aws` CLI.

Both Cloud Shells close a session after about 20 minutes without a key press. On a long run, press Enter every few minutes until the report is written.

- **Azure:** it assesses every enabled subscription in the Azure directory (tenant) you are signed in to. Subscriptions in other directories are listed as not scanned. To include one, run `az login --tenant <tenant-id>`, then run it again.
- **AWS:** it assesses every enabled region of the account you are signed in to. A region the account's policies don't let it read is listed as not scanned, with the reason.

### What it reads

**Azure:**
- **Azure Resource Graph:** your VMs and managed disks, and which resource groups another Azure service manages.
- **Azure Compute:**
  - each VM size's data-disk slots;
  - each VM size's uncached disk IOPS and throughput limits;
  - which zones offer Premium SSD v2 to your subscription.
- **Azure Monitor:** 14 days of IOPS and throughput for the data disks the pool would replace, on each VM whose disk usage was read, as each hour's busiest minute. The metrics are Composite Disk Read/Write Operations/sec and Bytes/sec, with the Maximum aggregation.
- **prices.azure.com:** the public list prices of managed disks.

**AWS, in each enabled region:**
- **EC2:** your instances and EBS volumes, and the EBS limits of each instance type with data volumes (volume slots, baseline IOPS and throughput, whether its volumes are NVMe devices).
- **Systems Manager:** which instances its agent manages.
- **CloudWatch:** 14 days of IOPS and throughput for the data volumes the pool would replace, on each instance whose disk usage was read, as each hour's busiest minute. The metrics are VolumeReadOps, VolumeWriteOps, VolumeReadBytes and VolumeWriteBytes, with the Maximum statistic over AWS's 1-minute data points. These requests count toward CloudWatch's free tier of 1 million API requests a month.
- **The AWS Price List API:** EBS list prices, through `aws pricing get-products`.

### Beyond reads: one command inside each VM, only if you answer y

The saving needs the used space on each data disk, and only the VM itself can report that.

The script first finds the running Linux VMs that have data disks and pass its first checks. If there are any, it lists them, prints the command below, says how long it will take, and asks once for each cloud:

```
Read disk usage inside these N VM(s) with Azure Run Command? [y/N]
Read disk usage inside these N instance(s) with Systems Manager Run Command? [y/N]
```

**If you answer y**, the command runs in each of those VMs, 8 at a time. It reads:
- the kernel version;
- whether btrfs is available;
- which mounts sit on each data disk;
- used space, from `df`.

**On Azure,** it runs through Azure Run Command (`RunShellScript`). On its side, Azure:
- adds its Run Command extension (RunCommandLinux) to any VM that doesn't have it yet;
- keeps a copy of the command and its output under `/var/lib/waagent` on the VM;
- records each run in the activity log.

**On AWS,** it runs through AWS Systems Manager Run Command (`AWS-RunShellScript`), only in instances Systems Manager manages. On its side:
- Systems Manager keeps the command and its output in its Run Command history;
- its agent keeps a copy under `/var/lib/amazon/ssm` on the instance;
- CloudTrail records each call.

**If you answer anything else, or there is no terminal to ask on:** nothing runs inside any VM. The report lists those VMs under Not in the saving yet, with the reason.

**Ctrl-C** stops the script at once, and no new command starts after it. Commands already started finish in the cloud, and the script's last line says how many VMs they reached.

This is the command. The two clouds differ only in the fourth line, which finds the data disks:

```sh
echo S
echo "K $(uname -r)"
if grep -qw btrfs /proc/filesystems; then echo "B loaded"; elif modinfo -n btrfs >/dev/null 2>&1; then echo "B module"; else echo "B absent"; fi
for l in /dev/disk/azure/scsi1/lun*; do n=${l##*/lun}; case $n in ''|*[!0-9]*) continue;; esac; echo "L $n $(lsblk -nro MOUNTPOINT "$l" | tr '\n' ' ')"; done
timeout 30 df -P -B1 -l | awk '$1 ~ /^\/dev\// && $1 !~ /^\/dev\/loop/ {m=$6; for (i=7; i<=NF; i++) m=m" "$i; print "D", $3, m}'
echo E
```

On AWS the fourth line is:

```sh
for l in /dev/disk/by-id/nvme-Amazon_Elastic_Block_Store_vol*; do case $l in *-part*) continue;; esac; [ -e "$l" ] || continue; v=${l##*_vol}; echo "V vol-${v%%[-_]*} $(lsblk -nro MOUNTPOINT "$l" | tr '\n' ' ')"; done
```

Apart from this step, the script only reads, and it writes only the report below.

### What it sends

Nothing. There is no upload and no telemetry, and you send the report yourself. It switches off the Azure CLI's own usage telemetry for its calls, without changing your az settings. Its only calls go to:

- your Azure and AWS APIs;
- the public Azure price list, which receives a region name for each lookup;
- the AWS Price List API, through `aws`, which receives a region code for each lookup.

### What it writes

One PDF report, `zoorik-amoeba-assessment-YYYYMMDD-HHMM.pdf` (UTC).

- It goes in the current directory, or in your home folder if the current directory can't be written.
- It never overwrites an earlier report: a second run in the same minute adds `-2`, `-3` and so on.
- It shows what you can save across your accounts, then by account and VM, and each VM with data disks that is not in the saving yet, with the reason.
- It names your subscriptions, AWS account ids, regions and VMs, and in some reasons a mount point.
- When no VM has data disks, it writes no report.

On Windows, `az` is a batch file, and Windows' `cmd.exe` would change some of its arguments (the Resource Graph query, URLs, the command above). Those arguments go through a temporary file that `az` reads, and the script deletes it right after the call, or as it stops if you press Ctrl-C.

### Access it needs

**Azure:**
- Reader on the subscriptions.
- For the Run Command step, `Microsoft.Compute/virtualMachines/runCommand/action`. Virtual Machine Contributor has it. A VM where this permission is missing shows no saving, with the reason.

**AWS:**
- `ec2:DescribeRegions`, `ec2:DescribeInstances`, `ec2:DescribeVolumes` and `ec2:DescribeInstanceTypes`;
- `ssm:DescribeInstanceInformation`;
- `cloudwatch:GetMetricStatistics`;
- `pricing:GetProducts`;
- for the Run Command step, `ssm:SendCommand` (on the instances and the `AWS-RunShellScript` document) and `ssm:GetCommandInvocation`. An instance needs the Systems Manager agent, registered with Systems Manager. An instance where either is missing shows no saving, with the reason.

### Options

None.

### Download the report and send it

When the script finishes, it prints the report's full path.

- **Azure Cloud Shell:** on the toolbar, choose **Manage files > Download**, then enter that path.
- **AWS CloudShell:** choose **Actions > Download file**, then enter that path. This works only from the full CloudShell page. If **Actions** is missing, open CloudShell in a new browser tab.

Then e-mail the report to **founders@zoorik.com**.

### How the estimate works

The script estimates a saving for each Linux VM whose disk usage was read.

1. **Used space** is the space used on the data-disk mounts the pool takes. A shared disk (an Azure disk attached to several VMs, or an AWS Multi-Attach volume) is never pooled; it stays as it is.
2. **Pool size:**
   - **Azure:** used + 20% of used (at least 50 GB), rounded up. Each mount is first rounded up to a whole GB.
   - **AWS,** as Amoeba sizes a new pool: used × 1.03 (btrfs's own space), held at most 90% full and with at least 10 GB free, rounded up to whole GB. One reading has no growth or burst to add.
3. **Disk type:**
   - **Azure:** the pool goes on Premium SSD v2 where the VM's region and zone offer it, as Azure lists it for your subscription. A VM without a zone also needs a region where Microsoft supports nonzonal Premium SSD v2. Otherwise the pool goes on Premium SSD. Pool disks are locally redundant (LRS).
   - **AWS:** the pool goes on gp3, the volume type Amoeba creates.
4. **Layout, as Amoeba plans it:**
   - **Azure:**
     - The pool has 3 to 6 equal elastic disks. Pools of 512 GB and over also have 2 anchor disks.
     - On a VM with few free data-disk slots, the pool has 1 anchor and up to 3 elastics.
     - The pool is built beside today's disks, so it must fit the free slots, with one slot kept spare.
     - A Premium SSD v2 pool adds one 8 GB disk.
     - In a Premium SSD pool, each disk is a Premium SSD size, and elastics are at least 128 GB.
   - **AWS:**
     - The pool is 2 or more equal gp3 volumes, built beside today's volumes in the free volume slots, with one slot kept spare.
     - It has as few volumes as the slots allow, each no larger than the instance can empty in 2 hours when it is 80% full, reading and writing that data at 30% of the instance's EBS throughput, unless the slots need larger ones.
     - An instance's volume slots are its type's EBS attachment limit, less the root volume and, where the limit is shared, its network interfaces and NVMe instance-store disks, within the device names `/dev/sdf` to `/dev/sdz`.
5. **Performance:**
   - The measured peak is the highest hourly figure of the last 14 days. Each hour's figure is its busiest minute (the Maximum), with reads and writes added and summed over the disks the pool replaces.
   - Each Premium SSD v2 disk and each gp3 volume includes 3,000 IOPS and 125 MB/s.
   - **Azure:** more is priced in only when the measured peak × 1.3, shared across the pool's disks, needs it. Even then it is never more than the VM itself can drive. A Premium SSD pool has the fixed IOPS and MB/s of its disk sizes, without bursting. If those together can't carry the measured peak × 1.3 (or what the VM can drive, if less), the next rule applies.
   - **AWS,** as Amoeba sets its volumes: each gp3 volume gets the measured peak × 1.3 shared across the pool's volumes, and at least the write peak × 1.3. It stays within what a gp3 volume of its size takes (500 IOPS per GB, up to 80,000; 0.25 MB/s per IOPS, up to 2,000) and within its share of what the instance can drive. Amoeba itself plans from its own readings inside the instance; the estimate uses CloudWatch's instead. If the volumes can't carry that, the next rule applies.
   - **Where the pool can't carry the load,** the mount with the highest peak IOPS (then MB/s) stays on its disks, as Amoeba leaves it, and the pool is sized again for the other mounts. Its disks count on neither side of the saving. If no mount is left, the VM is not estimated.
   - Without metrics for the disks the pool replaces, the VM is not estimated.
6. **Saving per month** = today's list price of the disks the pool replaces − the pool's list price. Where the pool costs no less, the VM is not in the saving.
   - The report's disk bill covers every data disk of the VMs in the saving. A disk that stays as it is costs the same today and after; a shared disk belongs to more than one VM and is left out.

**Prices:**
- **Azure:** the public Azure Retail Prices API, using pay-as-you-go USD list prices for each disk's region, read at run time.
  - **Premium SSD, Standard SSD and Standard HDD:** priced by billed tier. A Premium SSD set to a higher performance tier is priced at that tier.
  - **Premium SSD v2 and Ultra Disk:** priced by provisioned GB, IOPS and MB/s, over a 730-hour month.
- **AWS:** the AWS Price List API, using on-demand USD list prices for each volume's region, read at run time. EBS bills by the month:
  - **gp3:** its GB, the IOPS above 3,000 and the MB/s above 125;
  - **io1 and io2:** their GB and every provisioned IOPS (io2 in bands);
  - **gp2, st1 and sc1:** their GB;
  - **magnetic (standard):** not priced, because it bills each I/O request.

**Not included:**
- discounts, reservations, savings plans and credits;
- transaction and I/O request charges, bursting and snapshots;
- OS disks and root volumes; instance store;
- unmanaged (VHD) disks;
- scale-set (uniform) instances;
- unattached disks;
- subscriptions in other directories, and AWS regions not enabled for the account.

Data disks without a mounted file system stay as they are.

**Not in the saving yet.** A VM with data disks is in the saving when its disk usage was read and the pool costs less than the disks it replaces. The report lists every other one under Not in the saving yet, with the reason, such as:
- Windows; a Linux kernel without btrfs; or no mounted data disk other than shared disks;
- on Azure, a VM in a resource group another Azure service manages, such as Azure Databricks or an AKS node resource group, or a VM size with fewer than 8 data-disk slots;
- on AWS, a Xen instance type, because Amoeba needs a Nitro type, where each EBS volume is an NVMe device named by its volume id;
- disk usage not read, for example because you answered no, the VM is not running, or Systems Manager doesn't manage the instance;
- not enough free data-disk slots to build the pool beside today's disks;
- no metrics, or no list price, for the disks the pool replaces;
- a pool that would cost no less.

btrfs is read from each VM's running kernel, not from its distribution. The stock kernels of Ubuntu, Amazon Linux 2023, Fedora and Oracle Linux's UEK carry it; those of RHEL, Rocky Linux and AlmaLinux 9, and Oracle Linux's Red Hat compatible kernel, do not.

A VM that needs a fix first, such as a kernel below 5.14 or Azure Disk Encryption, stays in the saving.

**Units:** GB means GiB (2^30 bytes), the unit both clouds size disks in. On Azure, MB/s means 10^6 bytes per second. On AWS, MB/s means MiB/s (2^20 bytes per second), the unit gp3 throughput is set in.

---

## Where the figures come from

Every figure in either report is one of these:
- read from your cloud or your clusters;
- read from the public price lists;
- computed from those by the method above.

When a figure can't be known, the report gives the reason instead of a number: a node pool with no list price shows Not priced and why, and a cluster or VM with no estimate is listed under Not in the saving yet, with the reason. Nothing is guessed or filled in.

The figures are estimates at list prices, not a quote.

Questions: founders@zoorik.com.

---

## Contributing

Contributions are welcome. [CONTRIBUTING.md](https://github.com/getzoorik/assessment/blob/main/CONTRIBUTING.md) explains how to run the scripts from a clone, how to add a check, a cloud or a region, and how to test a change. Issues labelled [good first issue](https://github.com/getzoorik/assessment/labels/good%20first%20issue) are a good place to start.

This repo is public, so please don't paste a report or your account's names into an issue. To report a security problem, use **Report a vulnerability** on the Security tab, or e-mail founders@zoorik.com.

## License

Apache-2.0, see [LICENSE](https://github.com/getzoorik/assessment/blob/main/LICENSE). Copyright 2026 Zoorik Private Limited.
