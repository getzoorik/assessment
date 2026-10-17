# Contributing

Thanks for helping. This repo holds two scripts:

- `k8s.py` assesses Kubernetes clusters (AKS, EKS, or any cluster kubectl can reach);
- `disks.py` assesses the data disks of Linux VMs on Azure and AWS.

Each one reads a cloud account, estimates a saving and writes one Excel file. The [README](README.md) says exactly what each script reads, runs and writes. Read it first: it is our promise to the people who run them.

## Before you start

- **What is on `main` runs for everyone.** `zoorik.com/assess/k8s` and `zoorik.com/assess/disks` redirect to the files on `main`, so a merged change reaches the next person who pastes the line. The `zoorik` package on PyPI gets it at the next release.
- **Open an issue first** for anything bigger than a small fix, so we can agree on the approach before you write it.
- **New here?** Issues labelled [good first issue](https://github.com/getzoorik/assessment/labels/good%20first%20issue) are small and say where to start.

## The rules the scripts follow

- **One file, standard library only.** Each script is a single Python file that runs as `curl ... | python3 -`. No third-party packages, and a script never imports another file of this repo. It must run on Python 3.8 and later. The `zoorik` package ships the two files unchanged.
- **Read only.** Apart from the steps the README describes (`az aks command invoke` in k8s.py, and Azure Run Command or AWS Systems Manager Run Command after a yes in disks.py), the scripts only read. Don't add a call that changes anything in a cloud or a cluster.
- **Send nothing.** No telemetry and no uploads. The only network calls go to the user's cloud, their clusters and the clouds' price lists.
- **Keep only what the report needs.** k8s.py never reads Secrets or ConfigMaps, and never keeps env values, annotations or pod IPs. When you read something new, keep only the fields you use.
- **Never guess a figure.** When a value can't be known, leave its cell blank and say why in the Status, Reason, Note or Price note column.
- **Run everywhere.** Both scripts run on Linux, macOS and Windows. Call a CLI through `run` (k8s.py) or `cli` (disks.py), which find `az.cmd` on Windows and pass any argument `cmd.exe` would change through a file. Don't use `/dev/tty`, POSIX-only signals or shell syntax outside those.
- **Keep the docs in step.** If you change what a script reads, runs, writes or how it estimates, update the README and the sheet that explains the method (Assumptions in k8s.py, About in disks.py) in the same pull request.
- **Bump `VERSION`** near the top of the script you change, so a report shows which code made it.

## What you need

| Script | Needs |
|---|---|
| `k8s.py` | `python3` and `kubectl`. For AKS, the `az` CLI, plus `kubelogin` for clusters that sign in with Entra ID. For EKS, the `aws` CLI. For usage figures, metrics-server in the cluster. |
| `disks.py` | `python3`, and a signed-in `az` CLI with Reader on a subscription or a signed-in `aws` CLI with the access the README lists. |

Both run on Linux, macOS and Windows. Azure Cloud Shell and AWS CloudShell already have all of these.

## Run a script from your clone

```bash
python3 k8s.py --help
python3 k8s.py --context kind-kind --no-prices --sample-minutes 0
python3 disks.py
```

- **k8s.py:** a local cluster (kind, minikube or k3d) is enough to try most changes. Install metrics-server to get usage; on kind it needs the `--kubelet-insecure-tls` flag. `--no-prices` skips the price lookups and `--sample-minutes 0` takes one sample, so a run takes seconds. Name the cluster with `--context`: with no options, the script assesses the AKS clusters in your current Azure subscription and the EKS clusters in your AWS account, wherever `az` or `aws` is signed in.
- **disks.py:** you need an Azure subscription or an AWS account. A small Linux VM with one data disk is enough; on AWS, make it a Nitro instance type that Systems Manager manages, so the in-VM step can reach it. It assesses each cloud whose CLI is signed in. It asks before it runs anything inside a VM; answer N to keep the run read-only. Apart from `--help` and `--version`, it has no options yet.
- **Windows:** run the scripts from PowerShell or Command Prompt (`py k8s.py`, `py disks.py`). The disk assessment asks its y/N question on the console; in Git Bash, which has none, it skips the in-VM step and says so.

## How the code is laid out

Both files are split into sections, each headed by a `# ----` line with its name.

**k8s.py**
- *commands:* `run`, `az` and `aws` wrap the CLI calls. An expected failure raises `Failed` with a short reason, and `_REASONS` turns tool errors into those reasons.
- *kubectl* and *command invoke:* `fetch_direct` reads the kinds in `RESOURCES` with kubectl. `fetch_invoke` reads them through `az aks command invoke` when kubectl can't read an AKS cluster.
- *discovery:* `choose_targets`, `discover_azure`, `discover_aws` and `kube_contexts` decide which clusters to read, one `Target` each.
- *collectors:* `collect` reads one cluster, `build` turns what it read into `Node`, `Pod` and `Workload`, and `sample_all` takes the `kubectl top` samples.
- *pricing:* `Pricer.hourly`, `azure_price` and `aws_price`.
- *estimate:* `estimate`, `keep_minimums` and `money`.
- *report rows:* each sheet is a `*_COLS` list and a `*_rows` function, and `assumptions` writes the Assumptions sheet.
- *xlsx writer:* `write_xlsx` writes the file.

**disks.py**
- *commands:* `cli` runs every `az` and `aws` call and retries when the cloud throttles; `az`, `aws`, `rest`, `http_json`, `parallel` and `ask`.
- *inventory:* the Resource Graph queries (`VM_QUERY`, `DISK_QUERY`, `MANAGED_GROUPS_QUERY`), run by `graph` and `inventory`.
- *AWS inventory:* `aws_signed_in`, `aws_region` (instances, volumes, instance types, Systems Manager) and `ebs_limits`, which counts an instance's volume slots as Amoeba does.
- *VM sizes and prices:* `region_facts`, `region_prices` and `price_disk`; on AWS, `ebs_prices` and `price_volume`.
- *measured performance:* `disk_metrics` (Azure Monitor) and `volume_metrics` (CloudWatch).
- *inside the VM:* `GUEST_SCRIPT`, `read_guest` (Azure Run Command), `read_instance` (Systems Manager Run Command) and `parse_guest`.
- *pool sizing:* `pool_members` and the functions above it for Azure, `gp3_pool` for AWS. They use the constants at the top of the file, which are the numbers Amoeba itself plans with. Please open an issue before changing one.
- *assessment:* `screen` and `assess` set each VM's lane, reasons and saving, the same for both clouds; `azure_pool` and `aws_pool` size and price the pool.
- *report:* the `*_COLS` lists, the `*_rows` functions, `summary_rows` and `about_rows`. `fit` leaves out the columns no cloud in the run fills.
- *xlsx writer:* `write_xlsx` writes the file.

**zoorik/**, the `zoorik` command for people who install the package:
- `__main__.py` starts `k8s.py` or `disks.py`. The wheel ships both files inside the package (see `force-include` in `pyproject.toml`), so the package needs no copy of them.
- `__init__.py` holds the package version.

## Add a check

- **k8s.py, a new figure or column:** work it out in `build` or `estimate`. Then add the column to the right `*_COLS` list, and the value to its `*_rows` function at the same position.
- **k8s.py, a new Kubernetes resource to read:**
  - add it to `RESOURCES` with a jq filter that keeps only the fields you use (`az aks command invoke` runs that filter inside the cluster);
  - if its objects carry data the report must not keep, such as env values, or are large, add a `slim_*` function to `SLIM`; kubectl reads use it to drop the rest;
  - add a short label to `OPTIONAL`, so the Status column says when it can't be read;
  - add it to "What it reads" and to the `kubectl create clusterrole` line in the README, because people grant exactly that role, and to "Data read" in `assumptions`.
- **disks.py, a new lane rule:** add a reason in `screen` (from what Azure reports) or in `assess` (from what the VM reports). A reason in `red` makes the VM Not supported, and one in `amber` makes it Needs work. Update the Lanes table in the README and the Lanes row in `about_rows`.
- **disks.py, a new disk type:** add it to `DISK_TYPES`, then teach `add_price` and `price_disk` its price meters (on AWS, `EBS_BILLS`).
- **Any check that needs a new CLI call or permission:** add it to "Access it needs" in the README.

## Add a cloud, a region or a price

- **Regions** come from the cloud at run time, and prices are looked up by region name, so most new regions need no code. If a report says "no list price for ... in REGION", first check that the public price list has that region.
- **k8s.py prices:** `Pricer.hourly` decides whether a node can be priced, and `azure_price` and `aws_price` fetch the price. A new source must give public list prices, read through a public URL or a CLI the script already uses.
- **k8s.py, a new cloud or platform:** `make_node` recognises nodes. It takes the cloud from `spec.providerID` or the labels, the pool from `POOL_LABELS`, and Spot or on-demand from the labels. `choose_targets` finds clusters. A platform without a discovery step can still be read with `--context`.
- **disks.py, Premium SSD v2 on VMs without a zone:** Azure's API doesn't say where this is offered, so `V2_NONZONAL_REGIONS` is copied by hand from the Microsoft Learn page "Deploy a Premium SSD v2 managed disk" (Regional availability). To add a region, link that page in your pull request and update the date in the comment above the list.
- **disks.py, disk sizes and limits:** `TIER_SIZES`, `V1_TIER_PERFORMANCE` and the `V2_*` limits come from Azure's documentation. Link the page you used. The `GP3_*` limits and the EBS slot rules are Amoeba's own, from its AWS adapter.

## Test a change

There is no test suite yet. CI runs these checks on every pull request, on Linux, macOS and Windows, with the newest Python, and on Linux with Python 3.8 too:

```bash
python3 -m py_compile k8s.py disks.py
pipx run --spec ruff==0.16.10 ruff check .
python3 k8s.py --help
python3 k8s.py --context no-such-context --no-prices --sample-minutes 0 --out /tmp/zoorik-report/
```

- The last command reaches no cluster. It shows that a run still finishes, and writes a report, when a cluster can't be assessed.
- CI also runs disks.py where neither `az` nor `aws` is installed, and checks that it stops with the message that asks for one. Where both are installed but not signed in, it checks that both scripts say to sign in, which shows they found `az.cmd` on Windows.
- CI also installs the package with `pip install .` and runs the same no-cluster report through `zoorik assess coral`, and checks that `npx zoorik` (the `npm/` folder) runs the PyPI package, on all three systems.
- `ruff.toml` sets Python 3.8 as the target, so ruff also flags syntax that is newer than 3.8. If you use uv, `uv run --python 3.8 k8s.py --help` tries Python 3.8 locally.

To try the AWS path of disks.py without an account, put a small fake `aws` script first on your PATH that prints canned JSON for the calls `disks.py` makes (each goes through `aws(...)`), and keep `az` off the PATH.

Then run your change for real, against a cluster, subscription or AWS account you own, and open the report. Excel, LibreOffice and Google Sheets all open it. In the pull request, say what you ran and what changed in the report. Replace your own names, or run k8s.py with `--anonymize`. Never commit a report file: it names things in your account.

## Code style

- Match the code around you: type hints, dataclasses, short docstrings, and `Failed("short reason")` for anything the user can fix.
- Keep the diff to your change; please don't reformat whole files. Lines go up to about 120 characters.
- Text the user sees is plain and short.

## Pull requests

1. Fork the repo and make a branch.
2. Keep each pull request to one change.
3. Say what you changed, why, and how you tested it.
4. CI must pass. A maintainer reviews every pull request and merges it.

By sending a pull request, you agree that your contribution is licensed under Apache-2.0, the license of this repo.

## Releases

Maintainers publish the `zoorik` package to PyPI:

1. Bump `__version__` in `zoorik/__init__.py`, and `version` in `npm/package.json` to the same number, in a pull request.
2. Once it is merged, publish a GitHub release whose tag is `v` plus that version, such as `v1.0.1`.
3. The release workflow checks that the tag matches the version, builds the package and installs it once. It publishes to PyPI after a maintainer approves the `pypi` environment, then the same version to npm. PyPI trusts this workflow directly (trusted publishing), so no PyPI token is stored anywhere.

## Security and conduct

- Please don't report a security problem in a public issue. Use **Report a vulnerability** on the repo's Security tab, or e-mail founders@zoorik.com.
- Everyone who takes part follows our [Code of Conduct](https://github.com/getzoorik/.github/blob/main/CODE_OF_CONDUCT.md).

Questions: founders@zoorik.com.
