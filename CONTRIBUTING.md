# Contributing

Thanks for helping. This repo holds the `zoorik` Python package. Its one command, `zoorik assess`, reads the AKS and EKS clusters and the VM data disks of every Azure and AWS account the `az` and `aws` CLIs are signed in to, estimates a saving and writes one PDF report. The [README](README.md) says exactly what it reads, changes and writes. Read it first: it is our promise to the people who run it.

## Before you start

- **What is on `main` reaches everyone at the next release.** The package on PyPI and the npm launcher are built from release tags of this repo.
- **Open an issue first** for anything bigger than a small fix, so we can agree on the approach before you write it.
- **New here?** Issues labelled [good first issue](https://github.com/getzoorik/assessment/labels/good%20first%20issue) are small and say where to start.

## The rules the code follows

- **Standard library only at runtime,** on Python 3.8 and later. The tools in `requirements-dev.txt` are for the checks only.
- **Read only.** Apart from the two steps the README describes (`az aks command invoke` in an AKS cluster whose API server is private or limited to authorized IP ranges, and Azure Run Command or Systems Manager Run Command inside VMs after a yes), the run only reads. Don't add a call that changes anything in a cloud or a cluster.
- **Send nothing.** No telemetry and no uploads. The only network calls go to the user's clouds, their clusters and the public price lists.
- **Keep only what the report needs.** `zoorik/kubectl/objects.py` is the allowlist of the fields the run reads in a cluster. Both read methods use it, and the jq filter that runs inside the cluster through command invoke is generated from it. When you read something new, add only the fields you use.
- **Never guess a figure.** When a value can't be known, don't show it as 0: leave it out and let the report give the reason, such as under Not in the saving yet.
- **One rule per decision.** No fallbacks: no "try one way, then another", no aliases for removed commands or options, no old paths kept alive. How a cluster is read is decided from its settings, before the read.
- **Run everywhere.** The package runs on Linux, macOS and Windows. Every program starts through the command runner in `zoorik/system/process.py`, which finds `az.cmd` on Windows, and `zoorik/cloud/cli.py` passes any `az` argument that `cmd.exe` would change through a file. Calls that exist on one platform only sit under `sys.platform` checks.
- **Name the two parts `kubernetes` and `disks`,** in modules, files, classes, functions and messages.
- **Keep the docs in step.** If you change what the run reads, changes or writes, or how it estimates, update the README in the same pull request. A new permission goes under "Access it needs", and a new Kubernetes kind in the `kubectl create clusterrole` line there, because people grant exactly that role.

## Set up

The pinned check tools need Python 3.11 to 3.13: mypy runs on 3.9 to 3.13, and import-linter on 3.11 or later. CI uses 3.13, whose venv also keeps itself out of git.

```bash
git clone https://github.com/getzoorik/assessment.git
cd assessment
python3.13 -m venv .venv
. .venv/bin/activate            # Windows: .venv\Scripts\activate
python -m pip install -r requirements-dev.txt
```

From the clone's root, `python -m zoorik.cli assess --help` runs your working copy. A real run reads every cloud your `az` and `aws` CLIs are signed in to, so run it only against accounts and clusters you own. `--sample-minutes 0` takes one usage sample, so a run takes seconds. It asks before it runs anything inside a VM; answer N to keep the run read-only.

## How the code is laid out

```
zoorik/
  __init__.py      the version, and the address the report asks to be sent to
  cli.py           the command line: builds the real runtime and the typed clients, runs both parts side by side,
                   writes the report, the exit codes and the stop
  errors.py        one exception type per kind of failure
  console.py       the terminal: whole lines, the one status line, lines held while a question is open
  format.py        the shared number formats and units: half_up, usd, count, cents, GiB and minutes
  output.py        the report's folder, checked before any read, and its file, never overwritten
  system/          the outside world behind protocols: process.py (the only user of subprocess), http.py (the
                   HttpClient protocol), clock.py, prompt.py, workers.py, memo.py, env.py (the environment's facts)
                   and runtime.py
  cloud/           az and aws: cli.py (one CLI call, its JSON, the retry when throttled, the reasons), azure.py,
                   aws.py, keys.py (each cloud's key and names), fields.py, shell.py (which Cloud Shell) and
                   started.py (what the run started in the cloud)
  kubectl/         the cluster reads: objects.py (the allowlist), api.py (KubeApi, Snapshot), direct.py (kubectl),
                   invoke.py (az aks command invoke), usage.py (kubectl top) and quantity.py
  pricing/         http.py (the one HTTP policy, the only user of urllib.request), retail.py (the Azure Retail
                   Prices API), machines.py, disks.py, azure_disks.py and aws_ebs.py
  kubernetes/      the Kubernetes part: discovery.py (which clusters, and how each is read), collect.py (pools,
                   machines and pods), placement.py (where a pod may run), packing.py, catalogue.py (the machine
                   types a region offers), allocatable.py (what a type the cluster doesn't run gives its pods),
                   shares.py, model.py, assessment.py and steps/ (the saving steps, in order)
  disks/           the disk part: azure_inventory.py, aws_inventory.py, facts.py, metrics.py, guest.py (the read
                   inside a VM), pools.py, model.py and assessment.py
  report/          the PDF: pdf.py and blocks.py (the engine), summary.py (page 1), kubernetes_pages.py,
                   disks_pages.py, closing.py (Not in the saving yet and Next steps), figures.py, names.py
                   (--anonymize) and document.py
tests/
  unit/            the unit tests, plus test_layers.py and test_names.py
  replay/          record and replay: the fake cloud, the scenarios and the goldens
```

Pages never work out figures, and figures never draw. Values are frozen dataclasses, and each constant lives in the module that owns it.

## The layers, and what enforces each rule

Each layer imports only the layers below it, highest first: `cli`; `report`; `kubernetes` and `disks`; `kubectl` and `pricing`; `cloud`; `system`; `console`, `format` and `output`; `errors`.

| Rule | Enforced by |
|---|---|
| Each layer imports only the layers below it; nothing imports `cli.py`; `kubernetes` and `disks` never import each other | import-linter's layers contract in `pyproject.toml` |
| Only the runtime, `cloud/` and `kubectl/` import the command runner | import-linter, a protected contract |
| Only the runtime, `cloud/` and `pricing/` import the HTTP protocol | import-linter, a protected contract |
| Only `cli.py` imports the runtime and the HTTP client in `pricing/http.py` | import-linter, a protected contract |
| Only `report/` draws the PDF | import-linter, a protected contract |
| `subprocess` is imported only in `zoorik/system/process.py` | `tests/unit/test_layers.py` |
| `urllib.request` and `http.client` are imported only in `zoorik/pricing/http.py` | `tests/unit/test_layers.py` |
| Only `cloud/` builds an `az` or `aws` command, and only `kubectl/` a `kubectl` or `kubelogin` command | `tests/unit/test_layers.py` |
| One catch-all, in `cli.py`; no bare `except` and no `except Exception` anywhere else | `tests/unit/test_layers.py` |
| File and folder names hold no product name and no short form of Kubernetes; products are named only where the report says what to do next; the short form appears only in the label keys `k8s.io` and `k8s.aws` | `tests/unit/test_names.py` |

## Run the checks

CI runs these on every pull request. Run them from the clone's root, in the venv:

```bash
ruff check .
ruff format --check .
mypy --platform linux
mypy --platform win32
lint-imports --no-cache
python -m unittest discover -s tests -t .
```

- **ruff** checks a wide rule set (pycodestyle, pyflakes, import order, PEP 8 names, PEP 257 docstrings, complexity, bugbear, pyupgrade, simplify, pylint and ruff's own) with Python 3.8 as the target, and `ruff format` sets the layout. There are no per-file ignores.
- **mypy** runs in strict mode, once for each platform, so the Windows and the POSIX branches are both checked.
- **The unit tests** also replay every scenario against its golden and check that each PDF is whole. They must pass on Python 3.8 too: CI runs them on Linux, macOS and Windows with the newest Python, and on Linux with Python 3.8.

CI also installs the package with `pip install .`, runs the command where no cloud CLI is installed and where `az` and `aws` are installed but not signed in, and runs the npm launcher and `npx` against a wheel built from the same commit.

## Record and replay

The tests never reach a real cloud. Each run in `tests/replay/` answers every `az`, `aws`, `kubectl` and price call from a scenario, through the fake cloud in `tests/replay/fake_cloud.py`, on a virtual clock. The scenarios are in `kubernetes.py`, `disks.py` and `estates.py` (the whole command), built with the helpers in `scenarios.py`, and `clusters.py` holds made-up API objects for the cluster readers. Names, ids and prices are neutral and round, so each figure can be worked out by hand. Each scenario's golden, in `tests/replay/goldens/`, holds the exit code, the console lines of each part and, for each PDF, its normalized hash, its page count and its text.

- **A change that changes a report on purpose** rewrites the goldens with `python -m tests.replay update`, which runs the tests and writes every golden from what the code gives. Read the diff of the goldens: it is part of the review.
- **A real run, recorded:** `python -m tests.replay record --out FILE -- zoorik assess [options]` runs the command for real, against the clouds you are signed in to, and writes a redacted recording. The file must be outside the repo, and on Linux and macOS only you can read it. A recording keeps of each cluster read only the allowlist's fields, of each kubeconfig only whether it needs kubelogin, and of your environment only which Cloud Shell you ran in. It still names your accounts, clusters and VMs, so never commit one or attach it to an issue.

## Add a step

A saving step is a class with a `name`, the page-1 `row` its saving counts in, and `apply(state)`, which gives the cluster state the step leaves (the `Step` protocol in `zoorik/kubernetes/steps/__init__.py`).

1. Write it in its own module under `zoorik/kubernetes/steps/`. Place pods only through `zoorik/kubernetes/placement.py` and `zoorik/kubernetes/packing.py`, so every step keeps the same placement rules.
2. Add it to the pipeline in `zoorik/kubernetes/assessment.py`, in its place. Each step works on what the one before left, and the pipeline doesn't apply a step that would cost more, so no step is negative and the steps add up exactly.
3. Test it with a figure worked out by hand, add a replay scenario, and update the goldens.
4. Describe it under "How the estimate works" in the README.

## Add a cloud

Each outside system is one protocol, with an Azure and an AWS implementation:

| Protocol | Lives in |
|---|---|
| `ClusterSource`: the clusters, and how each is read | `zoorik/kubernetes/discovery.py` |
| `KubeApi`: one cluster's objects and usage | `zoorik/kubectl/api.py` |
| `MachinePrices`: a machine type's hourly list price | `zoorik/pricing/machines.py` |
| `MachineCatalogue`: the machine types a region offers | `zoorik/kubernetes/catalogue.py` |
| `VmInventory`: the VMs and their data disks | `zoorik/disks/assessment.py` |
| `GuestReader`: the read inside a VM | `zoorik/disks/guest.py` |
| `DiskMetrics`: a disk's busiest minute of each hour | `zoorik/disks/metrics.py` |
| `DiskPrices`: a disk's monthly list price | `zoorik/pricing/disks.py` |

A new cloud is a new CLI client in `zoorik/cloud/` (the only place that builds its commands), an implementation of each protocol, and the wiring in `cli.py`, which alone builds the concrete classes. Prices must be public list prices, read through a public URL or the cloud's own CLI. Add replay scenarios for it, and its permissions to "Access it needs" in the README.

## Regions, prices and limits

- **Regions** come from the cloud at run time, and prices are looked up by region, so most new regions need no code. If a report says there is no list price in a region, first check that the public price list has that region.
- **Premium SSD v2 on VMs without a zone:** Azure's API doesn't say where this is offered, so `V2_NONZONAL_REGIONS` in `zoorik/disks/facts.py` is copied by hand from the Microsoft Learn page "Deploy a Premium SSD v2 managed disk" (Regional availability). To add a region, link that page in your pull request and update the date in the comment above the list.
- **Disk sizes and limits:** the Premium SSD sizes and performance and the Premium SSD v2 and gp3 limits in `zoorik/disks/pools.py`, and the gp3 baseline in `zoorik/pricing/aws_ebs.py`, come from the clouds' documentation. Link the page you used. The pool sizing constants at the top of that file are the disk method's own; please open an issue before changing one.

## Code style

- Type hints everywhere, checked by mypy in strict mode; PEP 8 names; a PEP 257 docstring on every module, class and function; dataclasses for values.
- One exception type per kind of failure, from `zoorik/errors.py`. Its text is the short reason the reader sees. Catch by type, never by text.
- A one-line comment says why, wherever a line guards something.
- Text the user sees is plain and short.
- Keep the diff to your change; lines go up to 120 characters.

## Pull requests

1. Fork the repo and make a branch.
2. Keep each pull request to one change.
3. Say what you changed, why, and how you tested it. If you ran it for real, say against what kind of estate, and replace your own names, or run with `--anonymize`. Never commit a report or a recording: they name things in your account.
4. CI must pass. A maintainer reviews every pull request and merges it.

By sending a pull request, you agree that your contribution is licensed under Apache-2.0, the license of this repo.

## Releases

Maintainers publish the `zoorik` package to PyPI, and its launcher to npm:

1. Bump `__version__` in `zoorik/__init__.py`, and `version` in `npm/package.json` to the same number, in a pull request.
2. Once it is merged, publish a GitHub release whose tag is `v` plus that version, such as `v2.0.1`.
3. The release workflow checks that the tag matches both versions, builds the package with `build` 1.6.1, installs the wheel, and runs `zoorik --version` and `zoorik assess --help`. It publishes to PyPI after a maintainer approves the `pypi` environment, then the same version to npm. PyPI trusts this workflow directly (trusted publishing), so no PyPI token is stored anywhere.

The npm launcher installs exactly its own version from PyPI, so each npm release runs the PyPI release of the same number.

## Security and conduct

- Please don't report a security problem in a public issue. Use **Report a vulnerability** on the repo's Security tab, or email founders@zoorik.com.
- Everyone who takes part follows our [Code of Conduct](https://github.com/getzoorik/.github/blob/main/CODE_OF_CONDUCT.md).

Questions: founders@zoorik.com.
