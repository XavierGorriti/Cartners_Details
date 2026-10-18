"""Cart quantity matching and list-preview helpers."""

from __future__ import annotations

from typing import Any

COMPATIBLE_UNITS = {"count", "g", "ml", "can", "pack"}


def normalize_name(name: str) -> str:
    return " ".join(name.strip().lower().split())


def active_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [item for item in items if not item.get("removed") and not item.get("bought")]


def find_compatible(
    items: list[dict[str, Any]],
    *,
    name: str,
    unit: str,
    note: str,
    request_for: str,
) -> dict[str, Any] | None:
    target = normalize_name(name)
    note_n = (note or "").strip().lower()
    for item in active_items(items):
        if item.get("reservedPlanId"):
            # Quantity already committed to another meal cannot satisfy this one.
            continue
        if normalize_name(item["name"]) != target:
            continue
        if item.get("unit") != unit:
            continue
        if (item.get("note") or "").strip().lower() != note_n:
            continue
        if item.get("requestFor") != request_for:
            continue
        return item
    return None


def build_list_preview(
    proposed: list[dict[str, Any]],
    cart_items: list[dict[str, Any]],
    *,
    default_request_for: str = "household",
) -> list[dict[str, Any]]:
    """Compare proposed recipe/grocery lines to the live cart."""
    preview: list[dict[str, Any]] = []
    for row in proposed:
        unit = row.get("unit") or "count"
        if unit not in COMPATIBLE_UNITS:
            preview.append(
                {
                    "ingredientId": row["id"],
                    "action": "review",
                    "reason": f"Unsupported unit '{unit}' for automatic matching.",
                }
            )
            continue
        request_for = row.get("requestFor") or default_request_for
        existing = find_compatible(
            cart_items,
            name=row["name"],
            unit=unit,
            note=row.get("note") or "",
            request_for=request_for,
        )
        needed = float(row["quantity"])
        if existing is None:
            preview.append(
                {
                    "ingredientId": row["id"],
                    "action": "add",
                    "deltaQuantity": needed,
                }
            )
            continue
        current = float(existing["quantity"])
        if current >= needed:
            preview.append(
                {
                    "ingredientId": row["id"],
                    "action": "keep",
                    "existingItemId": existing["id"],
                    "existingQuantity": current,
                    "deltaQuantity": 0,
                }
            )
        else:
            preview.append(
                {
                    "ingredientId": row["id"],
                    "action": "increase",
                    "existingItemId": existing["id"],
                    "existingQuantity": current,
                    "deltaQuantity": needed - current,
                    "reason": f"{needed:g} needed; {current:g} already requested.",
                }
            )
    return preview


def apply_preview_to_cart(
    proposed: list[dict[str, Any]],
    preview: list[dict[str, Any]],
    cart_items: list[dict[str, Any]],
    *,
    excluded_ids: set[str],
    actor_id: str,
    now: str,
    new_item_id,
    default_request_for: str = "household",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """
    Mutate cart according to preview. Returns (updated items, undo deltas).
    """
    by_id = {item["id"]: dict(item) for item in cart_items}
    order = [item["id"] for item in cart_items]
    deltas: list[dict[str, Any]] = []
    proposed_by_id = {row["id"]: row for row in proposed}

    for row in preview:
        ingredient_id = row["ingredientId"]
        if ingredient_id in excluded_ids:
            continue
        action = row["action"]
        if action in {"keep", "review"}:
            continue
        source = proposed_by_id[ingredient_id]
        request_for = source.get("requestFor") or default_request_for
        if action == "add":
            item_id = new_item_id()
            item = {
                "id": item_id,
                "name": source["name"],
                "quantity": float(row.get("deltaQuantity") or source["quantity"]),
                "unit": source.get("unit") or "count",
                "note": source.get("note") or "",
                "requestFor": request_for,
                "addedBy": actor_id,
                "bought": False,
                "version": 1,
                "createdAt": now,
                "removed": False,
                "runId": None,
                "reservedPlanId": None,
            }
            by_id[item_id] = item
            order.append(item_id)
            deltas.append({"op": "add", "itemId": item_id, "after": dict(item)})
        elif action == "increase":
            item_id = row["existingItemId"]
            item = by_id[item_id]
            before = dict(item)
            item["quantity"] = float(item["quantity"]) + float(row["deltaQuantity"])
            item["version"] = int(item["version"]) + 1
            deltas.append(
                {
                    "op": "increase",
                    "itemId": item_id,
                    "before": before,
                    "after": dict(item),
                    "deltaQuantity": float(row["deltaQuantity"]),
                }
            )
    return [by_id[item_id] for item_id in order], deltas
