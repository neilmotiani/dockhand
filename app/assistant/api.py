"""POST /v1/assistant/turn: one turn of the simulated Alexa+ assistant (app/assistant/agent.py).

Admin token, like every dashboard endpoint: the dashboard server calls it, the browser never
does. Needs MCP_TOKEN (and MCP_URL if the MCP server isn't on localhost:8020) plus Bedrock
credentials; without MCP_TOKEN it answers 503 rather than failing at startup, so the rest of
the API runs without the assistant.
"""

import logging

import httpx2
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, model_validator

from app.assistant.agent import Approval, Assistant, TurnError, leaves, mcp_connector
from app.auth import require_admin
from app.ingest.queue import utcnow

log = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(require_admin)])


class ApprovalIn(BaseModel):
    tool_use_id: str = Field(min_length=1, max_length=200)
    approved: bool


class TurnIn(BaseModel):
    messages: list[dict] = Field(default_factory=list, max_length=100)
    text: str | None = Field(default=None, min_length=1, max_length=1000)
    approval: ApprovalIn | None = None

    @model_validator(mode="after")
    def _one_input(self) -> "TurnIn":
        if (self.text is None) == (self.approval is None):
            raise ValueError("send either text or an approval")
        return self


def _assistant(request: Request) -> Assistant:
    state = request.app.state
    if getattr(state, "assistant", None) is None:
        settings = state.settings
        if settings.mcp_token is None:
            raise HTTPException(503, "the assistant needs MCP_TOKEN (and MCP_URL) to be set")
        from app.extract.client import make_bedrock_client

        state.assistant = Assistant(
            make_bedrock_client(settings), mcp_connector(settings), settings, clock=utcnow
        )
    return state.assistant


@router.post("/v1/assistant/turn")
async def assistant_turn(request: Request, body: TurnIn) -> dict:
    assistant = _assistant(request)
    approval = Approval(**body.approval.model_dump()) if body.approval else None
    try:
        result = await assistant.turn(body.messages, text=body.text, approval=approval)
    except Exception as e:
        # Connection failures can still arrive as exception groups from the MCP client.
        errors = leaves(e)
        turn = [x for x in errors if isinstance(x, TurnError)]
        if turn:
            raise HTTPException(409, str(turn[0])) from None
        bedrock = [x for x in errors if isinstance(x, (ClientError, BotoCoreError))]
        if bedrock:
            log.warning("assistant: Bedrock call failed: %s", bedrock[0])
            raise HTTPException(502, f"Bedrock call failed: {bedrock[0]}") from None
        if any(isinstance(x, (OSError, httpx2.TransportError)) for x in errors):
            log.warning("assistant: MCP server unreachable: %r", errors)
            url = request.app.state.settings.mcp_url
            raise HTTPException(502, f"MCP server unreachable at {url}") from None
        raise
    return result.as_dict()
