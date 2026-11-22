"""Fixed previously public text; import never reads private or current website material."""

LEGACY_SOURCE = "frontend/src/data/homeNotes.ts"
# Fixed UUIDs preserved for compatibility with existing legacy imports.
LEGACY_IDS = {
    "work-status-filter": "650beaf2-831d-5d21-8ba3-1d59faf45a22",
    "work-receipt": "c47746e9-bdb8-5e95-9e0d-22075f630924",
    "work-attachments": "18c5ce19-2acb-5422-a201-e7a82c731872",
}
# Original display order: latest first. Import in reverse to preserve that order.
LEGACY_NOTES = (
    {
        "key": "work-status-filter",
        "title": "交办可以按状态找了",
        "text": "想看还在做的、等核对的，或已经结束的，都可以从交办列表直接切过去。",
        "category": "交办工作桌",
    },
    {
        "key": "work-receipt",
        "title": "在原单上就能看回执",
        "text": "打开交办单就能看这次处理的答复；如果还没有回执，页面会说明。",
        "category": "回执与交付",
    },
    {
        "key": "work-attachments",
        "title": "交办交付的文件可以下载了",
        "text": "有文件交付时，从原单下载，挑几份或一起打包都行。",
        "category": "回执与交付",
    },
)


def legacy_id(key):
    return LEGACY_IDS[key]


def import_legacy(service):
    created, skipped = [], []
    for note in reversed(LEGACY_NOTES):
        note_id = legacy_id(note["key"])
        content = {
            "title": note["title"],
            "summary": note["text"],
            "body": note["text"],
            "category": note["category"],
            "content_date": "2026-11-14",
        }
        _, imported = service.import_published(
            note_id, content, source_key="legacy-home:" + note["key"]
        )
        (created if imported else skipped).append(note_id)
    return {"created": created, "skipped": skipped, "source": LEGACY_SOURCE}
