"""The environment variables the run reads, which say which Cloud Shell it runs in, if any."""

from dataclasses import dataclass
from typing import Mapping, Optional


@dataclass(frozen=True)
class EnvFacts:
    """The environment variables the run reads, which say which Cloud Shell it runs in, if any.

    They are also the only environment a recording keeps. A program the run starts gets the user's whole environment.
    """

    acc_cloud: Optional[str]
    azureps_host_environment: Optional[str]
    aws_execution_env: Optional[str]

    @classmethod
    def read(cls, environ: Mapping[str, str]) -> "EnvFacts":
        """Take the facts from environ, and nothing else."""
        return cls(environ.get("ACC_CLOUD"), environ.get("AZUREPS_HOST_ENVIRONMENT"), environ.get("AWS_EXECUTION_ENV"))
