"""The zoorik command: starts k8s.py or disks.py, which the package ships unchanged."""

import sys
from typing import List, Optional

from zoorik import __version__

USAGE = """usage: zoorik assess coral [options]
       zoorik assess amoeba

Zoorik's free cloud cost assessment. It estimates how much you can reduce your
cloud bill and writes a PDF report for you to email to founders@zoorik.com.

  coral    Kubernetes: AKS, EKS, or any cluster kubectl can reach
  amoeba   block storage: the data disks of your Azure VMs and EC2 instances

"zoorik assess coral --help" lists its options. What each one reads, runs and
writes: https://github.com/getzoorik/assessment
"""


def main(argv: Optional[List[str]] = None) -> int:
    args = sys.argv[1:] if argv is None else list(argv)
    if not args:
        sys.stderr.write(USAGE)
        return 2
    name, rest = args[0], args[1:]
    if name in ("-h", "--help"):
        sys.stdout.write(USAGE)
        return 0
    if name == "--version":
        print(f"zoorik {__version__}")
        return 0
    if name == "assess":
        if not rest:
            sys.stderr.write(USAGE)
            return 2
        name, rest = rest[0], rest[1:]
    name = {"k8s": "coral", "disks": "amoeba"}.get(name, name)
    if name == "coral":
        from zoorik import k8s
        k8s.ONE_LINER = "zoorik assess coral"
        return k8s.main(rest)
    if name == "amoeba":
        from zoorik import disks
        disks.ONE_LINER = "zoorik assess amoeba"
        return disks.main(rest)
    sys.stderr.write(f"zoorik: no assessment called {name!r}. Try: zoorik assess coral\n\n{USAGE}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
