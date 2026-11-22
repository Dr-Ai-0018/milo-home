"""Authorize one successor to an explicitly retired creation under the host lock."""

from dataclasses import dataclass
from uuid import uuid4

from .codex_rpc import ProtocolError
from .database_identity import private_run_directory
from .work_attempt_history import (
    attempt_rows,
    check_archive_bound,
    publish_history,
    read_history,
    read_retirement,
)
from .work_creation_records import choice_digest


def _snapshot(rows):
    return tuple(map(dict, rows))


@dataclass(frozen=True)
class SuccessorAuthorization:
    predecessor_id: str
    authorization_id: str
    evidence: dict
    previous_rows: tuple

    @classmethod
    def prepare(cls, claim, context, previous_home, predecessor_id):
        previous_home = private_run_directory(previous_home)
        if previous_home.is_relative_to(claim.directory) or claim.directory.is_relative_to(
            previous_home
        ):
            raise ProtocolError("New creation requires a separate dedicated home")
        retired = read_retirement(previous_home, live=True)
        if retired.choice["id"] != predecessor_id or any(
            retired.choice.get(key) != value
            for key, value in context.items()
            if key != "codex_home"
        ):
            raise ProtocolError("Retired creation identity differs from the new authorization")
        with claim.database.connect() as connection:
            connection.execute("BEGIN")
            rows = attempt_rows(connection, claim.session_id)
        history = read_history(previous_home, retired.choice, *rows)
        if not any(row["id"] == predecessor_id and row["state"] == "closed" for row in rows[0]):
            raise ProtocolError("Original creation occupancy is not closed")
        evidence = history | {predecessor_id: retired}
        check_archive_bound(evidence)
        return cls(predecessor_id, str(uuid4()), evidence, tuple(map(_snapshot, rows)))

    def choice_fields(self):
        return {"supersedes_id": self.predecessor_id, "authorization_id": self.authorization_id}

    def publish(self, home, choice):
        publish_history(home, self.evidence)
        row, receipt, grant = self.records(choice)
        old, retirements, grants = self.previous_rows
        read_history(home, choice, [*old, row], [*retirements, receipt], [*grants, grant])

    def records(self, choice):
        digest = choice_digest(choice)
        row = {
            key: choice[key]
            for key in (
                "id",
                "session_id",
                "owner_revision",
                "workspace_revision",
                "created_at",
                "deadline_at",
                "supersedes_id",
                "authorization_id",
            )
        } | {"choice_digest": digest, "state": "held"}
        receipt = {
            "attempt_id": self.predecessor_id,
            "evidence_digest": self.evidence[self.predecessor_id].digest,
        }
        grant = {
            "id": self.authorization_id,
            "predecessor_id": self.predecessor_id,
            "new_choice_digest": digest,
        }
        return row, receipt, grant

    def claim(self, connection, session_id, choice):
        if tuple(map(_snapshot, attempt_rows(connection, session_id))) != self.previous_rows:
            raise ProtocolError("Original creation chain changed during preparation")
        _, receipt, grant = self.records(choice)
        connection.execute(
            "INSERT INTO work_main_attempt_retirements (attempt_id,evidence_digest) "
            "VALUES (:attempt_id,:evidence_digest)",
            receipt,
        )
        connection.execute(
            "INSERT INTO work_main_attempt_grants (id,predecessor_id,new_choice_digest) "
            "VALUES (:id,:predecessor_id,:new_choice_digest)",
            grant,
        )
