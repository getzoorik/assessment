"""The zoorik command: starts k8s.py or disks.py, which the package ships unchanged."""

import sys
from typing import List, Optional

from zoorik import __version__

USAGE = """usage: zoorik k8s [options]
       zoorik disks

Zoorik's free cost assessments. Each one writes an Excel file that estimates
what you could save, for you to e-mail to founders@zoorik.com.

  k8s     Kubernetes: AKS, EKS, or any cluster kubectl can reach (Zoorik Coral)
  disks   the data disks of your Azure Linux VMs (Zoorik Amoeba)

"zoorik k8s --help" lists its options. What each one reads, runs and writes:
https://github.com/getzoorik/assessment
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
    if name == "k8s":
        from zoorik import k8s
        k8s.ONE_LINER = "zoorik k8s"
        return k8s.main(rest)
    if name == "disks":
        from zoorik import disks
        if rest in (["-h"], ["--help"]):
            print(disks.__doc__)
            return 0
        if rest == ["--version"]:
            print(f"Zoorik Amoeba: disk assessment {disks.VERSION}")
            return 0
        if rest:
            sys.stderr.write("zoorik disks has no options yet. Run it on its own: zoorik disks\n")
            return 2
        disks.ONE_LINER = "zoorik disks"
        return disks.main()
    sys.stderr.write(f"zoorik: no assessment called {name!r}.\n\n{USAGE}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
