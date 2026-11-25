"""Fuzzy-match tier: score page candidates and pause for human confirmation when unsure."""

from browser_platform.fuzzy.candidates import Candidate, apply_candidate, collect_candidates
from browser_platform.fuzzy.hitl import ConfirmationBroker, ConfirmationDecision, EventHub
from browser_platform.fuzzy.normalize import name_variants, normalize_text
from browser_platform.fuzzy.score import FuzzyDecision, score_candidates, score_text

__all__ = [
    "Candidate",
    "ConfirmationBroker",
    "ConfirmationDecision",
    "EventHub",
    "FuzzyDecision",
    "apply_candidate",
    "collect_candidates",
    "name_variants",
    "normalize_text",
    "score_candidates",
    "score_text",
]
