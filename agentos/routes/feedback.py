"""用户反馈 → Langfuse score。"""

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

from agentos import scoring

router = APIRouter()


class FeedbackBody(BaseModel):
    trace_id: str  # 该 run 的 OTEL trace id(32-hex),score 靠它关联到 trace
    value: float | str
    name: str = "user_feedback"
    data_type: Literal["NUMERIC", "CATEGORICAL", "BOOLEAN"] = "NUMERIC"
    comment: str | None = None


@router.post("/feedback", tags=["Feedback"])
def feedback(body: FeedbackBody) -> dict:
    """把用户反馈作为 Langfuse score 关联到指定 trace;缺 Langfuse 凭据则静默忽略。"""
    scoring.score(body.name, body.value, trace_id=body.trace_id, data_type=body.data_type, comment=body.comment)
    return {"ok": True}
