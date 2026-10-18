"""Focused checks for Xavier's shared backend foundation."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.money import allocate_split, balances_from_expenses


@pytest.fixture()
def client(tmp_path: Path):
    app = create_app(tmp_path / "test.db")
    with TestClient(app) as test_client:
        yield test_client


def rid() -> str:
    return str(uuid.uuid4())


def test_health_and_two_group_snapshots(client: TestClient):
    health = client.get("/api/health").json()
    assert health["ok"] is True
    assert health["schemaVersion"] == 2

    family = client.get("/api/workspace", params={"workspaceId": "demo-family"}).json()
    roommates = client.get("/api/workspace", params={"workspaceId": "demo-roommates"}).json()
    assert family["schemaVersion"] == 2
    assert family["workspaceId"] == "demo-family"
    assert roommates["workspaceId"] == "demo-roommates"
    assert family["preferences"]["spendingMode"] == "pooled"
    assert roommates["preferences"]["spendingMode"] == "split"
    assert {g["workspaceId"] for g in family["groups"]} == {"demo-family", "demo-roommates"}

    # Seeded Movie Night plan: Event / Date / Plan
    plans = family["plans"]
    assert any(p["event"] == "Movie Night" and p["date"] == "2026-10-17" for p in plans)
    movie = next(p for p in plans if p["event"] == "Movie Night")
    assert movie["plan"]["ingredients"]
    assert movie["plan"]["steps"]


def test_group_isolation_on_add_item(client: TestClient):
    body = {
        "workspaceId": "demo-family",
        "actorId": "leila",
        "requestId": rid(),
        "name": "Paper plates",
    }
    result = client.post("/api/items", json=body).json()
    assert result["duplicate"] is False
    family_ids = {item["name"] for item in result["workspace"]["items"] if not item["removed"]}
    assert "Paper plates" in family_ids

    roommates = client.get("/api/workspace", params={"workspaceId": "demo-roommates"}).json()
    assert "Paper plates" not in {item["name"] for item in roommates["items"]}


def test_add_item_replay_safe(client: TestClient):
    request_id = rid()
    body = {
        "workspaceId": "demo-family",
        "actorId": "sam",
        "requestId": request_id,
        "name": "Sparkling water",
    }
    first = client.post("/api/items", json=body).json()
    second = client.post("/api/items", json=body).json()
    assert first["actionId"] == second["actionId"]
    assert first["itemId"] == second["itemId"]
    names = [item["name"] for item in second["workspace"]["items"] if not item["removed"]]
    assert names.count("Sparkling water") == 1


def test_meal_confirm_saves_recipe_plan_and_cart(client: TestClient):
    suggestion = client.post(
        "/api/suggestions",
        json={
            "workspaceId": "demo-family",
            "actorId": "leila",
            "requestId": rid(),
            "message": "Something fun for dinner",
            "planDate": "2026-10-18",
        },
    ).json()
    assert suggestion["kind"] == "recommendation"
    assert suggestion["recommendation"]["ingredients"]
    assert suggestion["recommendation"]["steps"]

    confirm = client.post(
        "/api/drafts/confirm",
        json={
            "workspaceId": "demo-family",
            "actorId": "leila",
            "requestId": rid(),
            "draftId": suggestion["draftId"],
            "expectedListRevision": suggestion["listRevision"],
            "excludedIngredientIds": [],
            "target": "cart",
        },
    ).json()
    workspace = confirm["workspace"]
    assert any(p["date"] == "2026-10-18" for p in workspace["plans"])
    # Tortillas should increase 6 -> 8
    tortillas = next(i for i in workspace["items"] if i["id"] == "item-tortillas")
    assert tortillas["quantity"] == 8

    # Stale expected revision must 409 without another AI call
    stale = client.post(
        "/api/drafts/confirm",
        json={
            "workspaceId": "demo-family",
            "actorId": "leila",
            "requestId": rid(),
            "draftId": suggestion["draftId"],
            "expectedListRevision": suggestion["listRevision"],
            "target": "cart",
        },
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "LIST_CHANGED"


def test_confirm_replay_same_request_id(client: TestClient):
    suggestion = client.post(
        "/api/suggestions",
        json={
            "workspaceId": "demo-roommates",
            "actorId": "leila",
            "requestId": rid(),
            "message": "Milk, eggs, and paper plates",
            "planDate": "2026-10-18",
        },
    ).json()
    request_id = rid()
    body = {
        "workspaceId": "demo-roommates",
        "actorId": "leila",
        "requestId": request_id,
        "draftId": suggestion["draftId"],
        "expectedListRevision": suggestion["listRevision"],
        "target": "cart",
    }
    first = client.post("/api/drafts/confirm", json=body).json()
    second = client.post("/api/drafts/confirm", json=body).json()
    assert first["actionId"] == second["actionId"]
    paper = [i for i in second["workspace"]["items"] if i["name"] == "Paper plates" and not i["removed"]]
    assert len(paper) == 1


def test_concurrent_trip_claim(client: TestClient):
    # Clear seeded family run first by finishing as sam
    family = client.get("/api/workspace", params={"workspaceId": "demo-family"}).json()
    trip = family["trip"]
    assert trip["claimedBy"] == "sam"
    finish = client.patch(
        "/api/trip",
        json={
            "workspaceId": "demo-family",
            "actorId": "sam",
            "requestId": rid(),
            "expectedTripVersion": trip["version"],
            "action": "finish",
        },
    ).json()
    version = finish["workspace"]["trip"]["version"]

    start_a = {
        "workspaceId": "demo-family",
        "actorId": "leila",
        "requestId": rid(),
        "expectedTripVersion": version,
        "action": "start",
        "storeName": "Kroger",
        "requestWindowMinutes": 10,
    }
    start_b = {
        "workspaceId": "demo-family",
        "actorId": "sam",
        "requestId": rid(),
        "expectedTripVersion": version,
        "action": "start",
        "storeName": "QFC",
        "requestWindowMinutes": 10,
    }
    first = client.patch("/api/trip", json=start_a)
    second = client.patch("/api/trip", json=start_b)
    assert first.status_code == 200
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "TRIP_CLAIMED"
    assert first.json()["workspace"]["trip"]["claimedBy"] == "leila"


def test_late_requests_after_deadline_unassigned(client: TestClient, monkeypatch):
    from app import store as store_mod

    family = client.get("/api/workspace", params={"workspaceId": "demo-family"}).json()
    # Seeded run deadline is in the past relative to "now" if we freeze time after deadline
    monkeypatch.setattr(store_mod, "utc_now", lambda: store_mod.datetime(2026, 10, 17, 18, 15, tzinfo=store_mod.timezone.utc))

    result = client.post(
        "/api/items",
        json={
            "workspaceId": "demo-family",
            "actorId": "leila",
            "requestId": rid(),
            "name": "Late bananas",
        },
    ).json()
    bananas = next(i for i in result["workspace"]["items"] if i["name"] == "Late bananas")
    assert bananas["runId"] is None
    snap = result["workspace"]["trip"]
    assert snap["state"] in {"closed_to_requests", "accepting_requests"}


def test_exact_cent_allocations():
    # 1001 among three adults
    alloc = allocate_split(1001, ["a", "b", "c"], {})
    assert sum(alloc.values()) == 1001
    assert sorted(alloc.values()) == [333, 334, 334]

    # 2000 with 600 personal for sam, two shared participants
    alloc = allocate_split(2000, ["leila", "sam"], {"sam": 600})
    assert alloc["sam"] == 1300
    assert alloc["leila"] == 700
    assert sum(alloc.values()) == 2000

    balances = balances_from_expenses(
        [
            {
                "status": "confirmed",
                "mode": "split",
                "payerId": "leila",
                "allocations": {"leila": 700, "sam": 1300},
            },
            {
                "status": "needs_review",
                "mode": "split",
                "payerId": "leila",
                "allocations": {},
            },
            {
                "status": "confirmed",
                "mode": "pooled",
                "payerId": "leila",
                "allocations": {"leila": 500},
            },
        ]
    )
    assert balances == [{"from": "sam", "to": "leila", "cents": 1300}]


def test_expense_endpoint_split(client: TestClient):
    result = client.post(
        "/api/expenses",
        json={
            "workspaceId": "demo-roommates",
            "actorId": "leila",
            "requestId": rid(),
            "totalCents": 2000,
            "payerId": "leila",
            "mode": "split",
            "sharedMemberIds": ["leila", "sam"],
            "personalAllocations": {"sam": 600},
            "status": "confirmed",
        },
    ).json()
    assert result["workspace"]["balances"] == [{"from": "sam", "to": "leila", "cents": 1300}]


def test_ingredients_catalog_shape(client: TestClient):
    payload = client.get("/api/ingredients").json()
    assert payload["ingredients"]
    sample = payload["ingredients"][0]
    assert {"id", "name", "description", "priceCents"} <= set(sample)


def test_basket_compare_sample(client: TestClient):
    result = client.post(
        "/api/baskets/compare",
        json={
            "workspaceId": "demo-family",
            "actorId": "leila",
            "requestId": rid(),
            "itemIds": ["item-tortillas", "item-milk"],
            "postalCode": "98105",
        },
    ).json()
    assert result["sourceMode"] in {"sample", "live_api"}
    complete = [b for b in result["baskets"] if b["complete"]]
    assert complete
    totals = [b["knownSubtotalCents"] for b in complete]
    assert totals == sorted(totals)
