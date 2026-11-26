"""One exception type for each kind of failure the run names, so callers catch by type and never by text."""


class AssessmentError(Exception):
    """A failure the run expects; its text is the short reason the reader sees."""


class ToolNotInstalled(AssessmentError):
    """A program the run needs is not on PATH."""


class NotSignedIn(AssessmentError):
    """A cloud CLI is installed but not signed in."""


class AccessDenied(AssessmentError):
    """The signed-in identity may not read what the run asked for."""


class CommandFailed(AssessmentError):
    """A program exited with an error that no other type names."""


class CommandTimedOut(AssessmentError):
    """A program gave no answer in time."""


class Throttled(AssessmentError):
    """A cloud kept throttling requests after every retry."""


class UnexpectedResponse(AssessmentError):
    """An answer did not have the shape its API documents."""


class PriceListUnreachable(AssessmentError):
    """A public price list did not answer."""


class PriceNotFound(AssessmentError):
    """A price list has no price for a machine or a disk."""


class ClusterUnreadable(AssessmentError):
    """A cluster could not be read in full."""


class ApiUnreachable(ClusterUnreadable):
    """A cluster's API server did not answer from where the run is."""


class ReportNotWritable(AssessmentError):
    """The report's folder or file cannot be written."""
