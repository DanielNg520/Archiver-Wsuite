"""
core.scan_order
───────────────
Decide the ORDER in which a platform's users are walked in one scan cycle.

The old behavior was `sorted(users)` — plain alphabetical, identical every
run. Combined with a full cycle that now takes many hours (conservative
per-user pacing × a large roster) plus periodic loop restarts that reset the
walk to the top, that meant users late in the alphabet (e.g. an X handle
starting "R") could go days without a scan while "A…" users were refreshed
every cycle. Classic tail starvation.

This module orders users **staleness-first**: whoever was scanned longest ago
(or never) goes to the front, so a restart naturally favors exactly the users a
previous cycle didn't reach. On top of that it adds bounded randomness so users
of similar staleness don't form a fixed metronome — that unpredictability is
also an anti-detection win (a scanner sweeping a sorted list is a bot tell).

Pure and I/O-free on purpose: the caller supplies `last_runs` (username →
epoch-seconds of last scan, missing = never) and this returns the ordered
tuple. That keeps it trivially unit-testable and keeps DB access in the store.

Phase 2 will add a `priority` set (always-first users, re-injected on an
interval); the signature already carries it so callers don't churn.
"""

from __future__ import annotations

import random
from collections.abc import Iterable, Mapping, Sequence

# A user never scanned sorts before everyone: treat "never" as epoch 0 so the
# staleness key is uniformly a float and the newest-added users get picked up
# promptly rather than languishing behind the incremental crowd.
_NEVER = 0.0


def order_users(
    usernames: Sequence[str],
    last_runs: Mapping[str, float],
    *,
    jitter_seconds: float = 21600.0,
    priority: Iterable[str] = (),
    rng: "random.Random | None" = None,
) -> tuple[str, ...]:
    """Return `usernames` reordered staleness-first with bounded randomness.

    - `last_runs`: username → epoch-seconds of the last scan. A username absent
      from the map (or mapping to a falsy value) is treated as "never scanned"
      and sinks to the front.
    - `jitter_seconds`: width of a uniform random offset ADDED to each user's
      last-run key before sorting. Users whose last scans fall within this
      window of each other get shuffled relative to one another every call, so
      the order isn't a fixed sweep — while users genuinely more stale than the
      window still come first. 0 disables randomness (pure staleness order).
    - `priority`: usernames to force to the FRONT (Phase 2). Their internal
      order is randomized; among themselves staleness is ignored. Names in
      `priority` that aren't in `usernames` are silently dropped.
    - `rng`: injectable Random for deterministic tests; defaults to the module
      random.

    De-duplicates while preserving the computed order, so a username appearing
    in both `priority` and the general pool is emitted once (in the priority
    block).
    """
    r = rng or random
    pool = list(usernames)
    prio_set = {u for u in priority if u in set(pool)}

    def key(u: str) -> float:
        base = float(last_runs.get(u) or _NEVER)
        if jitter_seconds > 0:
            base += r.uniform(0.0, jitter_seconds)
        return base

    prio_block = [u for u in pool if u in prio_set]
    r.shuffle(prio_block)  # no fixed order among priority users

    rest = sorted((u for u in pool if u not in prio_set), key=key)

    seen: set[str] = set()
    ordered: list[str] = []
    for u in (*prio_block, *rest):
        if u not in seen:
            seen.add(u)
            ordered.append(u)
    return tuple(ordered)


def due_for_reinjection(
    users_since: int,
    elapsed_seconds: float,
    *,
    every_n_users: int,
    every_seconds: float,
) -> bool:
    """Should the priority users be spliced back into an in-progress walk now?

    The 'fit interval' is a min-of trigger: fire when EITHER `every_n_users`
    non-priority users have been walked since the last priority pass OR
    `every_seconds` of wall-clock have elapsed since it — whichever comes first.
    A non-positive threshold disables that arm (so `every_n_users<=0` means
    "time only", `every_seconds<=0` means "count only", both<=0 means never).

    Kept pure and tiny so the orchestrator's time-driven walk can lean on a
    tested decision instead of open-coding the condition."""
    by_count = every_n_users > 0 and users_since >= every_n_users
    by_time  = every_seconds > 0 and elapsed_seconds >= every_seconds
    return bool(by_count or by_time)


def auto_priority(
    active_days: Mapping[str, int],
    *,
    min_active_days: int,
    limit: int = 0,
) -> tuple[str, ...]:
    """Rank the 'regular posters' to auto-promote to priority, most-active first.

    `active_days`: username → number of distinct days the user posted within the
    look-back window (from ItemStore.active_days_since). A user with at least
    `min_active_days` qualifies — i.e. posted on that many separate days
    recently, the signal for someone worth keeping fresh. Returns them ORDERED
    most-active-first (name as a stable tiebreak, so the ranking is
    deterministic), truncated to `limit` when limit>0 (0 = no cap).

    Ordered, not a set, so the caller can keep manual marks and fill only the
    remaining priority slots with the top regulars. Pure."""
    if min_active_days <= 0:
        return ()
    qualified = sorted(
        ((u, d) for u, d in active_days.items() if d >= min_active_days),
        key=lambda kv: (-kv[1], kv[0]),
    )
    if limit and len(qualified) > limit:
        qualified = qualified[:limit]
    return tuple(u for u, _ in qualified)
