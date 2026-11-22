"""Reserve SQLite growth for every still-unsettled native input batch."""

from .codex_rpc import ProtocolError

PAGE_LIMIT = 16384
CLEANUP_PAGES = 128
CLAIM_PAGES = 64


def capacity(connection, *, additional=0):
    page_size = connection.execute("PRAGMA page_size").fetchone()[0]
    pages = connection.execute("PRAGMA page_count").fetchone()[0]
    reusable = connection.execute("PRAGMA freelist_count").fetchone()[0]
    unsettled = connection.execute(
        "SELECT count(*) FROM deliveries d WHERE NOT EXISTS "
        "(SELECT 1 FROM events e WHERE e.run_id=d.run_id AND e.kind='stopped' "
        "AND json_extract(e.body,'$.stop_confirmed')=1)"
    ).fetchone()[0]
    available = PAGE_LIMIT - pages + reusable
    required = CLEANUP_PAGES * (unsettled + additional) + CLAIM_PAGES * additional
    return {
        "page_size": page_size,
        "used_pages": pages,
        "reusable_pages": reusable,
        "limit_pages": PAGE_LIMIT,
        "available_bytes": available * page_size,
        "cleanup_reserve_bytes": required * page_size,
        "unsettled_deliveries": unsettled,
        "has_capacity": pages <= PAGE_LIMIT and available >= required,
    }


def require_capacity(connection, *, additional=0):
    if not capacity(connection, additional=additional)["has_capacity"]:
        raise ProtocolError("Native message retention capacity; cleanup space reserved")
