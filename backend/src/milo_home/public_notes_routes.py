"""Anonymous read-only access to explicitly published home notes."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Request

from .public_notes_models import PublicNoteDetail, PublicNotePage

router = APIRouter(prefix="/api/notes")


@router.get("", response_model=PublicNotePage)
def notes(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=30)] = 10,
    before: Annotated[int | None, Query(ge=1)] = None,
):
    return request.app.state.public_notes.list_notes(limit=limit, before=before)


@router.get("/{note_id}", response_model=PublicNoteDetail)
def note(note_id: UUID, request: Request):
    return request.app.state.public_notes.get(str(note_id))
