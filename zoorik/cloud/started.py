"""The commands the run starts inside the user's clusters and VMs, kept so a stop can say what had started."""

import threading
from typing import List, Set

from zoorik.format import count


class StartedCommands:
    """The command invoke, Run Command and Systems Manager calls the run has started, whatever their outcome."""

    def __init__(self) -> None:
        """Start with none."""
        self._lock = threading.Lock()
        self._invoked: Set[str] = set()
        self._run_command: Set[str] = set()
        self._ssm: Set[str] = set()

    def invoke(self, cluster: str) -> None:
        """Keep that az aks command invoke started in cluster, named by its resource id."""
        with self._lock:
            self._invoked.add(cluster)

    def run_command(self, vm: str) -> None:
        """Keep that Azure Run Command started in vm, named by its resource id."""
        with self._lock:
            self._run_command.add(vm)

    def ssm(self, instance: str) -> None:
        """Keep that Systems Manager Run Command started in instance."""
        with self._lock:
            self._ssm.add(instance)

    def note(self) -> str:
        """Say what the run had started in the user's cloud, and where each start shows; or that nothing had."""
        with self._lock:
            invoked, run_command, ssm = len(self._invoked), len(self._run_command), len(self._ssm)
        started: List[str] = []
        if invoked:
            started.append(
                f"az aks command invoke had already started in {count(invoked, 'cluster')} (see the activity log)"
            )
        if run_command:
            started.append(
                f"Azure Run Command had already started in {count(run_command, 'VM')} (see the activity log)"
            )
        if ssm:
            started.append(
                f"Systems Manager Run Command had already started in {count(ssm, 'instance')} "
                "(see its Run Command history)"
            )
        if not started:
            return "Nothing in your cloud was changed."
        return "; ".join(started) + "; nothing else in your cloud was changed."
