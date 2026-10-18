"""Shared request models for contract v2 routes."""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field


class WriteBase(BaseModel):
    workspaceId: str = Field(min_length=1, max_length=80)
    actorId: str = Field(min_length=1, max_length=80)
    requestId: str = Field(min_length=1, max_length=80)


class PreferencesPatch(WriteBase):
    servings: int | None = Field(default=None, ge=1, le=12)
    restrictions: list[str] | None = None
    likes: list[str] | None = None
    maxPrepMinutes: int | None = Field(default=None, ge=1, le=240)
    spendingMode: Literal["pooled", "split"] | None = None


class SuggestionRequest(WriteBase):
    message: str = Field(min_length=1, max_length=500)
    previousDraftId: str | None = None
    planDate: date | None = None


class ConfirmDraftRequest(WriteBase):
    draftId: str
    expectedListRevision: int = Field(ge=0)
    excludedIngredientIds: list[str] = Field(default_factory=list)
    target: Literal["cart", "plans"] = "cart"


class UndoRequest(WriteBase):
    pass


class AddItemRequest(WriteBase):
    name: str = Field(min_length=1, max_length=120)
    quantity: float = Field(default=1, gt=0)
    unit: Literal["count", "g", "ml", "can", "pack"] = "count"
    note: str = Field(default="", max_length=200)
    requestFor: str = "household"


class PatchItemRequest(WriteBase):
    expectedItemVersion: int = Field(ge=0)
    name: str | None = Field(default=None, min_length=1, max_length=120)
    quantity: float | None = Field(default=None, gt=0)
    unit: Literal["count", "g", "ml", "can", "pack"] | None = None
    note: str | None = Field(default=None, max_length=200)
    requestFor: str | None = None
    bought: bool | None = None
    removed: bool | None = None


class TripPatchRequest(WriteBase):
    expectedTripVersion: int = Field(ge=0)
    action: Literal["start", "release", "finish"]
    storeName: str | None = Field(default=None, max_length=120)
    requestWindowMinutes: Literal[5, 10, 15] | None = 10


class ExpenseRequest(WriteBase):
    runId: str | None = None
    totalCents: int = Field(ge=0)
    payerId: str
    mode: Literal["pooled", "split"]
    sharedMemberIds: list[str] = Field(default_factory=list)
    personalAllocations: dict[str, int | None] = Field(default_factory=dict)
    status: Literal["confirmed", "needs_review"] = "confirmed"


class BasketCompareRequest(WriteBase):
    itemIds: list[str] = Field(min_length=1)
    postalCode: str = Field(min_length=5, max_length=12)


# --- Domain shapes documented for teammates ---

class RecipeIngredient(BaseModel):
    id: str
    name: str
    quantity: float
    unit: str
    note: str = ""


class Recipe(BaseModel):
    """Main Search recipe: ingredients + steps."""

    title: str
    servings: int
    prepMinutesApprox: int | None = None
    fitReason: str | None = None
    ingredients: list[RecipeIngredient]
    steps: list[str]
    uncertainties: list[str] = Field(default_factory=list)
    listPreview: list[dict[str, Any]] = Field(default_factory=list)


class PlanRecord(BaseModel):
    """Plans page: Event, Date, Plan (recipe body)."""

    id: str
    event: str
    date: str
    planDate: str
    title: str
    servings: int | None = None
    steps: list[str] = Field(default_factory=list)
    recommendation: dict[str, Any] | None = None


class KrogerIngredient(BaseModel):
    """Priced product option used by basket comparison."""

    id: str
    name: str
    description: str = ""
    priceCents: int | None = None
    currency: str = "USD"
