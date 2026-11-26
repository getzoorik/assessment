"""The commands the run starts inside the user's clusters and VMs."""

import unittest

from zoorik.cloud.started import StartedCommands


class StartedCommandsTest(unittest.TestCase):
    """StartedCommands."""

    def test_nothing_started_is_said_plainly(self) -> None:
        """With nothing started, the note says nothing in the cloud was changed."""
        self.assertEqual(StartedCommands().note(), "Nothing in your cloud was changed.")

    def test_each_kind_is_counted_once_per_place_with_where_it_shows(self) -> None:
        """Each kind counts the places it started in, once each, and says where its starts show."""
        started = StartedCommands()
        started.invoke("cluster-one")
        started.invoke("cluster-one")
        started.invoke("cluster-two")
        started.run_command("vm-one")
        started.ssm("i-one")
        self.assertEqual(
            started.note(),
            "az aks command invoke had already started in 2 clusters (see the activity log); "
            "Azure Run Command had already started in 1 VM (see the activity log); "
            "Systems Manager Run Command had already started in 1 instance (see its Run Command history); "
            "nothing else in your cloud was changed.",
        )
