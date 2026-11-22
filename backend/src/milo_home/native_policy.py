"""Versioned native settings beside the unchanged, registered conversation identity."""

import hashlib
import json
import os
import sqlite3
import stat
from contextlib import contextmanager
from dataclasses import replace
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from .codex_config import private_create, private_directory, private_read
from .codex_execution_policy import NativeExecutionPolicy
from .codex_rpc import ProtocolError


class NativePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    revision: int = Field(strict=True, ge=1)
    registered_at: int = Field(strict=True, ge=0)
    previous_digest: str | None
    baseline_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    owner_id: UUID
    session_id: UUID
    model: str = Field(min_length=1, max_length=200, pattern=r"^[^\s\x00-\x1f\x7f]+$")
    execution_policy: Literal["auto_review"] = "auto_review"
    instructions_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    def encoded(self):
        body = self.model_dump(mode="json")
        # Retain canonical bytes of policies registered before rules were pinned.
        if self.instructions_digest is None:
            body.pop("instructions_digest")
        return json.dumps(body, sort_keys=True, ensure_ascii=False)

    @property
    def digest(self):
        return hashlib.sha256(self.encoded().encode()).hexdigest()

    def conversation_binding(self, journal):
        return replace(journal.binding(), model=self.model)

    @property
    def execution(self):
        return NativeExecutionPolicy.AUTO_REVIEW

    def summary(self):
        return {
            "registered": True,
            "revision": self.revision,
            "digest": self.digest,
            "model": self.model,
            "execution_policy": self.execution_policy,
            "instructions_pinned": self.instructions_digest is not None,
            "execution_available": False,
        }


class NativePolicyStore:
    def __init__(self, journal):
        self.journal = journal
        self.directory = journal.path.parent
        self.path = self.directory / "native-session-policy.sqlite3"
        self.required = self.directory / "native-session-policy.required"

    def read(self):
        if not self.path.exists() and not self.path.is_symlink():
            if self.required.exists() or self.required.is_symlink():
                raise ProtocolError("Registered native policy is missing")
            return None
        if private_read(self.required) != b"1\n":
            raise ProtocolError("Native policy registration marker differs")
        with self._connect() as connection:
            if connection.execute("PRAGMA user_version").fetchone()[0] != 1:
                raise ProtocolError("Native policy registration is incomplete")
            row = connection.execute(
                "SELECT body,digest FROM revisions ORDER BY revision DESC LIMIT 1"
            ).fetchone()
            if row is None:
                raise ProtocolError("Native policy registration is incomplete")
            policy = NativePolicy.model_validate_json(row[0])
            if policy.encoded() != row[0] or policy.digest != row[1]:
                raise ProtocolError("Native policy evidence differs")
            self._identity(policy)
            return policy

    def _identity(self, policy):
        self.journal.installation()
        binding = self.journal.business_binding()
        if (
            binding is None
            or policy.baseline_fingerprint != self.journal.fingerprint()
            or str(policy.owner_id) != binding.owner_id
            or str(policy.session_id) != binding.session_id
        ):
            raise ProtocolError("Native policy belongs to another main binding")

    def configure(self, *, owner, session, model, expected, now):
        """Caller holds the shared host lease and has checked authority/occupancy."""
        private_directory(self.directory)
        instructions_digest = self.instructions_digest()
        candidate = NativePolicy(
            revision=1,
            registered_at=now,
            previous_digest=None,
            baseline_fingerprint=self.journal.fingerprint(),
            owner_id=UUID(owner),
            session_id=UUID(session),
            model=model,
            instructions_digest=instructions_digest,
        )
        self._identity(candidate)
        if not self.path.exists() and not self.path.is_symlink() and expected is not None:
            raise ProtocolError("Native policy revision changed; inspect before updating")
        try:
            private_create(self.required, b"1\n")
        except FileExistsError:
            if private_read(self.required) != b"1\n":
                raise ProtocolError("Native policy registration marker differs") from None
        if not self.path.exists() and not self.path.is_symlink():
            private_create(self.path, b"")
        with self._connect(write=True) as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                if connection.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone():
                    raise ProtocolError("Unversioned native policy is not empty")
                connection.execute(
                    "CREATE TABLE revisions (revision INTEGER PRIMARY KEY, "
                    "body TEXT NOT NULL,digest TEXT NOT NULL UNIQUE)"
                )
                for operation in ("UPDATE", "DELETE"):
                    connection.execute(
                        f"CREATE TRIGGER immutable_{operation.lower()} BEFORE {operation} "
                        "ON revisions BEGIN SELECT RAISE(ABORT,'Native policy is immutable'); END"
                    )
                connection.execute("PRAGMA user_version=1")
            elif version != 1:
                raise ProtocolError("Unsupported native policy version")
            row = connection.execute(
                "SELECT body,digest FROM revisions ORDER BY revision DESC LIMIT 1"
            ).fetchone()
            previous = NativePolicy.model_validate_json(row[0]) if row else None
            if previous:
                self._identity(previous)
                if previous.digest != row[1] or previous.encoded() != row[0]:
                    raise ProtocolError("Native policy evidence differs")
                if now < previous.registered_at:
                    raise ProtocolError("Native policy clock moved backwards")
            if (
                previous
                and previous.model == model
                and previous.instructions_digest == instructions_digest
                and expected
                in {
                    previous.digest,
                    previous.previous_digest,
                }
            ):
                if str(previous.owner_id) != owner or str(previous.session_id) != session:
                    raise ProtocolError("Native policy account differs")
                return previous
            if expected != (previous.digest if previous else None):
                raise ProtocolError("Native policy revision changed; inspect before updating")
            policy = NativePolicy(
                revision=previous.revision + 1 if previous else 1,
                registered_at=now,
                previous_digest=previous.digest if previous else None,
                baseline_fingerprint=self.journal.fingerprint(),
                owner_id=UUID(owner),
                session_id=UUID(session),
                model=model,
                instructions_digest=instructions_digest,
            )
            self._identity(policy)
            connection.execute(
                "INSERT INTO revisions VALUES (?,?,?)",
                (policy.revision, policy.encoded(), policy.digest),
            )
            return policy

    def instructions_digest(self):
        """Pin native CODEX_HOME rules; never import developer history or project rules."""
        content = private_read(self.directory / "AGENTS.md", limit=32 * 1024)
        if not content.decode("utf-8").strip():
            raise ProtocolError("Native home instructions must not be empty")
        if (self.directory / "AGENTS.override.md").exists() or (
            self.directory / "AGENTS.override.md"
        ).is_symlink():
            raise ProtocolError("Unregistered native instruction override exists")
        return hashlib.sha256(content).hexdigest()

    def verify_instructions(self, policy):
        if policy.instructions_digest is None:
            raise ProtocolError("Native home instructions must be explicitly registered")
        if self.instructions_digest() != policy.instructions_digest:
            raise ProtocolError("Native home instructions changed; inspect before executing")

    @contextmanager
    def _connect(self, *, write=False):
        private_directory(self.directory)
        for suffix in ("", "-journal", "-wal", "-shm"):
            path = self.path.with_name(self.path.name + suffix)
            if suffix and not path.exists() and not path.is_symlink():
                continue
            metadata = path.lstat()
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or metadata.st_mode & 0o077
            ):
                raise ProtocolError("Native policy must be private and non-linked")
        connection = sqlite3.connect(
            self.path.as_uri() + ("?mode=rw" if write else "?mode=ro"),
            uri=True,
            isolation_level=None,
        )
        try:
            connection.execute("PRAGMA synchronous=FULL")
            if write:
                connection.execute("PRAGMA max_page_count=128")
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
