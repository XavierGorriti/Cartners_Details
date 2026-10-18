"""Build meal/grocery drafts for POST /api/suggestions.

Uses Ethan's Gemini adapter when a key is present; otherwise returns labeled sample drafts.
"""

from __future__ import annotations

import copy
import json
import uuid
from datetime import date
from pathlib import Path
from typing import Any

from app.errors import ApiError

FIXTURES = Path(__file__).resolve().parents[2] / "docs" / "fixtures"


def _load_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text())


def _stamp(draft: dict[str, Any], *, workspace_id: str, plan_date: str, list_revision: int, previous: str | None) -> dict[str, Any]:
    draft = copy.deepcopy(draft)
    draft["draftId"] = f"draft-{uuid.uuid4().hex[:10]}"
    draft["workspaceId"] = workspace_id
    draft["planDate"] = plan_date
    draft["listRevision"] = list_revision
    draft["previousDraftId"] = previous
    if draft.get("kind") == "recommendation":
        for ingredient in draft["recommendation"]["ingredients"]:
            if not ingredient.get("id"):
                ingredient["id"] = f"ing-{uuid.uuid4().hex[:8]}"
    elif draft.get("kind") == "grocery_list":
        for item in draft.get("items") or []:
            if not item.get("id"):
                item["id"] = f"g-{uuid.uuid4().hex[:8]}"
    return draft


def sample_draft_for_message(message: str) -> dict[str, Any]:
    text = message.lower()
    if any(word in text for word in ("milk", "eggs", "grocery", "groceries", "buy", "paper plate")):
        return _load_fixture("groceries.json")
    if "quick" in text or "quicker" in text or "faster" in text:
        return _load_fixture("refinement.json")
    if "?" in text and any(word in text for word in ("or", "which", "what kind")):
        return _load_fixture("clarification.json")
    return _load_fixture("suggestions.json")


def build_suggestion(
    *,
    workspace_id: str,
    message: str,
    plan_date: date | None,
    list_revision: int,
    preferences: dict[str, Any],
    previous_draft: dict[str, Any] | None,
    previous_draft_id: str | None,
) -> dict[str, Any]:
    day = (plan_date or date.today()).isoformat()
    previous_id = previous_draft_id or (previous_draft or {}).get("draftId")

    # Prefer live AI when configured.
    try:
        from app.services.gemini_suggestions import Preferences, generate_suggestion

        prefs = Preferences(
            servings=int(preferences.get("servings") or 4),
            restrictions=list(preferences.get("restrictions") or []),
            likes=list(preferences.get("likes") or []),
            maxPrepMinutes=int(preferences.get("maxPrepMinutes") or 30),
        )
        result = generate_suggestion(message, preferences=prefs, previous_draft=previous_draft)
        draft = {
            "draftId": f"draft-{uuid.uuid4().hex[:10]}",
            "previousDraftId": previous_id,
            "workspaceId": workspace_id,
            "planDate": day,
            "listRevision": list_revision,
            "sourceMode": "live_ai",
            "sampleNotice": None,
            **result,
        }
        if draft.get("kind") == "recommendation":
            for ingredient in draft["recommendation"]["ingredients"]:
                ingredient.setdefault("id", f"ing-{uuid.uuid4().hex[:8]}")
                ingredient.setdefault("note", "")
        return draft
    except Exception as exc:
        # Missing key or provider failure → labeled sample, still useful for demos.
        if "Set GEMINI_API_KEY" not in str(exc) and "GOOGLE_API_KEY" not in str(exc):
            # Unexpected model/validation issues: still fall back, but keep message recoverable.
            pass

    sample = sample_draft_for_message(message)
    sample["sourceMode"] = "sample"
    sample["sampleNotice"] = "Curated contract fixture, not a live AI response."
    return _stamp(
        sample,
        workspace_id=workspace_id,
        plan_date=day,
        list_revision=list_revision,
        previous=previous_id,
    )


def require_message(message: str) -> None:
    if not message or not message.strip():
        raise ApiError(422, "INVALID_SUGGESTION_REQUEST", "Message is required.")
    if len(message) > 500:
        raise ApiError(422, "INVALID_SUGGESTION_REQUEST", "Message must be 500 characters or fewer.")
