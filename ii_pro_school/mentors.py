"""Contracts only. No cloud clients or autonomous GPT sessions."""
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class MentorFeedback:
    role: str
    author: str
    text: str
    origin: str = "manual"
    evidence_ref: str | None = None


class ArchitectMentor(Protocol):
    def plan(self, curriculum: dict) -> dict: ...
    def review_exam(self, private_spec: dict) -> MentorFeedback: ...


class EngineerMentor(Protocol):
    def review(self, public_attempt_report: dict) -> MentorFeedback: ...
