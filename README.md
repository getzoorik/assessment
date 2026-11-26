<p align="center">
  <img alt="Zoorik. Reduce cloud cost, autonomously." src=".github/brand/zoorik-reveal-tagline-banner-on-navy-960x240.gif" width="100%">
</p>

# Zoorik cloud cost assessment

Zoorik's free cloud cost assessment. One command reads your AKS and EKS clusters and the data disks of your VMs on Azure and AWS, and estimates how much you could reduce your cloud cost. It sends nothing anywhere: it writes one PDF report for you to email to founders@zoorik.com.

```bash
npx zoorik assess
```

Or with pip: `pip install zoorik`, then `zoorik assess`. Either way it needs Python 3.8 or later. It runs in your terminal on Linux, macOS or Windows (PowerShell or Command Prompt), or in Azure Cloud Shell or AWS CloudShell, which already have everything it calls.

- [Where to run it](#where-to-run-it)
- [What it reads](#what-it-reads)
- [What it changes](#what-it-changes)
- [What it sends](#what-it-sends)
- [What it writes](#what-it-writes)
- [Options](#options)
- [Access it needs](#access-it-needs)
- [How the estimate works](#how-the-estimate-works)
- [What is not in the saving](#what-is-not-in-the-saving)
- [Where the figures come from](#where-the-figures-come-from)
- [Next steps](#next-steps)

The assessment is the `zoorik` Python package in this repo, and it uses only the standard library. The npm package installs this same package from PyPI and runs it. Every release of both is built from a release tag of this repo by its [release workflow](https://github.com/getzoorik/assessment/blob/main/.github/workflows/release.yaml), so the code you read is the code that runs.

---

## Where to run it

- **Azure Cloud Shell (Bash) or AWS CloudShell:** they have Python, the cloud's CLI, signed in, and `kubectl`.
- **Any other terminal:** it needs Python 3.8 or later, the `az` or `aws` CLI, signed in, and `kubectl` for the clusters. An AKS cluster that signs in with Entra ID also needs `kubelogin`; `az aks install-cli` installs both.

It reads every cloud whose CLI is signed in:
- **Azure:** every enabled subscription in the Azure directory (tenant) `az` is signed in to. The report names the subscriptions of other directories. To include one, run `az login --tenant <tenant-id>`, then run the assessment again.
- **AWS:** every enabled region of the account `aws` is signed in to.

A cloud whose CLI isn't installed is skipped. A CLI that is installed but can't read, for example because it isn't signed in, is named with the reason. With neither CLI installed, the run says so and stops.

A run takes a little over 5 minutes, because it samples each cluster's usage for 5 minutes. Many or large clusters take longer, and so does the read inside VMs if you answer y to it.

Both Cloud Shells close a session after about 20 minutes without a key press, even while the run goes on. In a Cloud Shell, the run reminds you at the start: press Enter now and then until the report's path appears, and if the run asks about disks first, answer it y or N, since Enter alone answers N.

## What it reads

### Clusters

Every AKS cluster in the subscriptions it reads, and every EKS cluster in the regions it reads. How a cluster is read is decided from its settings:

- **AKS,** from `az aks list` in each subscription:
  - a cluster whose API server is private, or limited to authorized IP ranges, is read through `az aks command invoke` (see [What it changes](#what-it-changes));
  - every other cluster is read with kubectl, through the credentials `az aks get-credentials` writes to a temporary kubeconfig. For a cluster that signs in with Entra ID, `kubelogin convert-kubeconfig -l azurecli` converts that kubeconfig first;
  - a stopped cluster is named as not assessed.
- **EKS,** from `aws eks list-clusters` in each region:
  - an active cluster is read with kubectl, through the kubeconfig `aws eks update-kubeconfig` writes to a temporary file;
  - a cluster in any other state, or whose node groups can't be read, is named as not assessed;
  - a cluster whose API server doesn't answer is named as not assessed. When its endpoint is private only, the reason says to run the assessment from inside its VPC.

In each cluster it lists, 500 at a time: nodes, pods, ReplicaSets, Jobs, PersistentVolumeClaims, PersistentVolumes, CSINodes, LimitRanges, HorizontalPodAutoscalers and PodDisruptionBudgets. It keeps only these fields:
- **nodes:** name, labels, provider id, taints, capacity, allocatable and kubelet version;
- **pods:** name, namespace, controller, phase, node, overhead, node selector, required node affinity, required pod affinity and anti-affinity, tolerations, topology spread, volume claims, the pod's requests and limits, and each container's name, requests, limits and host ports (and an init container's restart policy). Of the labels, only the keys that a required placement rule of any pod or a PodDisruptionBudget's selector uses, and `addonmanager.kubernetes.io/mode`. Pods that have ended (Succeeded or Failed) are left out;
- **ReplicaSets and Jobs:** name, namespace and controller;
- **volume claims and volumes:** names, the claim's namespace and volume, the volume's node affinity, CSI driver and disk type;
- **CSINodes:** each driver's attach limit;
- **LimitRanges:** namespace, type, min, max and maxLimitRequestRatio;
- **HorizontalPodAutoscalers:** namespace, and the kind and name of the object each one scales;
- **PodDisruptionBudgets:** namespace, selector and minAvailable.

A cluster whose HorizontalPodAutoscalers or PodDisruptionBudgets can't be listed is still read: it gets no step 5, and the report says why.

It never reads Secrets or ConfigMaps, and it never keeps env values, images, commands, annotations or pod IPs.

**Usage,** with `kubectl top pods -A --containers`: one sample at the start, then one every 30 seconds for the sample window (5 minutes, or `--sample-minutes`). It keeps each container's peak.

**Self-managed EKS nodes:** `aws ec2 describe-instances` gives each node's Auto Scaling group and whether it runs on spot.

**Machine types,** for each region with a node pool whose type may change (step 3): for AKS, the VM sizes Azure Compute offers your subscription there (vCPUs, memory, CPU architecture, GPUs, premium disk and Ultra Disk support, data-disk slots and zones); for EKS, `aws ec2 describe-instance-types` (vCPUs, memory, architectures, GPUs, network interfaces and EBS limits) and `aws ec2 describe-instance-type-offerings` (the zones that offer each type).

**Machine prices,** for today's machines and the types step 3 compares, at the regular price and at spot: for AKS, the public Azure Retail Prices API (prices.azure.com); for EKS, the AWS Price List API and the region's EC2 spot price history, through `aws`.

### Disks

The data disks of every VM, except the VMs that are AKS or EKS nodes: the Kubernetes part counts those, so each cost is counted once.

**Azure:**
- **Azure Resource Graph:** your VMs and managed disks, and which resource groups another Azure service manages. The VMs in a resource group that an AKS cluster manages are its nodes.
- **Azure Compute:**
  - each VM size's data-disk slots;
  - each VM size's uncached disk IOPS and throughput limits;
  - which zones offer Premium SSD v2 to your subscription.
- **Azure Monitor:** 14 days of IOPS and throughput for the data disks the pool would replace, on each VM whose disk usage was read, as each hour's busiest minute. The metrics are Composite Disk Read/Write Operations/sec and Bytes/sec, with the Maximum aggregation.
- **prices.azure.com:** the public list prices of managed disks.

**AWS, in each enabled region:**
- **EC2:** your instances and EBS volumes, and the EBS limits of each instance type with data volumes (volume slots, baseline IOPS and throughput, whether its volumes are NVMe devices). An instance tagged `eks:cluster-name`, `eks:eks-cluster-name` or `aws:eks:cluster-name`, or tagged `kubernetes.io/cluster/<name>` for an EKS cluster of its region, is an EKS node.
- **Systems Manager:** which instances its agent manages.
- **CloudWatch:** 14 days of IOPS and throughput for the data volumes the pool would replace, on each instance whose disk usage was read, as each hour's busiest minute. The metrics are VolumeReadOps, VolumeWriteOps, VolumeReadBytes and VolumeWriteBytes, with the Maximum statistic over AWS's 1-minute data points. These requests count toward CloudWatch's free tier of 1 million API requests a month.
- **The AWS Price List API:** EBS list prices, through `aws pricing get-products`.

## What it changes

Apart from the two steps below, the assessment only reads, and it writes only the files under [What it writes](#what-it-writes).

### One short-lived pod, only in AKS clusters read through command invoke

An AKS cluster whose API server is private, or limited to authorized IP ranges, is read through `az aks command invoke`, one call per cluster. Azure starts a short-lived pod of its own in the cluster's `aks-command` namespace. The reads and the usage samples run in that pod, and the pod sends back only the fields above and each container's peak, compressed.

- Azure documents this pod as requesting 200m CPU and 500 MiB of memory. The report leaves it out.
- Azure records each call in the activity log.
- Azure keeps at most 512 KB of each call's output. A cluster whose answer is larger is named as not assessed, with the reason.
- A cluster with command invoke turned off is named as not assessed.

`--no-invoke` turns this off: those clusters are named as not assessed, with the reason.

### One command inside each VM, only if you answer y

The disk saving needs the used space on each data disk, and only the VM itself can report that.

The run first finds the running Linux VMs that have data disks and pass its first checks. If there are any, it lists them, prints the command below, says how long it will take, and asks once for each cloud:

```
Read disk usage inside 3 VMs with Azure Run Command? [y/N]
Read disk usage inside 2 instances with Systems Manager Run Command? [y/N]
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

### Stopping a run

**Ctrl-C** stops the run at once, and nothing new starts after it. A command invoke or Run Command call already started finishes in the cloud. The run's last line says in how many clusters and VMs they had started, and where they show (the activity log, or the Run Command history).

## What it sends

Nothing. There is no upload and no telemetry, and you send the report yourself. It switches off the Azure CLI's own usage telemetry for its calls, without changing your az settings. Its only calls go to:

- your clusters;
- your Azure and AWS APIs, through `az` and `aws`;
- the public Azure price list, which receives a region and the machine types or disk products it looks up;
- the AWS Price List API, through `aws`, which receives a region code for each lookup.

## What it writes

**One PDF report,** `zoorik-assessment-YYYYMMDD-HHMM.pdf` (UTC).

- It goes in the current folder, or in the folder `--out` names, made if it is missing. The run checks that it can write there before it reads anything.
- It never overwrites an earlier report: a second report in the same minute gets `-2`, `-3` and so on.
- Page 1 shows the monthly cost today of everything the run read, the cost after, the saving and its share of the cost today, and where the saving comes from, with the assumption beside each figure that rests on one. The Kubernetes pages show each cluster's steps, node pools, workloads and the apps that run fewer copies at quiet times; the Disks pages show each account's VMs. Then Not in the saving yet lists everything the run couldn't read or count, with the reason.
- It names your subscriptions, AWS account ids, clusters, node pools, namespaces, workloads and VMs, and in some reasons a mount point. `--anonymize` hides them.
- When the run finds no cluster machine or VM data disk with a list price, it writes no report, and it prints why.

**Working files:** the temporary kubeconfigs, kubectl's cache, and on Windows any `az` argument that `cmd.exe` would change (`az` is a batch file there). They go in a temporary folder that is removed when the run ends, also when you stop it. Your own kubeconfig is left as it is.

## Options

```bash
npx zoorik assess --anonymize
```

| Option | Effect |
|---|---|
| `--out PATH` | Write the report in this folder, made if it is missing (default: the current folder). |
| `--sample-minutes N` | Sample the clusters' usage for N minutes (default 5; 0 takes one sample). |
| `--no-invoke` | Never use `az aks command invoke`. The AKS clusters only it can read are named as not assessed. |
| `--anonymize` | Hide every name the report shows (subscriptions, accounts, clusters, node pools, namespaces, workloads, VMs and mount points) behind short hashes, salted for this run. Machine types, disk types and regions stay. The terminal still shows the real names. |

`zoorik assess --help` lists them, with the Kubernetes steps and the assumptions of steps 5 and 6, and `zoorik --version` prints the version.

## Access it needs

When a cluster, a subscription, a region or a VM can't be read, the run goes on, and the report lists it under Not in the saving yet, with the reason.

**Azure:**
- Reader on the subscriptions.
- The *Azure Kubernetes Service Cluster User Role* on each cluster read with kubectl.
- For `command invoke`, also `Microsoft.ContainerService/managedClusters/runcommand/action` and `Microsoft.ContainerService/managedClusters/commandResults/read`.
- For the read inside VMs, `Microsoft.Compute/virtualMachines/runCommand/action`. Virtual Machine Contributor has it.

**AWS:**
- `ec2:DescribeRegions`;
- for the clusters, `eks:ListClusters`, `eks:DescribeCluster`, `eks:ListNodegroups`, `eks:DescribeNodegroup`, `ec2:DescribeInstances`, `ec2:DescribeInstanceTypes`, `ec2:DescribeInstanceTypeOfferings` and `ec2:DescribeSpotPriceHistory`;
- for the disks, `ec2:DescribeInstances`, `ec2:DescribeVolumes`, `ec2:DescribeInstanceTypes`, `ssm:DescribeInstanceInformation` and `cloudwatch:GetMetricStatistics`;
- `pricing:GetProducts`;
- for the read inside instances, `ssm:SendCommand` (on the instances and the `AWS-RunShellScript` document) and `ssm:GetCommandInvocation`. An instance also needs the Systems Manager agent, registered with Systems Manager;
- an EKS access entry for the identity on each cluster.

The identity that created an EKS cluster usually has an access entry already. For any other identity, choose one of these:
- Associate `AmazonEKSAdminViewPolicy` at cluster scope. This policy can also read Secrets, but the run never asks for them.
- Put a Kubernetes group in the access entry, and bind that group to the role below, which allows only `get` and `list`.

`AmazonEKSViewPolicy` is not enough: it can't list nodes or read metrics.

**Inside each cluster:** the identity needs `get` and `list`, cluster-wide, on what the run reads. If it doesn't have them, a cluster admin can create this role and bind it. Replace `GROUP` with the Kubernetes group from the EKS access entry, or with the Entra ID group's object ID on AKS:

```bash
kubectl create clusterrole zoorik-assess-read --verb=get,list --resource=nodes,pods,replicasets.apps,jobs.batch,persistentvolumeclaims,persistentvolumes,csinodes.storage.k8s.io,limitranges,horizontalpodautoscalers.autoscaling,poddisruptionbudgets.policy,pods.metrics.k8s.io
kubectl create clusterrolebinding zoorik-assess-read --clusterrole=zoorik-assess-read --group=GROUP
```

## How the estimate works

### Kubernetes

Each cluster's saving is worked out in steps. Each step works on what the step before left.

**Machines and pools.** Each node belongs to the pool that provisions it: its Karpenter NodePool, its AKS node pool, its EKS managed node group, or else its Auto Scaling group. Its machine type, zone, CPU architecture and operating system come from its labels, and whether it runs on spot from its pool or, for a node of an Auto Scaling group, from its EC2 instance. Fargate and virtual nodes are not machines: they are counted, and their pods are left out.

**Pods that stay where they are.** DaemonSet and static pods count as each machine's overhead. These pods keep their machines:
- a pod with no controller, since nothing would recreate it;
- a pod whose volume is bound to its node;
- a pod whose volume claim is not bound to a volume the run read;
- a pod whose required affinity selects namespaces by label.

**Step 1, smaller pods.**
- Each container's request becomes its peak use × 1.3, at least 10m CPU and 32 MiB of memory.
- One rule both ways: a container that uses more than it asks for is raised, so the machines hold what the pods really use.
- Every replica of a workload gets one request per container, which holds its busiest replica.
- A raise has to fit the machines the pods run on today. Where the raised requests on a machine, with its DaemonSets', come to more than its allocatable, the workloads raised there keep today's requests. The workload table shows them as no room, and Not in the saving yet counts them.
- A request stays within the container's limit and its namespace's Container LimitRanges (min, max and maxLimitRequestRatio). A pod whose new total would break a Pod LimitRange keeps today's requests.
- Pod-level requests follow the pod's summed peaks, within its pod-level limits.
- Sidecars (init containers with `restartPolicy: Always`) are sized like the other containers. Other init containers keep their requests.
- A container with no usage reading keeps its request, and so do static pods and pods the platform puts back as they were (`addonmanager.kubernetes.io/mode: Reconcile`).
- The machines don't change in this step, so it saves nothing on its own: its saving shows in step 2.

**Step 2, spare machines.** Each pool's pods are packed onto as few of today's machines as hold them, keeping the machines that give the most room for their price.
- A pod goes only where Kubernetes would let it run: every resource it requests, its CSI drivers' attach limits, its node selector and required node affinity, taints with the NoSchedule and NoExecute effects against its tolerations, topology spread with DoNotSchedule, required pod affinity and anti-affinity in both directions, host ports, the machine's operating system, CPU architecture and type labels, and its volumes' node affinity.
- A pool with a machine that has no list price takes no part.
- A packing that needs as many machines as today is not applied.
- An AKS system pool keeps at least 1 machine. Any other pool may reach 0. The autoscaler's minimum is not kept.

**Step 3, cheaper machine types.** Each pool moves to the cheapest machine type, or mix of two types, that the search below finds, when that costs less than the pool after step 2. The packing follows step 2's rules, on new machines of each type in the pool's zones.
- A candidate type has the pool's CPU architecture, doesn't burst (Azure's B-series, AWS's burstable types), has no GPU, and is offered to your subscription or account in every zone the pool uses. On AKS it has at least 2 vCPUs and 4 GiB, below which AKS refuses a node size. Where the pool's pods use Azure disks of a premium tier (Premium SSD, Premium SSD v2 or Ultra Disk, or an in-tree `azureDisk` volume, which doesn't name its tier), it takes premium disks, and where they use Ultra Disk, it offers Ultra Disk in those zones.
- These pools keep their machine types: a pool with GPUs; a pool whose machines are bought both regular and spot, or one with a machine that has no list price; and a pool with a pod that selects its machine type (`node.kubernetes.io/instance-type`, or another label that comes from the type) or requests a resource other than CPU, memory and ephemeral storage, such as a GPU or hugepages.
- What a new machine would give pods comes from your nodes. CPU is the type's vCPUs. Memory is its listed memory × the smallest share of their listed memory your nodes report. Pods and ephemeral storage are the pool's, and the attach limit follows the CSI driver's rule for the type. From these it takes the platform's reservation for the pool's Kubernetes version and max pods (AKS's, or the EKS node images'), and the eviction threshold. The run first checks these rules on each node of the pools that may change: allocatable CPU and memory within 1%, pods and attach limits exactly. When a node misses, the cluster gets no step 3, and the report says why.
- The search: each candidate gets a lower bound, its price × the machines the pool's pods need on their scarcest resource. Single types are packed in lower-bound order until the next lower bound reaches the cheapest cost found. A type is skipped when one tried before it, whose machines would join the same pool, covers it: the same CPU architecture, at least as much of every resource and disk slot, and no higher price. So types that give the same room at the same price count once. Then, from the cheapest single type, for each k the pods of its k least-full machines move to new machines of a type it doesn't cover, packed in the same order and with the same stop, while its other machines keep their pods. A pool's search runs at most 20 packings, the mixes after the single types. The pool takes the cheapest of these. The search doesn't try every mix, so a cheaper one may exist.
- An AKS node pool or an EKS managed node group runs one machine type, so a mix's second type becomes a new pool beside it: the pool's labels, except its name, and its taints, in User mode. A pod that selects the pool by its name stays on the first type. A Karpenter NodePool or an Auto Scaling group holds both types.
- A pool on spot today is compared at spot prices.
- The run doesn't read your vCPU quotas: a cheaper type may need a quota increase first.

**Step 4, packing across pools.** A pod may move to any pool where Kubernetes would let it run, by step 2's rules. The cluster's pods are packed onto as few machines as hold them, the machines that give the most room for their price first.
- Each pool keeps the machine types step 3 left it. A pool can take new machines of each of its types in each zone where it has that type. A new machine has the labels and taints that the pool's machines of that type in that zone all share.
- A pool on spot today takes pods only into room on its machines, so it gains no machine.
- A pool with a machine that has no list price takes no part: its pods stay, and it takes none.
- A pod that fits no empty machine stays on its machine.
- An AKS system pool keeps at least 1 machine. Any other pool may reach 0.
- A packing that doesn't make the cluster cost less is not applied.

**Step 5, fewer copies at quiet times.** Apps with copies to spare run fewer of them for half of the hours, 12 a day, and today's count for the other half.
- An app is a Deployment or an Argo Rollout, and its copies are its pods the run read. StatefulSets, DaemonSets and Jobs stay as they are.
- An app with 3 or more copies and no HorizontalPodAutoscaler of its own (one whose scale target is the app, such as those KEDA creates) runs at its minimum at quiet times: 2 copies, or one more than the largest whole-number `minAvailable` of the PodDisruptionBudgets that select its pods, if that is higher. A percentage `minAvailable` and a `maxUnavailable` don't raise it. The copies kept are its first by name.
- An app whose minimum is at or above its copies stays as it is, and so does an app the platform puts back as it was (`addonmanager.kubernetes.io/mode: Reconcile`).
- At quiet times the pods are packed onto as few of step 4's machines as hold them, by step 4's rules. No pool grows, and a pool may shrink to the platform's minimum.
- The saving is half of step 4's monthly cost less half of the quiet hours' monthly cost, at regular prices.
- A cluster whose HorizontalPodAutoscalers or PodDisruptionBudgets couldn't be read gets no step 5, and the report says why.
- The report states the assumption beside the figure: each app with 3 or more copies and no autoscaler of its own runs at 2 copies (or its disruption budget's floor) for 12 hours a day.

**Step 6, spot.** The machines running at quiet times are counted on spot for those 12 hours a day, and step 4's machines at their regular price for the other 12. One formula holds for every cluster: a line's month costs 730 × (0.5 × its machines after step 4 × the regular price + 0.5 × its machines at quiet times × the spot price).
- An AKS system pool stays on regular machines, and a line on spot today stays as it is.
- A line whose machine type has no spot price keeps its regular price in both halves, and the report names it with the reason.
- The report states the assumption beside the figure: the machines running at quiet times, 12 h a day, on spot.

**Costs.**
- A line (a pool's machines of one type, regular or spot) costs its machines' hourly list price × 730 hours, rounded to whole dollars. From step 5, half of those hours are the quiet hours, with the line's machines then, which step 6 prices at spot. A line with no list price counts in no total, and the report names it with the reason.
- A step that would cost more than the state before it is not applied. So no step is negative, and the steps add up exactly to the cost today less the cost after.
- The workload table splits the saving of smaller pods and spare machines, packing included (steps 1, 2 and 4), by the CPU and memory each workload frees. A raised workload frees nothing. The shares add up to that saving exactly, and are never added on top of it.

**Prices** are USD list prices:
- **Azure:** for each machine type in its region, the cheapest Linux pay-as-you-go price above zero, and its Spot price, for a pool on Spot today and for step 6;
- **AWS:** Linux on-demand, with shared tenancy and no preinstalled software, from the Price List; for a pool on spot today and for step 6, the average over the region's zones of each zone's latest spot price.

Only Linux machines are priced. A machine whose labels don't give its type, or whose pool doesn't say whether it runs on spot, is not priced, with the reason.

**Usage:**
- Five minutes of samples can miss peaks. Use `--sample-minutes` for a longer window.
- Without metrics-server there is no usage. The cluster's pods then keep today's requests, and the report says why.

### Disks

The run estimates a saving for each Linux VM whose disk usage was read: the data disks it can move to a pool of new disks that costs less.

1. **Used space** is the space used on the data-disk mounts the pool takes. A shared disk (an Azure disk attached to several VMs, or an AWS Multi-Attach volume) is never pooled; it stays as it is.
2. **Pool size:**
   - **Azure:** used + 20% of used (at least 50 GB), rounded up. Each mount is first rounded up to a whole GB.
   - **AWS:** used × 1.03 (btrfs's own space), held at most 90% full and with at least 10 GB free, rounded up to whole GB. One reading has no growth or burst to add.
3. **Disk type:**
   - **Azure:** the pool goes on Premium SSD v2 where the VM's region and zone offer it, as Azure lists it for your subscription. A VM without a zone also needs a region where Microsoft supports nonzonal Premium SSD v2. Otherwise the pool goes on Premium SSD. Pool disks are locally redundant (LRS).
   - **AWS:** the pool goes on gp3.
4. **Layout:**
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
   - **AWS:** each gp3 volume gets the measured peak × 1.3 shared across the pool's volumes, and at least the write peak × 1.3. It stays within what a gp3 volume of its size takes (500 IOPS per GB, up to 80,000; 0.25 MB/s per IOPS, up to 2,000) and within its share of what the instance can drive. If the volumes can't carry that, the next rule applies.
   - **Where the pool can't carry the load,** the mount with the highest peak IOPS (then MB/s) stays on its disks, and the pool is sized again for the other mounts. Its disks count on neither side of the saving. If no mount is left, the VM is not estimated.
   - Without metrics for the disks the pool replaces, the VM is not estimated.
6. **Saving per month** = today's list price of the disks the pool replaces − the pool's list price. Where the pool costs no less, the VM is not in the saving.
   - The disks' cost today covers every priced data disk of the VMs read, each disk counted once. A disk that stays as it is costs the same today and after.

**Prices:**
- **Azure:** the public Azure Retail Prices API, using pay-as-you-go USD list prices for each disk's region, read at run time.
  - **Premium SSD, Standard SSD and Standard HDD:** priced by size tier. A Premium SSD set to a higher performance tier is priced at that tier.
  - **Premium SSD v2 and Ultra Disk:** priced by provisioned GB, IOPS and MB/s, over a 730-hour month.
- **AWS:** the AWS Price List API, using on-demand USD list prices for each volume's region, read at run time. EBS charges by the month:
  - **gp3:** its GB, the IOPS above 3,000 and the MB/s above 125;
  - **io1 and io2:** their GB and every provisioned IOPS (io2 in bands);
  - **gp2, st1 and sc1:** their GB;
  - **magnetic (standard):** not priced, because it is charged for each I/O request.

btrfs is read from each VM's running kernel, not from its distribution. The stock kernels of Ubuntu, Amazon Linux 2023, Fedora and Oracle Linux's UEK carry it; those of RHEL, Rocky Linux and AlmaLinux 9, and Oracle Linux's Red Hat compatible kernel, do not.

Data disks without a mounted file system stay as they are.

A VM that needs a fix first, such as a kernel below 5.14 or Azure Disk Encryption, stays in the saving.

**Units:** GB means GiB (2^30 bytes), the unit both clouds size disks in. On Azure, MB/s means 10^6 bytes per second. On AWS, MB/s means MiB/s (2^20 bytes per second), the unit gp3 throughput is set in.

## What is not in the saving

**Not counted:**
- discounts, reservations, savings plans and credits;
- Kubernetes clusters other than AKS and EKS;
- in clusters: storage, network, load balancers, control-plane fees and licences, and pods not yet placed on a machine;
- for disks: transaction and I/O request charges, bursting and snapshots; OS disks and root volumes; instance store; unmanaged (VHD) disks; scale-set (uniform) instances; unattached disks.

**Named in the report, under Not in the saving yet, with the reason:**
- a cloud whose CLI can't read, the subscriptions of other Azure directories, and each subscription or region whose clusters or VMs couldn't be listed;
- each cluster not assessed, such as a stopped cluster, one whose API server the run can't reach, or one it has no access to;
- in an assessed cluster: machines with no list price, usage that couldn't be read, pods with no controller, workloads kept at today's requests because their machines have no room to raise them, nodes that are not machines, why cheaper machine types couldn't be counted (machine types the run couldn't read, or a node the allocatable rules don't reproduce), why fewer copies at quiet times couldn't be counted (HorizontalPodAutoscalers or PodDisruptionBudgets the run couldn't read), and machines at quiet times with no spot price;
- each VM with data disks that is not in the saving, such as:
  - Windows; a Linux kernel without btrfs; or no mounted data disk other than shared disks;
  - on Azure, a VM in a resource group another Azure service manages, such as Azure Databricks, or a VM size with fewer than 8 data-disk slots;
  - on AWS, an instance type that isn't Nitro, because each volume must be an NVMe device named by its volume id;
  - disk usage not read, for example because you answered no, the VM is not running, or Systems Manager doesn't manage the instance;
  - not enough free data-disk slots to build the pool beside today's disks;
  - no metrics, or no list price, for the disks the pool replaces;
  - a pool that would cost no less.

## Where the figures come from

Every figure in the report is one of these:
- read from your clouds or your clusters;
- read from the public price lists;
- computed from those by the method above.

Steps 5 and 6 also rest on an assumption each, which the report states beside their figures: how many hours a day apps run fewer copies, and that the machines of those hours run spot.

When a figure can't be known, the report gives the reason instead of a number: a node pool with no list price shows Not priced and why, and a cluster or VM with no estimate is listed under Not in the saving yet, with the reason. Nothing is guessed or filled in.

The figures are estimates at public list prices, not a quote.

## Next steps

When the run ends, it prints the cost today, the saving and the report's full path.

- **Azure Cloud Shell:** on the toolbar, choose **Manage files > Download**, then enter that path.
- **AWS CloudShell:** choose **Actions > Download file**, then enter that path. This works only from the full CloudShell page. If **Actions** is missing, open CloudShell in a new browser tab.

Then:
1. Email the report to **founders@zoorik.com**.
2. Book 30 minutes to go through it: [cal.com/zoorik/talk](https://cal.com/zoorik/talk).

Coral rebalances pods and machines. Amoeba rightsizes disks.

---

## Contributing

Contributions are welcome. [CONTRIBUTING.md](https://github.com/getzoorik/assessment/blob/main/CONTRIBUTING.md) explains how the package is laid out, how to run the checks and replay tests, and how to add a step or a cloud. Issues labelled [good first issue](https://github.com/getzoorik/assessment/labels/good%20first%20issue) are a good place to start.

This repo is public, so please don't paste a report or your account's names into an issue. To report a security problem, use **Report a vulnerability** on the Security tab, or email founders@zoorik.com.

## License

Apache-2.0, see [LICENSE](https://github.com/getzoorik/assessment/blob/main/LICENSE). Copyright 2026 Zoorik Private Limited.

---

<p align="center"><sub><a href="https://zoorik.com">zoorik.com</a> · <a href="https://zoorik.com/docs">Docs</a> · <a href="https://zoorik.com/security">Security</a> · <a href="https://github.com/getzoorik">Zoorik on GitHub</a></sub></p>
<p align="center"><sub>The Zoorik, Coral and Amoeba names and logos belong to Zoorik Private Limited.</sub></p>
