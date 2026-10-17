# zoorik

Zoorik's free cloud cost assessment. It estimates how much you can reduce your cloud bill and sends nothing anywhere.

```bash
npx zoorik assess coral     # Kubernetes: AKS and EKS
npx zoorik assess amoeba    # block storage: the data disks of your VMs
```

It needs Python 3.8 or later. The first run creates a private Python environment in `~/.cache/zoorik` and installs the [zoorik package from PyPI](https://pypi.org/project/zoorik/) there, so nothing is installed into your system Python. Each later run updates it.

With Python alone, the same thing is:

```bash
pip install zoorik
zoorik assess coral
```

What each assessment reads, runs and writes is in [getzoorik/assessment](https://github.com/getzoorik/assessment). Steps for every cloud: [zoorik.com/assessment](https://zoorik.com/assessment).

Apache-2.0. Questions: founders@zoorik.com.
