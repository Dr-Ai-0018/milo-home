"""Shared read-only snapshot helpers and canonical historical-fact fingerprints."""

import hashlib
import json


class RetentionBlocked(ValueError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def require(condition, reason):
    if not condition:
        raise RetentionBlocked(reason)


def rows(connection, query, values=()):
    return [dict(row) for row in connection.execute(query, values)]


def one(connection, query, values=()):
    row = connection.execute(query, values).fetchone()
    return dict(row) if row is not None else None


def digest(value):
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()
