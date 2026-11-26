"""Where the report goes: a folder proven writable before anything is read, and a file that never replaces another."""

import os
import tempfile
from datetime import datetime
from typing import Optional

from zoorik.errors import ReportNotWritable

REPORT_STEM = "zoorik-assessment"


def report_folder(out: Optional[str]) -> str:
    """Make the report's folder (out, else the current folder) and prove a file can be made and removed there.

    The run calls this before it reads anything, so a folder it can't write stops the run at once.
    """
    folder = os.path.abspath(os.curdir if out is None else out)
    if os.path.isfile(folder):
        raise ReportNotWritable(f"{folder} is a file; --out takes a folder")
    try:
        os.makedirs(folder, exist_ok=True)
        # A probe file, because os.access trusts the mode bits and misses ACLs and read-only mounts.
        handle, probe = tempfile.mkstemp(prefix=".zoorik-probe-", dir=folder)
        os.close(handle)
        os.remove(probe)
    except OSError as error:
        raise ReportNotWritable(f"cannot write to {folder}: {error.strerror}") from None
    return folder


def write_report(folder: str, data: bytes, written: datetime) -> str:
    """Write the report as zoorik-assessment-YYYYMMDD-HHMM.pdf, taking -2, -3 and so on while a file has the name.

    written is the UTC time of writing. The name is chosen then, and the file is created only if no file has that
    name, so a report is never overwritten, even by another run writing in the same minute.
    """
    stem = f"{REPORT_STEM}-{written:%Y%m%d-%H%M}"
    number = 1
    while True:
        path = os.path.join(folder, f"{stem}.pdf" if number == 1 else f"{stem}-{number}.pdf")
        try:
            with open(path, "xb") as report:
                report.write(data)
            return path
        except FileExistsError:
            number += 1
        except OSError as error:
            raise ReportNotWritable(f"cannot write {path}: {error.strerror}") from None
