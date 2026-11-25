"""Auto-answer Pomerado needs_input from known mint inputs. Never fills secrets."""

from __future__ import annotations

from typing import Any

_PROTECTED = frozenset({"secret", "credential", "browser_handoff"})
_WRITE_HINTS = (
    "purchase",
    "pay",
    "checkout",
    "password",
    "sign in",
    "sign-in",
    "login",
    "credit card",
    "submit the form",
    "commit",
    "delete",
    "place the order",
)


def _folded(text: str) -> str:
    return " ".join(str(text or "").strip().lower().split())


def _match_known(question: dict[str, Any], known: dict[str, Any]) -> str | None:
    if not known:
        return None
    qid = _folded(question.get("id") or "")
    prompt = _folded(question.get("prompt") or "")
    for key, value in known.items():
        if value is None or value == "":
            continue
        k = _folded(str(key))
        if not k:
            continue
        if qid == k or k in qid or qid in k:
            return str(value)
        if k in prompt or (len(k) >= 3 and k.replace("_", " ") in prompt):
            return str(value)
    return None


def _looks_like_write(request: dict[str, Any], question: dict[str, Any]) -> bool:
    blob = _folded(" ".join(
        [
            str(request.get("notice") or ""),
            str(question.get("prompt") or ""),
            str(request.get("source") or ""),
        ]
    ))
    if request.get("order"):
        return True
    return any(hint in blob for hint in _WRITE_HINTS)


def auto_answers_for_request(request: dict[str, Any] | None, known: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return a complete answer map, or None if a human must answer.

    Never auto-fills secret, credential, or browser_handoff questions.
    Text/choice questions are filled from `known` mint inputs (term, name, url, …)
    or from a recommended option. Confirmations are only auto-accepted when they
    are clearly a continue/read step, not a write or login.
    """
    if not isinstance(request, dict):
        return None
    questions = request.get("questions") or []
    if not questions:
        return None
    known = {str(k): v for k, v in (known or {}).items()}
    answers: dict[str, Any] = {}
    for question in questions:
        typ = question.get("type")
        qid = question.get("id")
        if not qid:
            return None
        if typ in _PROTECTED:
            return None
        if typ == "text":
            value = _match_known(question, known) or question.get("recommended")
            if not value:
                return None
            answers[qid] = str(value)
        elif typ == "choice":
            recommended = question.get("recommended")
            if recommended:
                answers[qid] = recommended
                continue
            options = question.get("options") or []
            matched = _match_known(question, known)
            if matched:
                folded = _folded(matched)
                hit = next(
                    (
                        opt
                        for opt in options
                        if _folded(opt.get("id") or "") == folded or _folded(opt.get("label") or "") == folded
                    ),
                    None,
                )
                if hit:
                    answers[qid] = hit["id"]
                    continue
                if question.get("allowOther"):
                    answers[qid] = {"other": str(matched)}
                    continue
            return None
        elif typ == "multi_choice":
            recommended = question.get("recommended")
            if recommended:
                answers[qid] = recommended
                continue
            return None
        elif typ == "confirm":
            if _looks_like_write(request, question):
                return None
            prompt = _folded(question.get("prompt") or "")
            notice = _folded(request.get("notice") or "")
            # Notice "did you do it?" needs a human.
            if request.get("notice") and qid == "done":
                return None
            if any(word in prompt or word in notice for word in ("continue", "proceed", "read-only", "read only", "looks good", "confirm the term", "use this")):
                answers[qid] = {"confirmed": True}
                continue
            return None
        else:
            return None
    return answers or None
