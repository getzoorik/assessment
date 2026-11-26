# zoorik

Zoorik's free cloud cost assessment. One command reads your AKS and EKS clusters and the data disks of your VMs on Azure and AWS, estimates how much you could reduce your cloud cost, and writes one PDF report for you to email to founders@zoorik.com. It sends nothing anywhere.

```bash
npx zoorik assess
```

It runs on Linux, macOS and Windows, and in Azure Cloud Shell and AWS CloudShell. It needs Python 3.8 or later, as `python3` (on Windows, `py -3`). The first run creates a private Python environment in `~/.cache/zoorik` and installs the [zoorik package from PyPI](https://pypi.org/project/zoorik/) there, at this launcher's own version, so nothing is installed into your system Python.

With Python alone, the same thing is:

```bash
pip install zoorik
zoorik assess
```

What it reads, changes and writes is in [getzoorik/assessment](https://github.com/getzoorik/assessment). Steps for every cloud: [zoorik.com/assessment](https://zoorik.com/assessment).

Apache-2.0. Questions: founders@zoorik.com.
