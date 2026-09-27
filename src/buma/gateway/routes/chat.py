from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Annotated, Literal

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from buma.core.config import Settings, get_settings
from buma.gateway.chat.runtime import ChatService, chat_budget, chat_enabled
from buma.gateway.deps import get_db, get_redis, require_session
from buma.gateway.services import observability_queries as queries

# "Ask Buma" chat assistant (DD-27). The repo comes from the URL and is fixed for every tool the
# model calls; the model can never choose which repo it reads.

router = APIRouter(prefix="/api/chat", tags=["chat"])

MAX_QUESTION_CHARS = 2000
MAX_TURN_CHARS = 8000
MAX_HISTORY_TURNS = 20


class ChatTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=MAX_TURN_CHARS)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=MAX_QUESTION_CHARS)
    # Prior plain-text turns, oldest first. The server keeps no conversation state.
    history: list[ChatTurn] = Field(default_factory=list, max_length=MAX_HISTORY_TURNS)


class ChatStatus(BaseModel):
    enabled: bool
    model: str | None


def get_chat_service(settings: Annotated[Settings, Depends(get_settings)]) -> ChatService | None:
    return ChatService.from_settings(settings) if chat_enabled(settings) else None


@router.get("/status", response_model=ChatStatus)
async def chat_status(
    settings: Annotated[Settings, Depends(get_settings)],
    _session: Annotated[str, Depends(require_session)],
) -> ChatStatus:
    enabled = chat_enabled(settings)
    return ChatStatus(enabled=enabled, model=settings.chat_model if enabled else None)


@router.post("/{repo_id}")
async def chat(
    repo_id: int,
    body: ChatRequest,
    settings: Annotated[Settings, Depends(get_settings)],
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[aioredis.Redis, Depends(get_redis)],
    service: Annotated[ChatService | None, Depends(get_chat_service)],
    _session: Annotated[str, Depends(require_session)],
) -> StreamingResponse:
    """
    Ask a question about one repo's Buma data. Responds with Server-Sent Events, one JSON object
    per `data:` line (see buma.gateway.chat.agent for the event types); the last event is `done`.
    """
    if service is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="The chat assistant is disabled.")

    repo = await queries.get_repo(db, repo_id)
    if repo is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Repo {repo_id} not found.")

    # Checked before streaming starts so an exhausted budget is a real 429, not a mid-stream event.
    if not await chat_budget(redis, settings).allow(repo_id):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="The chat assistant is paused for this repository (daily limit reached or temporarily unavailable).",
        )

    history = [turn.model_dump() for turn in body.history]
    events = service.stream(repo_id, repo.repo_full_name, body.message, history)
    return StreamingResponse(
        _sse(events),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _sse(events: AsyncIterator[dict]) -> AsyncIterator[str]:
    async for event in events:
        yield f"data: {json.dumps(event)}\n\n"
