"""Read timer evidence without trusting configuration or one finite invocation."""

import subprocess

from .automatic_services import SERVICE_TTL, observation_current
from .reply_status import ReplyServiceView


def timer_active(unit):
    try:
        result = subprocess.run(
            ["systemctl", "show", unit, "-p", "ActiveState", "-p", "Triggers", "-p", "LoadState"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    return bool(
        result.returncode == 0
        and values.get("LoadState") == "loaded"
        and values.get("ActiveState") == "active"
        and unit.removesuffix(".timer") + ".service" in values.get("Triggers", "").split()
    )


def reply_service_view(row, owner, now, boot):
    if row is None or row["owner_id"] != owner:
        return ReplyServiceView()
    if not observation_current(row, now, boot):
        invalid = (
            (row["checked_boot"] is not None and row["checked_boot"] != boot)
            or row["updated_at"] > now
            or (row["checked_at"] or 0) > now
        )
        return ReplyServiceView(
            reason="clock_or_boot_invalid" if invalid else "observation_expired"
        )
    values = {
        "observed_at": row["checked_at"],
        "valid_until": row["checked_at"] + SERVICE_TTL,
    }
    if row["fault_code"]:
        return ReplyServiceView(state="faulted", reason="execution_fault", **values)
    if not row["enabled"]:
        return ReplyServiceView(state="disabled", reason="explicitly_disabled", **values)
    if row["timer_active"]:
        return ReplyServiceView(state="scheduled", reason="check_current", **values)
    return ReplyServiceView(reason="observation_missing")
