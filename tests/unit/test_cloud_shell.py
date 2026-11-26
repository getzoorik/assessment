"""Which Cloud Shell the run is in."""

import unittest

from zoorik.cloud.shell import CloudShell, cloud_shell
from zoorik.system.env import EnvFacts


class CloudShellTest(unittest.TestCase):
    """cloud_shell."""

    def test_each_shell_is_known_by_the_variables_it_sets(self) -> None:
        """Azure Cloud Shell sets ACC_CLOUD or a cloud-shell host; AWS CloudShell sets its execution environment."""
        cases = [
            (EnvFacts("PROD", None, None), CloudShell.AZURE),
            (EnvFacts(None, "cloud-shell/1.0", None), CloudShell.AZURE),
            (EnvFacts(None, None, "CloudShell"), CloudShell.AWS),
            (EnvFacts(None, None, None), None),
            (EnvFacts("", "vscode", "AWS_Lambda_python3.12"), None),
        ]
        for env, shell in cases:
            self.assertIs(cloud_shell(env), shell, env)
