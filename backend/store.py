"""Workspace state, seeding, and mutation handlers."""

from __future__ import annotations

import copy
import sqlite3
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

from app.db import dumps, init_db, loads, transaction
from app.errors import ApiError
from app.list_ops import apply_preview_to_cart, build_list_preview, find_compatible, normalize_name
from app.money import allocate_split, balances_from_expenses

SUPPORTED_UNITS = {"count", "g", "ml", "can", "pack"}


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


class Store:
    def __init__(self, conn) -> None:
        self.conn = conn

    # ---- bootstrap ----

    def ensure_seeded(self) -> None:
        init_db(self.conn)
        row = self.conn.execute("SELECT value FROM meta WHERE key = 'seeded'").fetchone()
        if row:
            return
        with transaction(self.conn):
            self._seed()
            self.conn.execute(
                "INSERT INTO meta(key, value) VALUES ('seeded', ?)",
                (iso(utc_now()),),
            )

    def _seed(self) -> None:
        actors = [("leila", "Leila"), ("sam", "Sam"), ("jordan", "Jordan"), ("maya", "Maya")]
        for actor_id, name in actors:
            self.conn.execute("INSERT INTO actors(id, name) VALUES (?, ?)", (actor_id, name))

        groups = [
            {
                "id": "demo-family",
                "name": "Family dinner",
                "mode": "pooled",
                "servings": 4,
                "restrictions": ["vegetarian"],
                "likes": ["Mexican food"],
                "max_prep": 30,
                "members": ["leila", "sam"],
            },
            {
                "id": "demo-roommates",
                "name": "Roommate kitchen",
                "mode": "split",
                "servings": 2,
                "restrictions": [],
                "likes": ["pasta", "tacos"],
                "max_prep": 25,
                "members": ["leila", "sam", "jordan", "maya"],
            },
        ]
        for group in groups:
            self.conn.execute(
                """
                INSERT INTO workspaces(
                  id, name, spending_mode, servings, restrictions_json, likes_json,
                  max_prep_minutes, list_revision, last_store_name
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
                """,
                (
                    group["id"],
                    group["name"],
                    group["mode"],
                    group["servings"],
                    dumps(group["restrictions"]),
                    dumps(group["likes"]),
                    group["max_prep"],
                    "Sample neighborhood store",
                ),
            )
            for actor_id in group["members"]:
                self.conn.execute(
                    "INSERT INTO workspace_actors(workspace_id, actor_id) VALUES (?, ?)",
                    (group["id"], actor_id),
                )
            self.conn.execute(
                """
                INSERT INTO trips(
                  id, workspace_id, version, claimed_by, store_name, claimed_at,
                  request_deadline_at, state, claimed_item_ids_json, completed_trips_json
                ) VALUES (?, ?, 0, NULL, NULL, NULL, NULL, 'idle', '[]', '[]')
                """,
                (f"trip-{group['id']}", group["id"]),
            )

        now = "2026-10-17T17:59:00Z"
        family_items = [
            ("item-tortillas", "Tortillas", 6, "count", "", "household", "leila", "run-sample-1"),
            ("item-milk", "Oat milk", 1, "pack", "Unsweetened", "sam", "sam", "run-sample-1"),
        ]
        for item_id, name, qty, unit, note, request_for, added_by, run_id in family_items:
            self.conn.execute(
                """
                INSERT INTO items(
                  id, workspace_id, name, quantity, unit, note, request_for, added_by,
                  bought, version, created_at, removed, run_id, reserved_plan_id
                ) VALUES (?, 'demo-family', ?, ?, ?, ?, ?, ?, 0, 1, ?, 0, ?, NULL)
                """,
                (item_id, name, qty, unit, note, request_for, added_by, now, run_id),
            )

        # Active sample run on family workspace
        self.conn.execute(
            """
            UPDATE trips SET
              id = 'run-sample-1',
              version = 1,
              claimed_by = 'sam',
              store_name = 'Sample neighborhood store',
              claimed_at = '2026-10-17T18:00:00Z',
              request_deadline_at = '2026-10-17T18:10:00Z',
              state = 'accepting_requests',
              claimed_item_ids_json = ?
            WHERE workspace_id = 'demo-family'
            """,
            (dumps(["item-tortillas", "item-milk"]),),
        )

        # Plans: Event / Date / Plan — Movie Night on 2026-10-17
        movie_recipe = {
            "title": "Movie Night snack board",
            "servings": 4,
            "prepMinutesApprox": 20,
            "fitReason": "Shareable snacks that fit a vegetarian household.",
            "ingredients": [
                {"id": "ing-popcorn", "name": "Popcorn kernels", "quantity": 1, "unit": "pack", "note": ""},
                {"id": "ing-salsa", "name": "Salsa", "quantity": 1, "unit": "pack", "note": ""},
                {"id": "ing-chips", "name": "Tortilla chips", "quantity": 1, "unit": "pack", "note": ""},
            ],
            "steps": [
                "Pop the corn and season lightly.",
                "Warm the salsa and open the chips.",
                "Set everything out before the movie starts.",
            ],
            "uncertainties": [],
        }
        self.conn.execute(
            """
            INSERT INTO plans(id, workspace_id, event, plan_date, servings, recipe_json, action_id, created_at)
            VALUES (?, 'demo-family', ?, '2026-10-17', 4, ?, NULL, ?)
            """,
            ("plan-movie-night", "Movie Night", dumps(movie_recipe), now),
        )
        self.conn.execute(
            """
            INSERT INTO activity(id, workspace_id, created_at, actor_id, message)
            VALUES (?, 'demo-family', ?, 'leila', ?)
            """,
            ("act-seed-1", now, "Leila saved Movie Night for Oct 17."),
        )

        # Kroger-shaped ingredient catalog (sample prices)
        catalog = [
            ("krog-tortillas", "Kroger Soft Flour Tortillas", "8-count package", 250, 8, "count"),
            ("krog-milk", "Kroger Unsweetened Oat Milk", "64 fl oz carton", 425, 1, "pack"),
            ("krog-beans", "Kroger Black Beans", "15 oz can", 89, 1, "can"),
            ("krog-popcorn", "Kroger Popcorn Kernels", "32 oz bag", 299, 1, "pack"),
            ("krog-salsa", "Kroger Mild Salsa", "16 oz jar", 249, 1, "pack"),
            ("krog-chips", "Kroger Tortilla Chips", "13 oz bag", 279, 1, "pack"),
        ]
        for item_id, name, description, price, pkg_qty, pkg_unit in catalog:
            self.conn.execute(
                """
                INSERT INTO ingredients(
                  id, name, description, price_cents, currency, package_quantity, package_unit,
                  store_name, source_mode
                ) VALUES (?, ?, ?, ?, 'USD', ?, ?, 'Sample Kroger', 'sample')
                """,
                (item_id, name, description, price, pkg_qty, pkg_unit),
            )

    # ---- reads ----

    def require_workspace(self, workspace_id: str) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM workspaces WHERE id = ?", (workspace_id,)).fetchone()
        if not row:
            raise ApiError(404, "WORKSPACE_NOT_FOUND", f"Unknown workspace '{workspace_id}'.")
        return row

    def require_actor(self, workspace_id: str, actor_id: str) -> None:
        row = self.conn.execute(
            "SELECT 1 FROM workspace_actors WHERE workspace_id = ? AND actor_id = ?",
            (workspace_id, actor_id),
        ).fetchone()
        if not row:
            raise ApiError(400, "UNKNOWN_ACTOR", f"Actor '{actor_id}' is not in this group.")

    def snapshot(self, workspace_id: str) -> dict[str, Any]:
        ws = self.require_workspace(workspace_id)
        server_now = iso(utc_now())

        groups = []
        for row in self.conn.execute("SELECT * FROM workspaces ORDER BY id"):
            members = [
                r["actor_id"]
                for r in self.conn.execute(
                    "SELECT actor_id FROM workspace_actors WHERE workspace_id = ? ORDER BY actor_id",
                    (row["id"],),
                )
            ]
            groups.append(
                {
                    "workspaceId": row["id"],
                    "name": row["name"],
                    "memberIds": members,
                    "spendingMode": row["spending_mode"],
                }
            )

        member_ids = [
            r["actor_id"]
            for r in self.conn.execute(
                "SELECT actor_id FROM workspace_actors WHERE workspace_id = ? ORDER BY actor_id",
                (workspace_id,),
            )
        ]
        actors = []
        for actor_id in member_ids:
            actor = self.conn.execute("SELECT * FROM actors WHERE id = ?", (actor_id,)).fetchone()
            actors.append({"id": actor["id"], "name": actor["name"]})

        items = [self._item_dict(row) for row in self.conn.execute(
            "SELECT * FROM items WHERE workspace_id = ? ORDER BY created_at, id",
            (workspace_id,),
        )]

        plans = []
        for row in self.conn.execute(
            "SELECT * FROM plans WHERE workspace_id = ? ORDER BY plan_date, created_at",
            (workspace_id,),
        ):
            recipe = loads(row["recipe_json"], {})
            plans.append(
                {
                    "id": row["id"],
                    "event": row["event"],
                    "date": row["plan_date"],
                    "planDate": row["plan_date"],
                    "title": row["event"],
                    "servings": row["servings"],
                    "steps": recipe.get("steps") or [],
                    "recommendation": recipe,
                    "plan": recipe,
                }
            )

        trip_row = self.conn.execute(
            "SELECT * FROM trips WHERE workspace_id = ?",
            (workspace_id,),
        ).fetchone()
        trip = self._trip_dict(trip_row, server_now) if trip_row else None

        expenses = []
        for row in self.conn.execute(
            "SELECT * FROM expenses WHERE workspace_id = ? ORDER BY created_at",
            (workspace_id,),
        ):
            expenses.append(
                {
                    "id": row["id"],
                    "runId": row["run_id"],
                    "totalCents": row["total_cents"],
                    "currency": row["currency"],
                    "payerId": row["payer_id"],
                    "mode": row["mode"],
                    "sharedMemberIds": loads(row["shared_member_ids_json"], []),
                    "personalAllocations": loads(row["personal_allocations_json"], {}),
                    "allocations": loads(row["allocations_json"], {}),
                    "status": row["status"],
                    "createdAt": row["created_at"],
                }
            )

        activity = [
            {
                "id": row["id"],
                "createdAt": row["created_at"],
                "actorId": row["actor_id"],
                "message": row["message"],
            }
            for row in self.conn.execute(
                "SELECT * FROM activity WHERE workspace_id = ? ORDER BY created_at DESC LIMIT 5",
                (workspace_id,),
            )
        ]

        return {
            "schemaVersion": 2,
            "workspaceId": workspace_id,
            "serverNow": server_now,
            "listRevision": ws["list_revision"],
            "preferences": {
                "servings": ws["servings"],
                "restrictions": loads(ws["restrictions_json"], []),
                "likes": loads(ws["likes_json"], []),
                "maxPrepMinutes": ws["max_prep_minutes"],
                "spendingMode": ws["spending_mode"],
            },
            "groups": groups,
            "actors": actors,
            "currentMeal": None,
            "discoveryDraftId": None,
            "plans": plans,
            "items": items,
            "trip": trip,
            "expenses": expenses,
            "balances": balances_from_expenses(expenses),
            "activity": activity,
        }

    def _item_dict(self, row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "name": row["name"],
            "quantity": row["quantity"],
            "unit": row["unit"],
            "note": row["note"],
            "requestFor": row["request_for"],
            "addedBy": row["added_by"],
            "bought": bool(row["bought"]),
            "version": row["version"],
            "createdAt": row["created_at"],
            "removed": bool(row["removed"]),
            "runId": row["run_id"],
            "reservedPlanId": row["reserved_plan_id"],
        }

    def _trip_dict(self, row, server_now: str) -> dict[str, Any]:
        state = row["state"]
        deadline = row["request_deadline_at"]
        if state == "accepting_requests" and deadline and deadline <= server_now:
            state = "closed_to_requests"
        return {
            "id": row["id"],
            "version": row["version"],
            "claimedBy": row["claimed_by"],
            "claimedAt": row["claimed_at"],
            "claimedItemIds": loads(row["claimed_item_ids_json"], []),
            "completedTrips": loads(row["completed_trips_json"], []),
            "storeName": row["store_name"],
            "requestDeadlineAt": row["request_deadline_at"],
            "state": state,
        }

    def _load_items(self, workspace_id: str) -> list[dict[str, Any]]:
        return [
            self._item_dict(row)
            for row in self.conn.execute(
                "SELECT * FROM items WHERE workspace_id = ? ORDER BY created_at, id",
                (workspace_id,),
            )
        ]

    def _save_item(self, workspace_id: str, item: dict[str, Any]) -> None:
        self.conn.execute(
            """
            INSERT INTO items(
              id, workspace_id, name, quantity, unit, note, request_for, added_by,
              bought, version, created_at, removed, run_id, reserved_plan_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              name=excluded.name,
              quantity=excluded.quantity,
              unit=excluded.unit,
              note=excluded.note,
              request_for=excluded.request_for,
              added_by=excluded.added_by,
              bought=excluded.bought,
              version=excluded.version,
              created_at=excluded.created_at,
              removed=excluded.removed,
              run_id=excluded.run_id,
              reserved_plan_id=excluded.reserved_plan_id
            """,
            (
                item["id"],
                workspace_id,
                item["name"],
                item["quantity"],
                item["unit"],
                item["note"],
                item["requestFor"],
                item["addedBy"],
                1 if item["bought"] else 0,
                item["version"],
                item["createdAt"],
                1 if item["removed"] else 0,
                item.get("runId"),
                item.get("reservedPlanId"),
            ),
        )

    def _bump_revision(self, workspace_id: str) -> int:
        self.conn.execute(
            "UPDATE workspaces SET list_revision = list_revision + 1 WHERE id = ?",
            (workspace_id,),
        )
        return self.conn.execute(
            "SELECT list_revision FROM workspaces WHERE id = ?",
            (workspace_id,),
        ).fetchone()["list_revision"]

    def _add_activity(self, workspace_id: str, actor_id: str | None, message: str) -> None:
        self.conn.execute(
            "INSERT INTO activity(id, workspace_id, created_at, actor_id, message) VALUES (?, ?, ?, ?, ?)",
            (new_id("act"), workspace_id, iso(utc_now()), actor_id, message),
        )

    def _find_action_by_request(self, request_id: str):
        return self.conn.execute(
            "SELECT * FROM actions WHERE request_id = ?",
            (request_id,),
        ).fetchone()

    def _replay_or_register(
        self,
        *,
        request_id: str,
        workspace_id: str,
        actor_id: str,
        kind: str,
        expected_kind: str | None = None,
    ) -> dict[str, Any] | None:
        existing = self._find_action_by_request(request_id)
        if not existing:
            return None
        if existing["kind"] != (expected_kind or kind) or existing["workspace_id"] != workspace_id:
            raise ApiError(
                409,
                "REQUEST_ID_REUSED",
                "This requestId was already used for a different action.",
                request_id=request_id,
            )
        return {
            "workspace": self.snapshot(workspace_id),
            "actionId": existing["action_id"],
            "changeSummary": existing["change_summary"],
            **(loads(existing["undo_json"], {}).get("replayExtra") or {}),
        }

    def _record_action(
        self,
        *,
        request_id: str,
        workspace_id: str,
        actor_id: str,
        kind: str,
        change_summary: str,
        undo: dict[str, Any] | None = None,
        replay_extra: dict[str, Any] | None = None,
    ) -> str:
        action_id = new_id("action")
        payload = dict(undo or {})
        if replay_extra:
            payload["replayExtra"] = replay_extra
        self.conn.execute(
            """
            INSERT INTO actions(
              action_id, request_id, workspace_id, actor_id, kind, change_summary, undo_json, undone, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)
            """,
            (
                action_id,
                request_id,
                workspace_id,
                actor_id,
                kind,
                change_summary,
                dumps(payload) if payload else None,
                iso(utc_now()),
            ),
        )
        return action_id

    # ---- mutations ----

    def patch_preferences(self, body: dict[str, Any]) -> dict[str, Any]:
        workspace_id = body["workspaceId"]
        actor_id = body["actorId"]
        request_id = body["requestId"]
        self.require_workspace(workspace_id)
        self.require_actor(workspace_id, actor_id)

        replay = self._replay_or_register(
            request_id=request_id, workspace_id=workspace_id, actor_id=actor_id, kind="preferences"
        )
        if replay:
            return replay

        with transaction(self.conn):
            ws = self.require_workspace(workspace_id)
            servings = body.get("servings") if body.get("servings") is not None else ws["servings"]
            restrictions = body.get("restrictions")
            if restrictions is None:
                restrictions = loads(ws["restrictions_json"], [])
            likes = body.get("likes")
            if likes is None:
                likes = loads(ws["likes_json"], [])
            max_prep = body.get("maxPrepMinutes")
            if max_prep is None:
                max_prep = ws["max_prep_minutes"]
            mode = body.get("spendingMode") or ws["spending_mode"]
            if mode not in {"pooled", "split"}:
                raise ApiError(422, "INVALID_PREFERENCES", "spendingMode must be pooled or split.")

            self.conn.execute(
                """
                UPDATE workspaces SET
                  servings = ?, restrictions_json = ?, likes_json = ?,
                  max_prep_minutes = ?, spending_mode = ?
                WHERE id = ?
                """,
                (servings, dumps(restrictions), dumps(likes), max_prep, mode, workspace_id),
            )
            summary = "Updated group preferences."
            action_id = self._record_action(
                request_id=request_id,
                workspace_id=workspace_id,
                actor_id=actor_id,
                kind="preferences",
                change_summary=summary,
            )
            self._add_activity(workspace_id, actor_id, summary)
            return {"workspace": self.snapshot(workspace_id), "actionId": action_id, "changeSummary": summary}

    def add_item(self, body: dict[str, Any]) -> dict[str, Any]:
        workspace_id = body["workspaceId"]
        actor_id = body["actorId"]
        request_id = body["requestId"]
        self.require_workspace(workspace_id)
        self.require_actor(workspace_id, actor_id)

        replay = self._replay_or_register(
            request_id=request_id, workspace_id=workspace_id, actor_id=actor_id, kind="add_item"
        )
        if replay:
            return replay

        name = body["name"].strip()
        if not name:
            raise ApiError(422, "INVALID_ITEM", "Name is required.")
        unit = body.get("unit") or "count"
        if unit not in SUPPORTED_UNITS:
            raise ApiError(422, "INVALID_ITEM", f"Unsupported unit '{unit}'.")
        quantity = float(body.get("quantity") or 1)
        note = body.get("note") or ""
        request_for = body.get("requestFor") or "household"
        if request_for != "household":
            self.require_actor(workspace_id, request_for)

        with transaction(self.conn):
            items = self._load_items(workspace_id)
            existing = find_compatible(
                items, name=name, unit=unit, note=note, request_for=request_for
            )
            if existing:
                summary = f"{existing['name']} is already on the list."
                action_id = self._record_action(
                    request_id=request_id,
                    workspace_id=workspace_id,
                    actor_id=actor_id,
                    kind="add_item",
                    change_summary=summary,
                    replay_extra={"itemId": existing["id"], "duplicate": True},
                )
                return {
                    "workspace": self.snapshot(workspace_id),
                    "actionId": action_id,
                    "changeSummary": summary,
                    "itemId": existing["id"],
                    "duplicate": True,
                }

            now = iso(utc_now())
            item_id = new_id("item")
            run_id = self._assign_run_if_open(workspace_id, now)
            item = {
                "id": item_id,
                "name": name,
                "quantity": quantity,
                "unit": unit,
                "note": note,
                "requestFor": request_for,
                "addedBy": actor_id,
                "bought": False,
                "version": 1,
                "createdAt": now,
                "removed": False,
                "runId": run_id,
                "reservedPlanId": None,
            }
            self._save_item(workspace_id, item)
            self._bump_revision(workspace_id)
            if run_id:
                self._append_claimed_item(workspace_id, item_id)
            summary = f"Added {name}."
            action_id = self._record_action(
                request_id=request_id,
                workspace_id=workspace_id,
                actor_id=actor_id,
                kind="add_item",
                change_summary=summary,
                undo={"kind": "add_item", "itemId": item_id},
                replay_extra={"itemId": item_id, "duplicate": False},
            )
            self._add_activity(workspace_id, actor_id, summary)
            return {
                "workspace": self.snapshot(workspace_id),
                "actionId": action_id,
                "changeSummary": summary,
                "itemId": item_id,
                "duplicate": False,
            }

    def _assign_run_if_open(self, workspace_id: str, now: str) -> str | None:
        trip = self.conn.execute(
            "SELECT * FROM trips WHERE workspace_id = ?",
            (workspace_id,),
        ).fetchone()
        if not trip or trip["state"] not in {"accepting_requests", "active"}:
            return None
        if trip["state"] == "accepting_requests" and trip["request_deadline_at"] and trip["request_deadline_at"] <= now:
            return None
        if trip["state"] == "accepting_requests":
            return trip["id"]
        return None

    def _append_claimed_item(self, workspace_id: str, item_id: str) -> None:
        trip = self.conn.execute(
            "SELECT * FROM trips WHERE workspace_id = ?",
            (workspace_id,),
        ).fetchone()
        claimed = loads(trip["claimed_item_ids_json"], [])
        if item_id not in claimed:
            claimed.append(item_id)
            self.conn.execute(
                "UPDATE trips SET claimed_item_ids_json = ?, version = version + 1 WHERE workspace_id = ?",
                (dumps(claimed), workspace_id),
            )

    def patch_item(self, item_id: str, body: dict[str, Any]) -> dict[str, Any]:
        workspace_id = body["workspaceId"]
        actor_id = body["actorId"]
        request_id = body["requestId"]
        self.require_workspace(workspace_id)
        self.require_actor(workspace_id, actor_id)

        replay = self._replay_or_register(
            request_id=request_id, workspace_id=workspace_id, actor_id=actor_id, kind="patch_item"
        )
        if replay:
            return replay

        with transaction(self.conn):
            row = self.conn.execute(
                "SELECT * FROM items WHERE id = ? AND workspace_id = ?",
                (item_id, workspace_id),
            ).fetchone()
            if not row:
                raise ApiError(404, "ITEM_NOT_FOUND", "That item is not in this group.")
            if row["version"] != body["expectedItemVersion"]:
                raise ApiError(
                    409,
                    "ITEM_VERSION_CONFLICT",
                    "This item changed. Refresh and try again.",
                    request_id=request_id,
                    extra={"workspace": self.snapshot(workspace_id)},
                )
            item = self._item_dict(row)
            before = copy.deepcopy(item)
            for field, key in [
                ("name", "name"),
                ("quantity", "quantity"),
                ("unit", "unit"),
                ("note", "note"),
                ("requestFor", "requestFor"),
                ("bought", "bought"),
                ("removed", "removed"),
            ]:
                if field in body and body[field] is not None:
                    item[key if key != "requestFor" else "requestFor"] = body[field]
            # map correctly
            if "name" in body and body["name"] is not None:
                item["name"] = body["name"].strip()
            if "quantity" in body and body["quantity"] is not None:
                item["quantity"] = float(body["quantity"])
            if "unit" in body and body["unit"] is not None:
                if body["unit"] not in SUPPORTED_UNITS:
                    raise ApiError(422, "INVALID_ITEM", f"Unsupported unit '{body['unit']}'.")
                item["unit"] = body["unit"]
            if "note" in body and body["note"] is not None:
                item["note"] = body["note"]
            if "requestFor" in body and body["requestFor"] is not None:
                item["requestFor"] = body["requestFor"]
            if "bought" in body and body["bought"] is not None:
                item["bought"] = bool(body["bought"])
            if "removed" in body and body["removed"] is not None:
                item["removed"] = bool(body["removed"])
            item["version"] = int(item["version"]) + 1
            self._save_item(workspace_id, item)
            self._bump_revision(workspace_id)
            summary = f"Updated {item['name']}."
            action_id = self._record_action(
                request_id=request_id,
                workspace_id=workspace_id,
                actor_id=actor_id,
                kind="patch_item",
                change_summary=summary,
                undo={"kind": "patch_item", "before": before, "after": item},
            )
            return {"workspace": self.snapshot(workspace_id), "actionId": action_id, "changeSummary": summary}

    def save_draft(self, draft: dict[str, Any]) -> None:
        with transaction(self.conn):
            self.conn.execute(
                """
                INSERT INTO drafts(id, workspace_id, previous_draft_id, kind, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET payload_json = excluded.payload_json
                """,
                (
                    draft["draftId"],
                    draft["workspaceId"],
                    draft.get("previousDraftId"),
                    draft["kind"],
                    dumps(draft),
                    iso(utc_now()),
                ),
            )

    def get_draft(self, draft_id: str, workspace_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM drafts WHERE id = ? AND workspace_id = ?",
            (draft_id, workspace_id),
        ).fetchone()
        if not row:
            raise ApiError(404, "DRAFT_NOT_FOUND", "That draft was not found for this group.")
        draft = loads(row["payload_json"], {})
        items = self._load_items(workspace_id)
        ws = self.require_workspace(workspace_id)
        return self.refresh_draft_preview(draft, items, ws["list_revision"])

    def refresh_draft_preview(
        self, draft: dict[str, Any], items: list[dict[str, Any]], list_revision: int
    ) -> dict[str, Any]:
        draft = copy.deepcopy(draft)
        draft["listRevision"] = list_revision
        if draft.get("kind") == "recommendation":
            ingredients = draft["recommendation"]["ingredients"]
            draft["recommendation"]["listPreview"] = build_list_preview(ingredients, items)
        elif draft.get("kind") == "grocery_list":
            draft["listPreview"] = build_list_preview(draft.get("items") or [], items)
        return draft

    def confirm_draft(self, body: dict[str, Any]) -> dict[str, Any]:
        workspace_id = body["workspaceId"]
        actor_id = body["actorId"]
        request_id = body["requestId"]
        self.require_workspace(workspace_id)
        self.require_actor(workspace_id, actor_id)

        replay = self._replay_or_register(
            request_id=request_id, workspace_id=workspace_id, actor_id=actor_id, kind="confirm_draft"
        )
        if replay:
            return replay

        with transaction(self.conn):
            ws = self.require_workspace(workspace_id)
            if ws["list_revision"] != body["expectedListRevision"]:
                raise ApiError(
                    409,
                    "LIST_CHANGED",
                    "The cart changed since this draft was prepared.",
                    request_id=request_id,
                    extra={"workspace": self.snapshot(workspace_id)},
                )
            row = self.conn.execute(
                "SELECT * FROM drafts WHERE id = ? AND workspace_id = ?",
                (body["draftId"], workspace_id),
            ).fetchone()
            if not row:
                raise ApiError(404, "DRAFT_NOT_FOUND", "That draft was not found for this group.")
            draft = loads(row["payload_json"], {})
            kind = draft.get("kind")
            if kind == "clarification":
                raise ApiError(422, "CANNOT_CONFIRM", "Clarifications cannot be confirmed.")
            if kind == "weekly_plan" and body.get("target") != "plans":
                raise ApiError(422, "CANNOT_CONFIRM", "Weekly drafts save to plans only.")

            items = self._load_items(workspace_id)
            excluded = set(body.get("excludedIngredientIds") or [])
            now = iso(utc_now())
            plan_id = None
            plan_date = draft.get("planDate") or date.today().isoformat()
            undo: dict[str, Any] = {"kind": "confirm_draft", "deltas": [], "planId": None}

            if kind == "recommendation" and body.get("target", "cart") == "cart":
                recipe = draft["recommendation"]
                preview = build_list_preview(recipe["ingredients"], items)
                updated, deltas = apply_preview_to_cart(
                    recipe["ingredients"],
                    preview,
                    items,
                    excluded_ids=excluded,
                    actor_id=actor_id,
                    now=now,
                    new_item_id=lambda: new_id("item"),
                )
                # Assign open-run IDs to new adds
                run_id = self._assign_run_if_open(workspace_id, now)
                for delta in deltas:
                    if delta["op"] == "add" and run_id:
                        delta["after"]["runId"] = run_id
                        for item in updated:
                            if item["id"] == delta["itemId"]:
                                item["runId"] = run_id
                        self._append_claimed_item(workspace_id, delta["itemId"])
                # Persist all changed items
                existing_ids = {i["id"] for i in items}
                for item in updated:
                    if item["id"] not in existing_ids or any(d["itemId"] == item["id"] for d in deltas):
                        self._save_item(workspace_id, item)
                self._bump_revision(workspace_id)

                plan_id = new_id("plan")
                self.conn.execute(
                    """
                    INSERT INTO plans(id, workspace_id, event, plan_date, servings, recipe_json, action_id, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, NULL, ?)
                    """,
                    (
                        plan_id,
                        workspace_id,
                        recipe["title"],
                        plan_date,
                        recipe.get("servings"),
                        dumps(recipe),
                        now,
                    ),
                )
                undo = {"kind": "confirm_draft", "deltas": deltas, "planId": plan_id, "meal": True}
                summary = f"Saved {recipe['title']} to Plans and updated the cart."
            elif kind == "grocery_list" and body.get("target", "cart") == "cart":
                proposed = draft.get("items") or []
                preview = build_list_preview(proposed, items)
                updated, deltas = apply_preview_to_cart(
                    proposed,
                    preview,
                    items,
                    excluded_ids=excluded,
                    actor_id=actor_id,
                    now=now,
                    new_item_id=lambda: new_id("item"),
                )
                run_id = self._assign_run_if_open(workspace_id, now)
                existing_ids = {i["id"] for i in items}
                for delta in deltas:
                    if delta["op"] == "add" and run_id:
                        delta["after"]["runId"] = run_id
                        for item in updated:
                            if item["id"] == delta["itemId"]:
                                item["runId"] = run_id
                        self._append_claimed_item(workspace_id, delta["itemId"])
                for item in updated:
                    if item["id"] not in existing_ids or any(d["itemId"] == item["id"] for d in deltas):
                        self._save_item(workspace_id, item)
                self._bump_revision(workspace_id)
                undo = {"kind": "confirm_draft", "deltas": deltas, "planId": None, "meal": False}
                summary = "Added grocery requests to the cart."
            elif kind == "weekly_plan" and body.get("target") == "plans":
                meals = draft.get("days") or draft.get("meals") or []
                plan_ids = []
                for meal in meals:
                    pid = new_id("plan")
                    recipe = meal.get("recommendation") or meal
                    title = recipe.get("title") or meal.get("title") or "Planned meal"
                    day = meal.get("planDate") or meal.get("date") or plan_date
                    self.conn.execute(
                        """
                        INSERT INTO plans(id, workspace_id, event, plan_date, servings, recipe_json, action_id, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, NULL, ?)
                        """,
                        (pid, workspace_id, title, day, recipe.get("servings"), dumps(recipe), now),
                    )
                    plan_ids.append(pid)
                undo = {"kind": "confirm_draft", "deltas": [], "planIds": plan_ids, "meal": False}
                summary = "Saved the weekly plans. No groceries were added."
            else:
                raise ApiError(422, "CANNOT_CONFIRM", "This draft cannot be confirmed with that target.")

            action_id = self._record_action(
                request_id=request_id,
                workspace_id=workspace_id,
                actor_id=actor_id,
                kind="confirm_draft",
                change_summary=summary,
                undo=undo,
            )
            if plan_id:
                self.conn.execute(
                    "UPDATE plans SET action_id = ? WHERE id = ?",
                    (action_id, plan_id),
                )
            self._add_activity(workspace_id, actor_id, summary)
            return {"workspace": self.snapshot(workspace_id), "actionId": action_id, "changeSummary": summary}

    def undo_action(self, action_id: str, body: dict[str, Any]) -> dict[str, Any]:
        workspace_id = body["workspaceId"]
        actor_id = body["actorId"]
        request_id = body["requestId"]
        self.require_workspace(workspace_id)
        self.require_actor(workspace_id, actor_id)

        replay = self._replay_or_register(
            request_id=request_id, workspace_id=workspace_id, actor_id=actor_id, kind="undo"
        )
        if replay:
            return replay

        with transaction(self.conn):
            action = self.conn.execute(
                "SELECT * FROM actions WHERE action_id = ? AND workspace_id = ?",
                (action_id, workspace_id),
            ).fetchone()
            if not action:
                raise ApiError(404, "ACTION_NOT_FOUND", "That action was not found.")
            if action["undone"]:
                summary = "Already undone."
                new_action = self._record_action(
                    request_id=request_id,
                    workspace_id=workspace_id,
                    actor_id=actor_id,
                    kind="undo",
                    change_summary=summary,
                )
                return {"workspace": self.snapshot(workspace_id), "actionId": new_action, "changeSummary": summary}

            undo = loads(action["undo_json"], {}) or {}
            retained = []
            if undo.get("kind") == "confirm_draft":
                for delta in undo.get("deltas") or []:
                    row = self.conn.execute(
                        "SELECT * FROM items WHERE id = ? AND workspace_id = ?",
                        (delta["itemId"], workspace_id),
                    ).fetchone()
                    if not row:
                        retained.append(delta["itemId"])
                        continue
                    item = self._item_dict(row)
                    if item.get("bought"):
                        retained.append(item["name"])
                        continue
                    if delta["op"] == "add":
                        after = delta["after"]
                        if (
                            item["quantity"] == after["quantity"]
                            and item["unit"] == after["unit"]
                            and normalize_name(item["name"]) == normalize_name(after["name"])
                        ):
                            item["removed"] = True
                            item["version"] = int(item["version"]) + 1
                            self._save_item(workspace_id, item)
                        else:
                            retained.append(item["name"])
                    elif delta["op"] == "increase":
                        after = delta["after"]
                        before = delta["before"]
                        if item["quantity"] == after["quantity"] and not item["bought"]:
                            item["quantity"] = before["quantity"]
                            item["version"] = int(item["version"]) + 1
                            self._save_item(workspace_id, item)
                        else:
                            retained.append(item["name"])
                if undo.get("planId") and not retained:
                    self.conn.execute(
                        "DELETE FROM plans WHERE id = ? AND workspace_id = ?",
                        (undo["planId"], workspace_id),
                    )
                elif undo.get("planId") and retained:
                    # Keep plan with partial undo
                    pass
                for pid in undo.get("planIds") or []:
                    self.conn.execute(
                        "DELETE FROM plans WHERE id = ? AND workspace_id = ?",
                        (pid, workspace_id),
                    )
                self._bump_revision(workspace_id)
            elif undo.get("kind") == "add_item":
                item_id = undo["itemId"]
                row = self.conn.execute(
                    "SELECT * FROM items WHERE id = ? AND workspace_id = ?",
                    (item_id, workspace_id),
                ).fetchone()
                if row and not row["bought"]:
                    item = self._item_dict(row)
                    item["removed"] = True
                    item["version"] = int(item["version"]) + 1
                    self._save_item(workspace_id, item)
                    self._bump_revision(workspace_id)
                else:
                    retained.append(item_id)

            self.conn.execute("UPDATE actions SET undone = 1 WHERE action_id = ?", (action_id,))
            if retained:
                summary = (
                    "Undo kept some changes that were edited or bought: "
                    + ", ".join(str(x) for x in retained)
                    + "."
                )
            else:
                summary = "Undid the saved changes."
            new_action = self._record_action(
                request_id=request_id,
                workspace_id=workspace_id,
                actor_id=actor_id,
                kind="undo",
                change_summary=summary,
            )
            self._add_activity(workspace_id, actor_id, summary)
            return {"workspace": self.snapshot(workspace_id), "actionId": new_action, "changeSummary": summary}

    def patch_trip(self, body: dict[str, Any]) -> dict[str, Any]:
        workspace_id = body["workspaceId"]
        actor_id = body["actorId"]
        request_id = body["requestId"]
        action = body["action"]
        self.require_workspace(workspace_id)
        self.require_actor(workspace_id, actor_id)

        replay = self._replay_or_register(
            request_id=request_id, workspace_id=workspace_id, actor_id=actor_id, kind=f"trip_{action}"
        )
        if replay:
            return replay

        with transaction(self.conn):
            trip = self.conn.execute(
                "SELECT * FROM trips WHERE workspace_id = ?",
                (workspace_id,),
            ).fetchone()
            if not trip:
                raise ApiError(404, "TRIP_NOT_FOUND", "No trip record for this group.")
            if trip["version"] != body["expectedTripVersion"]:
                # Concurrent start: if already claimed, winner keeps it
                if action == "start" and trip["claimed_by"]:
                    raise ApiError(
                        409,
                        "TRIP_CLAIMED",
                        f"{trip['claimed_by']} already started this store run.",
                        request_id=request_id,
                        extra={"workspace": self.snapshot(workspace_id)},
                    )
                raise ApiError(
                    409,
                    "TRIP_VERSION_CONFLICT",
                    "The store run changed. Refresh and try again.",
                    request_id=request_id,
                    extra={"workspace": self.snapshot(workspace_id)},
                )

            now = utc_now()
            now_s = iso(now)
            if action == "start":
                if trip["claimed_by"] and trip["state"] not in {"idle", "completed"}:
                    raise ApiError(
                        409,
                        "TRIP_CLAIMED",
                        f"{trip['claimed_by']} already started this store run.",
                        request_id=request_id,
                        extra={"workspace": self.snapshot(workspace_id)},
                    )
                minutes = body.get("requestWindowMinutes") or 10
                if minutes not in (5, 10, 15):
                    raise ApiError(422, "INVALID_TRIP", "requestWindowMinutes must be 5, 10, or 15.")
                store = (body.get("storeName") or trip["store_name"] or "the store").strip()
                deadline = iso(now + timedelta(minutes=minutes))
                claimed_ids = [
                    item["id"]
                    for item in self._load_items(workspace_id)
                    if not item["removed"] and not item["bought"] and not item.get("runId")
                ]
                # Also include currently unbought items with no run, and attach run id
                run_id = new_id("run")
                for item in self._load_items(workspace_id):
                    if not item["removed"] and not item["bought"]:
                        item["runId"] = run_id
                        self._save_item(workspace_id, item)
                        if item["id"] not in claimed_ids:
                            claimed_ids.append(item["id"])
                completed = loads(trip["completed_trips_json"], [])
                self.conn.execute(
                    """
                    UPDATE trips SET
                      id = ?, version = version + 1, claimed_by = ?, store_name = ?,
                      claimed_at = ?, request_deadline_at = ?, state = 'accepting_requests',
                      claimed_item_ids_json = ?, completed_trips_json = ?
                    WHERE workspace_id = ?
                    """,
                    (run_id, actor_id, store, now_s, deadline, dumps(claimed_ids), dumps(completed), workspace_id),
                )
                self.conn.execute(
                    "UPDATE workspaces SET last_store_name = ? WHERE id = ?",
                    (store, workspace_id),
                )
                summary = f"Started a store run to {store}."
            elif action in {"release", "finish"}:
                if trip["claimed_by"] != actor_id:
                    raise ApiError(403, "NOT_SHOPPER", "Only the current shopper can end this run.")
                completed = loads(trip["completed_trips_json"], [])
                if action == "finish":
                    bought_ids = [
                        item["id"]
                        for item in self._load_items(workspace_id)
                        if item.get("runId") == trip["id"] and item["bought"] and not item["removed"]
                    ]
                    completed.append(
                        {
                            "id": trip["id"],
                            "claimedBy": trip["claimed_by"],
                            "storeName": trip["store_name"],
                            "claimedAt": trip["claimed_at"],
                            "finishedAt": now_s,
                            "boughtItemIds": bought_ids,
                        }
                    )
                    # Clear run assignment from unbought items
                    for item in self._load_items(workspace_id):
                        if item.get("runId") == trip["id"] and not item["bought"]:
                            item["runId"] = None
                            item["version"] = int(item["version"]) + 1
                            self._save_item(workspace_id, item)
                    summary = "Finished the store run. Unbought requests stay for next time."
                else:
                    for item in self._load_items(workspace_id):
                        if item.get("runId") == trip["id"] and not item["bought"]:
                            item["runId"] = None
                            item["version"] = int(item["version"]) + 1
                            self._save_item(workspace_id, item)
                    summary = "Released the store run."
                self.conn.execute(
                    """
                    UPDATE trips SET
                      version = version + 1, claimed_by = NULL, claimed_at = NULL,
                      request_deadline_at = NULL, state = 'idle',
                      claimed_item_ids_json = '[]', completed_trips_json = ?
                    WHERE workspace_id = ?
                    """,
                    (dumps(completed), workspace_id),
                )
            else:
                raise ApiError(422, "INVALID_TRIP", f"Unknown trip action '{action}'.")

            action_id = self._record_action(
                request_id=request_id,
                workspace_id=workspace_id,
                actor_id=actor_id,
                kind=f"trip_{action}",
                change_summary=summary,
            )
            self._add_activity(workspace_id, actor_id, summary)
            return {"workspace": self.snapshot(workspace_id), "actionId": action_id, "changeSummary": summary}

    def create_expense(self, body: dict[str, Any]) -> dict[str, Any]:
        workspace_id = body["workspaceId"]
        actor_id = body["actorId"]
        request_id = body["requestId"]
        self.require_workspace(workspace_id)
        self.require_actor(workspace_id, actor_id)

        replay = self._replay_or_register(
            request_id=request_id, workspace_id=workspace_id, actor_id=actor_id, kind="expense"
        )
        if replay:
            return replay

        with transaction(self.conn):
            mode = body["mode"]
            status = body.get("status") or "confirmed"
            total = int(body["totalCents"])
            payer = body["payerId"]
            self.require_actor(workspace_id, payer)
            shared = list(body.get("sharedMemberIds") or [])
            personal = dict(body.get("personalAllocations") or {})
            run_id = body.get("runId")
            # Distinct requestIds make additional receipts explicit; replays are handled above.

            allocations: dict[str, int] = {}
            if mode == "pooled":
                allocations = {payer: total}
                status = "confirmed"
            elif mode == "split":
                if status == "needs_review" or any(v is None for v in personal.values()):
                    status = "needs_review"
                    allocations = {}
                else:
                    try:
                        allocations = allocate_split(total, shared, personal)
                    except ValueError as exc:
                        raise ApiError(422, "INVALID_EXPENSE", str(exc), request_id=request_id) from exc
            else:
                raise ApiError(422, "INVALID_EXPENSE", "mode must be pooled or split.")

            expense_id = new_id("expense")
            self.conn.execute(
                """
                INSERT INTO expenses(
                  id, workspace_id, run_id, total_cents, currency, payer_id, mode,
                  shared_member_ids_json, personal_allocations_json, allocations_json, status, created_at
                ) VALUES (?, ?, ?, ?, 'USD', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    expense_id,
                    workspace_id,
                    run_id,
                    total,
                    payer,
                    mode,
                    dumps(shared),
                    dumps(personal),
                    dumps(allocations),
                    status,
                    iso(utc_now()),
                ),
            )
            summary = (
                "Saved as Needs review. Nobody owes anything for it yet."
                if status == "needs_review"
                else "Receipt saved."
            )
            action_id = self._record_action(
                request_id=request_id,
                workspace_id=workspace_id,
                actor_id=actor_id,
                kind="expense",
                change_summary=summary,
                replay_extra={"expenseId": expense_id},
            )
            self._add_activity(workspace_id, actor_id, summary)
            return {
                "workspace": self.snapshot(workspace_id),
                "actionId": action_id,
                "changeSummary": summary,
                "expenseId": expense_id,
            }

    def compare_baskets(self, body: dict[str, Any]) -> dict[str, Any]:
        workspace_id = body["workspaceId"]
        actor_id = body["actorId"]
        self.require_workspace(workspace_id)
        self.require_actor(workspace_id, actor_id)
        item_ids = body["itemIds"]
        items = [item for item in self._load_items(workspace_id) if item["id"] in item_ids and not item["removed"]]
        if not items:
            raise ApiError(422, "INVALID_BASKET", "Select at least one cart item.")

        catalog = [
            dict(row)
            for row in self.conn.execute("SELECT * FROM ingredients ORDER BY name")
        ]

        def match_product(item: dict[str, Any]) -> dict[str, Any] | None:
            target = normalize_name(item["name"])
            for product in catalog:
                pname = normalize_name(product["name"])
                if target in pname or any(token in pname for token in target.split() if len(token) > 3):
                    return product
            # fuzzy: last word
            for product in catalog:
                if any(token in normalize_name(product["name"]) for token in target.split() if len(token) > 4):
                    return product
            return None

        request_quantities = [
            {"itemId": item["id"], "quantity": item["quantity"], "unit": item["unit"]}
            for item in items
        ]

        # Try live Kroger if credentials exist; otherwise sample catalog.
        live = None
        try:
            from app.services.kroger_prices import KrogerError, lookup_ingredients

            live = lookup_ingredients([item["name"] for item in items], body["postalCode"])
        except Exception:
            live = None

        if live:
            baskets = self._baskets_from_kroger(items, live)
            source_mode = "live_api"
            sample_notice = None
            checked_at = live.get("retrievedAt") or iso(utc_now())
        else:
            baskets = self._baskets_from_sample(items, catalog, match_product)
            source_mode = "sample"
            sample_notice = "Synthetic Kroger-shaped prices for UI development, not live retailer quotes."
            checked_at = iso(utc_now())

        # Rank complete baskets by known subtotal
        complete = sorted(
            [b for b in baskets if b["complete"]],
            key=lambda b: b["knownSubtotalCents"],
        )
        incomplete = [b for b in baskets if not b["complete"]]
        ranked = complete + incomplete

        ws = self.require_workspace(workspace_id)
        return {
            "schemaVersion": 2,
            "workspaceId": workspace_id,
            "listRevision": ws["list_revision"],
            "sourceMode": source_mode,
            "sampleNotice": sample_notice,
            "checkedAt": checked_at,
            "currency": "USD",
            "requestQuantities": request_quantities,
            "feeStatus": "Tax, delivery, discounts, and travel costs not included; final totals unverified.",
            "baskets": ranked,
            "ingredients": [
                {
                    "id": row["id"],
                    "name": row["name"],
                    "description": row["description"],
                    "priceCents": row["price_cents"],
                    "currency": row["currency"],
                }
                for row in catalog
            ],
        }

    def _baskets_from_sample(
        self,
        items: list[dict[str, Any]],
        catalog: list[dict[str, Any]],
        match_product: Callable[[dict[str, Any]], dict[str, Any] | None],
    ) -> list[dict[str, Any]]:
        # Two sample stores: Sample Kroger and a slightly different alt store
        stores = [
            ("basket-a", "Sample Kroger", 1.0),
            ("basket-b", "Sample Market B", 1.08),
        ]
        baskets = []
        for store_id, store_name, price_factor in stores:
            lines = []
            missing = []
            subtotal = 0
            for item in items:
                product = match_product(item)
                if not product or product["price_cents"] is None:
                    missing.append(item["id"])
                    continue
                pkg_qty = float(product["package_quantity"] or 1)
                packages = max(1, int((float(item["quantity"]) + pkg_qty - 1) // pkg_qty)) if pkg_qty else 1
                price = int(round(product["price_cents"] * price_factor))
                line = price * packages
                subtotal += line
                lines.append(
                    {
                        "itemId": item["id"],
                        "productId": product["id"],
                        "productName": product["name"],
                        "description": product["description"],
                        "packageSize": {
                            "quantity": product["package_quantity"],
                            "unit": product["package_unit"],
                        },
                        "packagesNeeded": packages,
                        "pricePerPackageCents": price,
                        "lineTotalCents": line,
                    }
                )
            baskets.append(
                {
                    "id": store_id,
                    "storeId": store_id,
                    "storeName": store_name,
                    "distanceKm": None,
                    "items": lines,
                    "missingItemIds": missing,
                    "complete": len(missing) == 0,
                    "knownSubtotalCents": subtotal,
                    "taxCents": None,
                    "deliveryFeeCents": None,
                }
            )
        return baskets

    def _baskets_from_kroger(self, items: list[dict[str, Any]], live: dict[str, Any]) -> list[dict[str, Any]]:
        store = live.get("store") or {}
        store_id = str(store.get("locationId") or "kroger")
        store_name = store.get("name") or "Kroger"
        by_ingredient = {row["ingredient"]: row for row in live.get("ingredients") or []}
        lines = []
        missing = []
        subtotal = 0
        for item in items:
            match = by_ingredient.get(item["name"]) or {}
            options = match.get("options") or []
            option = None
            for candidate in options:
                price = candidate.get("promoPrice")
                if price is None:
                    price = candidate.get("regularPrice")
                if price is not None:
                    option = candidate
                    break
            if not option:
                missing.append(item["id"])
                continue
            price_dollars = option.get("promoPrice")
            if price_dollars is None:
                price_dollars = option.get("regularPrice")
            price_cents = int(round(float(price_dollars) * 100))
            packages = 1
            line = price_cents * packages
            subtotal += line
            lines.append(
                {
                    "itemId": item["id"],
                    "productId": option.get("productId") or new_id("kroger"),
                    "productName": option.get("description") or item["name"],
                    "description": option.get("description") or "",
                    "packageSize": {"quantity": 1, "unit": item["unit"]},
                    "packagesNeeded": packages,
                    "pricePerPackageCents": price_cents,
                    "lineTotalCents": line,
                }
            )
        return [
            {
                "id": f"basket-{store_id}",
                "storeId": store_id,
                "storeName": store_name,
                "distanceKm": None,
                "items": lines,
                "missingItemIds": missing,
                "complete": len(missing) == 0,
                "knownSubtotalCents": subtotal,
                "taxCents": None,
                "deliveryFeeCents": None,
            }
        ]

