from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Clock = Annotated[str, Field(pattern=r"^(?:[0-1][0-9]|2[0-3]):[0-5][0-9]$|^24:00$")]
Phase = Literal["working", "winding_down", "resting"]


def minute(value: str) -> int:
    hours, minutes = map(int, value.split(":"))
    return hours * 60 + minutes


class ScheduleInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Window(ScheduleInput):
    start: Clock
    end: Clock
    phase: Literal["working", "winding_down"]

    @model_validator(mode="after")
    def ordered(self):
        if minute(self.start) >= minute(self.end):
            raise ValueError("Windows must be within one day and have increasing bounds")
        return self


class DayPlan(ScheduleInput):
    windows: list[Window] = Field(max_length=6)
    fallback: list[Clock] = Field(max_length=6)

    @model_validator(mode="after")
    def consistent(self):
        self.windows.sort(key=lambda window: minute(window.start))
        if any(
            minute(left.end) > minute(right.start)
            for left, right in zip(self.windows, self.windows[1:], strict=False)
        ):
            raise ValueError("Windows must not overlap")
        if len(set(self.fallback)) != len(self.fallback):
            raise ValueError("Fallback times must be unique")
        for alarm in self.fallback:
            if not any(
                window.phase == "working"
                and minute(window.start) <= minute(alarm) < minute(window.end)
                for window in self.windows
            ):
                raise ValueError("Fallback must be inside a working window")
        self.fallback.sort()
        return self


class SchedulePolicy(ScheduleInput):
    timezone: Literal["Asia/Shanghai"] = "Asia/Shanghai"
    weekdays: DayPlan
    days_off: DayPlan
    relaxed_dates: list[date] = Field(default_factory=list, max_length=366)

    @model_validator(mode="after")
    def unique_dates(self):
        if len(set(self.relaxed_dates)) != len(self.relaxed_dates):
            raise ValueError("Relaxed dates must be unique")
        self.relaxed_dates.sort()
        return self

    def day(self, local_date: date) -> tuple[DayPlan, str]:
        relaxed = local_date.weekday() >= 5 or local_date in self.relaxed_dates
        return (self.days_off, "relaxed") if relaxed else (self.weekdays, "weekly")


class ScheduleView(BaseModel):
    timezone: Literal["Asia/Shanghai"] = "Asia/Shanghai"
    automatic_wakeup: Literal[False] = False
    configured: bool
    paused: bool
    phase: Literal["working", "winding_down", "resting", "paused", "unconfigured"]
    source: Literal["weekly", "relaxed", "override", "paused", "unconfigured"]
    observed_at: int
    revision: int
    next_change_at: int | None
    next_fallback_at: int | None
    override_ends_at: int | None
    today: DayPlan | None
