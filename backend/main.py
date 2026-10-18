"""Cartners shared FastAPI backend (Xavier) + local AI preview routes (Ethan)."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Any
from uuid import uuid4

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from app.db import DEFAULT_DB_PATH, connect, loads
from app.errors import ApiError
from app.schemas import (
    AddItemRequest,
    BasketCompareRequest,
    ConfirmDraftRequest,
    ExpenseRequest,
    PatchItemRequest,
    PreferencesPatch,
    SuggestionRequest as ContractSuggestionRequest,
    TripPatchRequest,
    UndoRequest,
)
from app.services.gemini_suggestions import Preferences, generate_suggestion, suggest_for_event
from app.store import Store
from app.suggestions import build_suggestion, require_message

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

DEFAULT_ORIGINS = [
    "http://127.0.0.1:5173",
    "http://localhost:5173",
    "http://127.0.0.1:4173",
    "http://localhost:4173",
]


def _cors_origins() -> list[str]:
    raw = os.getenv("CORS_ORIGINS", "").strip()
    if not raw:
        return DEFAULT_ORIGINS
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


# --- Ethan AI preview models (module-level for FastAPI + unit tests) ---


class SavedSuggestionInput(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    planDate: date | None = None
    summary: str = Field(default="", max_length=240)


class EventSuggestionRequest(BaseModel):
    workspaceId: str = Field(min_length=1, max_length=80)
    requestId: str | None = None
    name: str = Field(min_length=1, max_length=80)
    planDate: date
    time: str
    theme: str = Field(default="", max_length=80)
    guestCount: int | None = Field(default=None, ge=1, le=12)
    notes: str = Field(default="", max_length=300)
    preferences: Preferences
    savedSuggestions: list[SavedSuggestionInput] = Field(default_factory=list, max_length=8)


class SuggestionRequest(BaseModel):
    """AI preview request for /api/ai/suggestions (Ethan tests import this name)."""

    workspaceId: str = Field(min_length=1, max_length=80)
    requestId: str | None = None
    message: str = Field(min_length=1, max_length=500)
    planDate: date
    listRevision: int = Field(ge=0)
    preferences: Preferences
    previousDraft: dict | None = None


def make_preview_draft(result: dict, request: SuggestionRequest) -> dict:
    draft = {
        "draftId": f"ai-{uuid4()}",
        "previousDraftId": request.previousDraft.get("draftId") if request.previousDraft else None,
        "workspaceId": request.workspaceId,
        "planDate": request.planDate.isoformat(),
        "listRevision": request.listRevision,
        "sourceMode": "live_ai",
        "sampleNotice": "AI-generated idea; the cart is still sample data and this preview is not saved.",
        **result,
    }
    if draft.get("kind") == "recommendation":
        ingredients = draft["recommendation"]["ingredients"]
        for ingredient in ingredients:
            ingredient["id"] = f"ingredient-{uuid4()}"
        draft["recommendation"]["listPreview"] = [
            {
                "ingredientId": item["id"],
                "action": "review",
                "reason": "Connect the shared cart to check for duplicates.",
            }
            for item in ingredients
        ]
    return draft


def make_event_preview(result: dict) -> dict:
    preview = {
        **result,
        "sourceMode": "live_ai",
        "sampleNotice": "AI meal ideas for this event. Nothing is saved and the cart is unchanged.",
    }
    for option in preview.get("options") or []:
        for ingredient in option.get("ingredients") or []:
            ingredient["id"] = f"ingredient-{uuid4()}"
    return preview


def create_app(db_path: Path | str | None = None) -> FastAPI:
    """Build the API app. Tests pass a temp SQLite path."""

    path = Path(db_path) if db_path is not None else Path(os.getenv("CARTNERS_DB_PATH") or DEFAULT_DB_PATH)

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        conn = connect(path)
        store = Store(conn)
        store.ensure_seeded()
        application.state.db = conn
        application.state.store = store
        try:
            yield
        finally:
            conn.close()

    application = FastAPI(title="Cartners API", version="2.0.0", lifespan=lifespan)
    application.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins(),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @application.exception_handler(ApiError)
    async def api_error_handler(_request: Request, exc: ApiError) -> JSONResponse:
        return exc.to_response()

    def store_of(request: Request) -> Store:
        return request.app.state.store

    @application.get("/api/health")
    def health() -> dict[str, Any]:
        return {
            "ok": True,
            "service": "cartners",
            "schemaVersion": 2,
            "aiAvailable": bool(os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")),
            "krogerConfigured": bool(os.getenv("KROGER_CLIENT_ID") and os.getenv("KROGER_CLIENT_SECRET")),
        }

    @application.get("/api/workspace")
    def workspace(workspaceId: str, request: Request) -> dict[str, Any]:
        return store_of(request).snapshot(workspaceId)

    @application.patch("/api/preferences")
    def preferences(body: PreferencesPatch, request: Request) -> dict[str, Any]:
        return store_of(request).patch_preferences(body.model_dump())

    @application.post("/api/suggestions")
    async def suggestions(body: ContractSuggestionRequest, request: Request) -> dict[str, Any]:
        store = store_of(request)
        require_message(body.message)
        ws = store.require_workspace(body.workspaceId)
        store.require_actor(body.workspaceId, body.actorId)
        previous = None
        if body.previousDraftId:
            previous = store.get_draft(body.previousDraftId, body.workspaceId)
        prefs = {
            "servings": ws["servings"],
            "restrictions": loads(ws["restrictions_json"], []),
            "likes": loads(ws["likes_json"], []),
            "maxPrepMinutes": ws["max_prep_minutes"],
        }
        draft = await run_in_threadpool(
            build_suggestion,
            workspace_id=body.workspaceId,
            message=body.message,
            plan_date=body.planDate,
            list_revision=ws["list_revision"],
            preferences=prefs,
            previous_draft=previous,
            previous_draft_id=body.previousDraftId,
        )
        items = store._load_items(body.workspaceId)
        draft = store.refresh_draft_preview(draft, items, ws["list_revision"])
        store.save_draft(draft)
        return draft

    @application.get("/api/suggestions/{draftId}")
    def get_suggestion(draftId: str, workspaceId: str, request: Request) -> dict[str, Any]:
        return store_of(request).get_draft(draftId, workspaceId)

    @application.post("/api/drafts/confirm")
    def confirm_draft(body: ConfirmDraftRequest, request: Request) -> dict[str, Any]:
        return store_of(request).confirm_draft(body.model_dump())

    @application.post("/api/actions/{actionId}/undo")
    def undo_action(actionId: str, body: UndoRequest, request: Request) -> dict[str, Any]:
        return store_of(request).undo_action(actionId, body.model_dump())

    @application.post("/api/items")
    def add_item(body: AddItemRequest, request: Request) -> dict[str, Any]:
        return store_of(request).add_item(body.model_dump())

    @application.patch("/api/items/{itemId}")
    def patch_item(itemId: str, body: PatchItemRequest, request: Request) -> dict[str, Any]:
        return store_of(request).patch_item(itemId, body.model_dump())

    @application.patch("/api/trip")
    def patch_trip(body: TripPatchRequest, request: Request) -> dict[str, Any]:
        return store_of(request).patch_trip(body.model_dump())

    @application.post("/api/expenses")
    def create_expense(body: ExpenseRequest, request: Request) -> dict[str, Any]:
        return store_of(request).create_expense(body.model_dump())

    @application.post("/api/baskets/compare")
    def compare_baskets(body: BasketCompareRequest, request: Request) -> dict[str, Any]:
        return store_of(request).compare_baskets(body.model_dump())

    @application.get("/api/ingredients")
    def list_ingredients(request: Request) -> dict[str, Any]:
        """Kroger-shaped catalog: id, name, description, price."""
        store = store_of(request)
        rows = store.conn.execute(
            """
            SELECT id, name, description, price_cents, currency,
                   package_quantity, package_unit, store_name, source_mode
            FROM ingredients ORDER BY name
            """
        ).fetchall()
        return {
            "ingredients": [
                {
                    "id": row["id"],
                    "name": row["name"],
                    "description": row["description"],
                    "priceCents": row["price_cents"],
                    "currency": row["currency"],
                    "packageQuantity": row["package_quantity"],
                    "packageUnit": row["package_unit"],
                    "storeName": row["store_name"],
                    "sourceMode": row["source_mode"],
                }
                for row in rows
            ]
        }

    @application.get("/api/ai/health")
    def ai_health() -> dict[str, bool]:
        return {"available": bool(os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"))}

    @application.post("/api/ai/event-suggestions")
    async def ai_event_suggestions(body: EventSuggestionRequest) -> dict:
        try:
            result = await run_in_threadpool(
                suggest_for_event,
                {
                    "name": body.name,
                    "planDate": body.planDate,
                    "time": body.time,
                    "theme": body.theme,
                    "guestCount": body.guestCount,
                    "notes": body.notes,
                },
                preferences=body.preferences,
                saved_suggestions=[item.model_dump() for item in body.savedSuggestions],
            )
            return make_event_preview(result)
        except RuntimeError as exc:
            message = str(exc)
            if "Set GEMINI_API_KEY" in message:
                message = "Set GEMINI_API_KEY in backend/.env, then restart the backend."
            return JSONResponse(
                status_code=503,
                content={
                    "error": {
                        "code": "AI_UNAVAILABLE",
                        "message": message,
                        "retryable": True,
                        "requestId": body.requestId,
                    }
                },
            )
        except ValueError as exc:
            return JSONResponse(
                status_code=422,
                content={
                    "error": {
                        "code": "INVALID_EVENT_REQUEST",
                        "message": str(exc),
                        "retryable": False,
                        "requestId": body.requestId,
                    }
                },
            )

    @application.post("/api/ai/suggestions")
    async def ai_suggestion_preview(body: SuggestionRequest) -> dict:
        try:
            result = await run_in_threadpool(
                generate_suggestion,
                body.message,
                preferences=body.preferences,
                previous_draft=body.previousDraft,
            )
            return make_preview_draft(result, body)
        except RuntimeError as exc:
            message = str(exc)
            if "Set GEMINI_API_KEY" in message:
                message = "Set GEMINI_API_KEY in backend/.env, then restart the backend."
            return JSONResponse(
                status_code=503,
                content={
                    "error": {
                        "code": "AI_UNAVAILABLE",
                        "message": message,
                        "retryable": True,
                        "requestId": body.requestId,
                    }
                },
            )
        except ValueError as exc:
            return JSONResponse(
                status_code=422,
                content={
                    "error": {
                        "code": "INVALID_SUGGESTION_REQUEST",
                        "message": str(exc),
                        "retryable": False,
                        "requestId": body.requestId,
                    }
                },
            )

    return application


app = create_app()
