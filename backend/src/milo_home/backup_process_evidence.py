"""Bounded, read-only observations of recorded families, never global quiescence."""

import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

from .codex_journal import PrivateJournal
from .codex_rpc import ProtocolError
from .journal_segments import STORAGE_LIMIT
from .journal_storage import canonical
from .work_creation_records import CHOICE, EVENTS, CreationRecords
from .work_stop import _matches, _show, boot_id, identity_shape

MAX_FAMILIES = 64
UNVERIFIED_WRITERS = ["ingress", "coordinators", "external-home-and-workspace-writers"]


def _online_families(home, *, skip=None):
    """Include streams with no ready and creation connections with incomplete launch."""
    journal = PrivateJournal(home, initialize=False, read_only=True)
    families = []
    streams = journal.list_streams(limit=MAX_FAMILIES + 1 if skip is None else None)
    if skip is not None:
        streams = [item for item in streams if item["stream"]["id"] not in skip]
    if len(streams) > MAX_FAMILIES:
        raise ValueError("Process evidence exceeds the family bound")
    for item in streams:
        stream = item["stream"]
        events = journal.process_events(stream["id"], limit=129)
        if len(events) > 128:
            raise ValueError("Process evidence exceeds the event bound")
        families.append(_family("stream", stream["id"], stream["unit"], events))
    if (home / EVENTS).exists():
        records = CreationRecords(home)
        connections = {}
        for event in records.events:
            unit = event.get("connection")
            if not isinstance(unit, str) or not unit:
                raise ValueError("Creation evidence lacks its process connection")
            connections.setdefault(unit, [])
            if event.get("kind") == "process":
                connections[unit].append(event)
        families.extend(
            _family("creation", unit, unit, events) for unit, events in sorted(connections.items())
        )
    if (home / CHOICE).exists() and not any(f["source"] == "creation" for f in families):
        families.append(
            {
                "source": "creation",
                "reference": CHOICE,
                "unit": None,
                "identity": None,
                "gap": "missing-creation-process-records",
            }
        )
    if len(families) > MAX_FAMILIES:
        raise ValueError("Process evidence exceeds the family bound")
    units = [family["unit"] for family in families]
    for family in families:
        if units.count(family["unit"]) != 1:
            family.update(identity=None, gap="duplicate-unit")
    return families


def _observation_scope(home, *, budget=STORAGE_LIMIT):
    journal = PrivateJournal(home, initialize=False, read_only=True)
    if journal.storage_summary(budget=budget) is None:
        return _online_families(home), None
    historical = []
    for record in journal.segment_catalogue():
        if record["has_overlay"]:
            continue
        scope = record["settlement"]["scope"]
        historical.append(
            {
                "source": "stream",
                "reference": record["stream"],
                "unit": scope["unit"],
                "identity": scope["process_identity"],
                "segment_sha256": record["sha256"],
                "settlement_digest": record["settlement_digest"],
                "stop_sequence": scope["stop_sequence"],
                "stop": scope["stop"],
                "fresh_observation": False,
            }
        )
    online = _online_families(home, skip={item["reference"] for item in historical})
    units = [item["unit"] for item in online + historical]
    if len(units) != len(set(units)):
        raise ValueError("Online and historical process scopes contain duplicate families")
    return online, historical


def recorded_families(home, *, budget=STORAGE_LIMIT):
    """List complete family coverage; archived evidence remains explicitly historical."""
    online, historical = _observation_scope(home, budget=budget)
    return online + (historical or [])


def _family(source, reference, unit, events):
    family = {
        "source": source,
        "reference": reference,
        "unit": unit,
        "identity": None,
    }
    ready = [event for event in events if event.get("phase") == "ready"]
    if len(ready) != 1:
        return family | {"gap": "missing-or-duplicate-ready"}
    identity = {
        key: ready[0].get(key) for key in ("unit", "group", "boot_id", "invocation", "main_pid")
    }
    try:
        identity_shape(identity)
    except ProtocolError:
        return family | {"gap": "invalid-ready-identity"}
    if identity["unit"] != unit:
        return family | {"gap": "ready-unit-mismatch"}
    return family | {
        "identity": identity,
        "gap": None,
    }


def observe_family(identity, *, run=subprocess.run, cgroup_root=Path("/sys/fs/cgroup")):
    """Two fresh systemd samples bracket a hierarchical cgroup observation."""
    identity_shape(identity)
    report = {
        "absent_observed": False,
        "reason": "observation-unavailable",
    }
    try:
        report["boot_before"] = boot_id()
        if report["boot_before"] != identity["boot_id"]:
            return report | {"reason": "boot-changed"}
        before = _show(run, identity["unit"])
        report["systemd_before"] = before
        group = cgroup_root / identity["group"].lstrip("/")
        if group.resolve() != group:
            return report | {"reason": "cgroup-path-changed"}
        exists = group.exists()
        populated = None
        if exists:
            events = dict(
                line.split() for line in (group / "cgroup.events").read_text().splitlines()
            )
            populated = events.get("populated")
        report["cgroup"] = {
            "exists": exists,
            "populated": populated,
        }
        after = _show(run, identity["unit"])
        report["systemd_after"] = after
        report["boot_after"] = boot_id()
        if report["boot_after"] != identity["boot_id"]:
            return report | {"reason": "boot-changed"}
        if not _matches(before, identity) or not _matches(after, identity):
            return report | {"reason": "identity-unconfirmed"}
        if any(
            values.get("ActiveState") not in {"inactive", "failed"} or values.get("MainPID") != "0"
            for values in (before, after)
        ):
            return report | {"reason": "family-active"}
        if (exists and populated != "0") or (
            not exists and any(values.get("LoadState") != "not-found" for values in (before, after))
        ):
            return report | {"reason": "cgroup-empty-unconfirmed"}
        return report | {
            "absent_observed": True,
            "reason": "recorded-family-absent",
        }
    except (OSError, ValueError, subprocess.SubprocessError):
        return report


