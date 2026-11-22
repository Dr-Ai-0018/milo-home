from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from .schedule_models import SchedulePolicy, ScheduleView, minute

BEIJING = ZoneInfo("Asia/Shanghai")


def phase_at(policy: SchedulePolicy, state: dict, timestamp: int) -> tuple[str, str]:
    if state["paused"]:
        return "paused", "paused"
    if state["override_until"] is not None and timestamp < state["override_until"]:
        return state["override_phase"], "override"
    local = datetime.fromtimestamp(timestamp, BEIJING)
    plan, source = policy.day(local.date())
    clock = local.hour * 60 + local.minute
    for window in plan.windows:
        if minute(window.start) <= clock < minute(window.end):
            return window.phase, source
    return "resting", source


def day_start(local_date) -> int:
    return int(datetime.combine(local_date, time(), BEIJING).timestamp())


def describe(policy: SchedulePolicy | None, state: dict, now: int) -> ScheduleView:
    phase, source = phase_at(policy, state, now) if policy else ("unconfigured", "unconfigured")
    if state["paused"]:
        phase, source = "paused", "paused"
    local_date = datetime.fromtimestamp(now, BEIJING).date()
    today = policy.day(local_date)[0] if policy else None
    next_change = None
    next_fallback = None
    override_until = state["override_until"]
    if policy and not state["paused"]:
        boundaries = {day_start(local_date + timedelta(days=1))}
        for offset in range(374):
            date_ = local_date + timedelta(days=offset)
            boundaries.add(day_start(date_))
            plan, _ = policy.day(date_)
            for window in plan.windows:
                for bound in [window.start, window.end]:
                    boundaries.add(day_start(date_) + minute(bound) * 60)
        if override_until is not None:
            boundaries.add(override_until)
        next_change = next(
            (
                timestamp
                for timestamp in sorted(boundaries)
                if timestamp > now and phase_at(policy, state, timestamp) != (phase, source)
            ),
            None,
        )
        # At most 366 explicit days off may hide the next weekly alarm.
        for offset in range(374):
            date_ = local_date + timedelta(days=offset)
            plan, _ = policy.day(date_)
            for alarm in plan.fallback:
                timestamp = day_start(date_) + minute(alarm) * 60
                if timestamp > now and phase_at(policy, state, timestamp)[0] == "working":
                    next_fallback = timestamp
                    break
            if next_fallback is not None:
                break
    return ScheduleView(
        configured=policy is not None,
        paused=bool(state["paused"]),
        phase=phase,
        source=source,
        observed_at=now,
        revision=state["revision"],
        next_change_at=next_change,
        next_fallback_at=next_fallback,
        override_ends_at=(
            override_until if override_until is not None and override_until > now else None
        ),
        today=today,
    )
