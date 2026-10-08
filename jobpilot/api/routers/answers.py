"""Answer bank: screening answers the apply agent uses before asking Gemini."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from jobpilot.api.deps import get_or_404, get_session
from jobpilot.db.models import Answer, utcnow

router = APIRouter(tags=["answers"])


class AnswerIn(BaseModel):
    question: str = Field(min_length=2)
    answer: str = Field(min_length=1)
    tags: str = ""


class AnswerPatch(BaseModel):
    question: Optional[str] = Field(None, min_length=2)
    answer: Optional[str] = None  # "" turns it back into an open question
    tags: Optional[str] = None


@router.get("/answers")
def list_answers(session: Session = Depends(get_session)) -> list[Answer]:
    return list(session.exec(select(Answer).order_by(Answer.times_used.desc(), Answer.question)))


@router.post("/answers", status_code=201)
def create_answer(body: AnswerIn, session: Session = Depends(get_session)) -> Answer:
    answer = Answer(**body.model_dump())
    session.add(answer)
    session.commit()
    session.refresh(answer)
    return answer


@router.put("/answers/{answer_id}")
def update_answer(answer_id: int, body: AnswerPatch, session: Session = Depends(get_session)) -> Answer:
    answer = get_or_404(session, Answer, answer_id)
    for key, value in body.model_dump(exclude_none=True).items():
        setattr(answer, key, value.strip() if isinstance(value, str) else value)
    if body.answer is not None:  # keep the needs-answer tag in step with whether there is an answer
        tags = [t.strip() for t in answer.tags.split(",") if t.strip() and t.strip() != "needs-answer"]
        answer.tags = ", ".join(tags + ([] if answer.answer else ["needs-answer"]))
    answer.updated_at = utcnow()
    session.add(answer)
    session.commit()
    session.refresh(answer)
    return answer


@router.delete("/answers/{answer_id}", status_code=204)
def delete_answer(answer_id: int, session: Session = Depends(get_session)) -> None:
    session.delete(get_or_404(session, Answer, answer_id))
    session.commit()