def observe_recorded(
    home, *, run=subprocess.run, cgroup_root=Path("/sys/fs/cgroup"), budget=STORAGE_LIMIT
):
    started = datetime.now(UTC).isoformat()
    families, historical = _observation_scope(home, budget=budget)
    deadline = time.monotonic() + 10
    observations = []

    def bounded_run(command, **options):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired("process-observation", 10)
        return run(command, **(options | {"timeout": min(options["timeout"], remaining)}))

    for family in families:
        if family["identity"] is None:
            observation = {
                "absent_observed": False,
                "reason": family["gap"],
            }
        elif time.monotonic() >= deadline:
            observation = {
                "absent_observed": False,
                "reason": "observation-deadline",
            }
        else:
            observation = observe_family(
                family["identity"], run=bounded_run, cgroup_root=cgroup_root
            )
        observations.append(observation)
    result = {
        "started_at": started,
        "finished_at": datetime.now(UTC).isoformat(),
        "families": families,
        "observations": observations,
        "recorded_families_absent_observed": bool(families)
        and not historical
        and all(item["absent_observed"] for item in observations),
        "unverified_writers": list(UNVERIFIED_WRITERS),
        "quiescence_verified": False,
    }
    if historical is not None:
        import hashlib

        result.update(
            {
                "version": 2,
                "archived_families": historical,
                "archived_count": len(historical),
                "archived_sha256": hashlib.sha256(canonical(historical)).hexdigest(),
                "online_families_absent_observed": bool(families)
                and all(item["absent_observed"] for item in observations),
            }
        )
    return result


def verify_observation_sources(archive, manifest, *, budget=STORAGE_LIMIT):
    """Observation claims stay historical; restoring files never makes them fresh."""
    evidence = manifest.get("process_observations")
    if evidence is None:
        return
    expected, historical = _observation_scope(archive / "runtime/evidence", budget=budget)
    if (
        not isinstance(evidence, dict)
        or set(evidence) != {"before", "after"}
        or any(not _valid_sample(sample, expected, historical) for sample in evidence.values())
        or manifest.get("quiescence_verified") is not False
    ):
        raise ValueError("Process observations differ from the captured original families")


def _valid_sample(sample, families, historical=None):
    if (
        not isinstance(sample, dict)
        or sample.get("families") != families
        or sample.get("quiescence_verified") is not False
        or sample.get("unverified_writers") != UNVERIFIED_WRITERS
        or not isinstance(sample.get("observations"), list)
        or len(sample["observations"]) != len(families)
    ):
        return False
    for family, observation in zip(families, sample["observations"], strict=True):
        if (
            not isinstance(observation, dict)
            or type(observation.get("absent_observed")) is not bool
        ):
            return False
        if observation["absent_observed"] and not _absence_supported(family, observation):
            return False
        if family["identity"] is None and observation.get("reason") != family["gap"]:
            return False
    all_absent = bool(families) and all(o["absent_observed"] for o in sample["observations"])
    if historical is not None:
        import hashlib

        if (
            sample.get("version") != 2
            or sample.get("archived_families") != historical
            or sample.get("archived_count") != len(historical)
            or sample.get("archived_sha256") != hashlib.sha256(canonical(historical)).hexdigest()
            or sample.get("online_families_absent_observed") is not all_absent
        ):
            return False
    elif "version" in sample:
        return False
    return sample.get("recorded_families_absent_observed") is (all_absent and not historical)


def _absence_supported(family, observation):
    identity = family["identity"]
    before = observation.get("systemd_before")
    after = observation.get("systemd_after")
    group = observation.get("cgroup")
    if (
        identity is None
        or observation.get("reason") != "recorded-family-absent"
        or observation.get("boot_before") != identity["boot_id"]
        or observation.get("boot_after") != identity["boot_id"]
        or not isinstance(before, dict)
        or not isinstance(after, dict)
        or not isinstance(group, dict)
        or not _matches(before, identity)
        or not _matches(after, identity)
        or any(
            v.get("ActiveState") not in {"inactive", "failed"} or v.get("MainPID") != "0"
            for v in (before, after)
        )
    ):
        return False
    return (group.get("exists") is True and group.get("populated") == "0") or (
        group.get("exists") is False
        and group.get("populated") is None
        and all(v.get("LoadState") == "not-found" for v in (before, after))
    )
