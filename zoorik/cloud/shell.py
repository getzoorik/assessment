"""Which Cloud Shell the run is in, if any: it changes only how the report is downloaded and the idle warning."""

from enum import Enum
from typing import Optional

from zoorik.system.env import EnvFacts


class CloudShell(Enum):
    """A cloud's browser shell."""

    AZURE = "azure"
    AWS = "aws"


def cloud_shell(env: EnvFacts) -> Optional[CloudShell]:
    """Name the Cloud Shell the run is in, from the variables each shell sets; None outside both."""
    host = env.azureps_host_environment
    if env.acc_cloud or (host is not None and host.startswith("cloud-shell")):
        return CloudShell.AZURE
    if env.aws_execution_env == "CloudShell":
        return CloudShell.AWS
    return None
