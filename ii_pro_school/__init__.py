"""School v0.1: proposals and static checks, never execution of learner code."""
from .adapters import ModelProfile, OllamaStudentAdapter, StudentAdapter, StudentReply
from .school import School

__all__ = ["School", "ModelProfile", "StudentAdapter", "StudentReply", "OllamaStudentAdapter"]
