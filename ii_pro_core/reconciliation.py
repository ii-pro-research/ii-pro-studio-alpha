"""Explicit domain verification, separate from observing that execution stopped.

Reconciler implementations are registered by trusted local application code,
never loaded from SQLite or provided by an agent. A receipt is a trusted
verifier's evidence assertion, not an exactly-once guarantee for external APIs.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass

from .models import EffectStatus, Run


@dataclass(frozen=True)
class ReconciliationReceipt:
    task_id: str
    run_id: str
    action_hash: str
    outcome: EffectStatus
    evidence_ref: str


class Reconciler(ABC):
    name: str

    @abstractmethod
    def reconcile(self, run: Run) -> ReconciliationReceipt:
        """Inspect authoritative domain evidence for this exact run/action."""
        raise NotImplementedError
