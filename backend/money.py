"""Exact-cent expense allocation."""

from __future__ import annotations


def allocate_split(
    total_cents: int,
    shared_member_ids: list[str],
    personal_allocations: dict[str, int | None],
) -> dict[str, int]:
    """
    Shared remainder = total - known personal cents.
    Divide remainder equally among shared members with deterministic remainder cents.
    Each person's personal amount is added to their share.
    """
    if total_cents < 0:
        raise ValueError("totalCents must be nonnegative")

    known_personal = 0
    cleaned: dict[str, int] = {}
    for actor_id, amount in personal_allocations.items():
        if amount is None:
            raise ValueError("unknown personal amounts cannot be confirmed")
        if amount < 0:
            raise ValueError("personal allocations must be nonnegative")
        cleaned[actor_id] = int(amount)
        known_personal += int(amount)

    if known_personal > total_cents:
        raise ValueError("personal allocations exceed total")

    remainder = total_cents - known_personal
    if remainder > 0 and not shared_member_ids:
        raise ValueError("shared remainder requires at least one participant")

    allocations: dict[str, int] = {actor_id: 0 for actor_id in shared_member_ids}
    for actor_id, amount in cleaned.items():
        allocations[actor_id] = allocations.get(actor_id, 0) + amount

    if shared_member_ids and remainder:
        n = len(shared_member_ids)
        base = remainder // n
        extra = remainder % n
        # Deterministic: sorted ids get the first remainder cents.
        ordered = sorted(shared_member_ids)
        for index, actor_id in enumerate(ordered):
            share = base + (1 if index < extra else 0)
            allocations[actor_id] = allocations.get(actor_id, 0) + share

    if sum(allocations.values()) != total_cents:
        raise ValueError("allocations must sum to totalCents")
    return allocations


def balances_from_expenses(
    expenses: list[dict],
) -> list[dict[str, object]]:
    """Who owes whom from confirmed split expenses. Pooled records create no debt."""
    net: dict[str, int] = {}
    for expense in expenses:
        if expense.get("status") != "confirmed" or expense.get("mode") != "split":
            continue
        payer = expense["payerId"]
        for actor_id, cents in (expense.get("allocations") or {}).items():
            if actor_id == payer:
                continue
            # actor owes payer `cents`
            key_from = actor_id
            key_to = payer
            net[f"{key_from}->{key_to}"] = net.get(f"{key_from}->{key_to}", 0) + int(cents)

    # Net opposing edges
    simplified: dict[tuple[str, str], int] = {}
    for key, cents in net.items():
        left, right = key.split("->", 1)
        forward = (left, right)
        backward = (right, left)
        if backward in simplified:
            if simplified[backward] >= cents:
                simplified[backward] -= cents
                if simplified[backward] == 0:
                    del simplified[backward]
            else:
                simplified[forward] = cents - simplified[backward]
                del simplified[backward]
        else:
            simplified[forward] = simplified.get(forward, 0) + cents

    return [
        {"from": pair[0], "to": pair[1], "cents": amount}
        for pair, amount in sorted(simplified.items())
        if amount > 0
    ]
