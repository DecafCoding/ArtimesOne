"""Shared SQL predicates for the item-visibility rule.

Phase 7 introduces three orthogonal ways to hide an item from the main
feed: it's a Short, the user has passed (dismissed) it, or the user has
filed it into a library. The same rule has to be threaded through every
list-query site (dashboard, /items, /topics/{slug}, /sources/{id}, and
the agent's search tools), so the predicate lives here rather than
duplicated inline.

Library membership hides; project membership does not — projects are
active-work staging areas and the user still wants those items in the
feed.

The ``show_passed`` flag flips the passed-items clause rather than
broadening it: when true, the filter returns *only* passed items
(the "trash" view on ``/items?show=passed``). Library-filed items stay
hidden in either mode — an item that is both passed and library-filed
still shows up on the library detail page, not the passed-items toggle.
"""

from __future__ import annotations


def build_visibility_filter(
    alias: str = "i",
    *,
    show_passed: bool = False,
) -> str:
    """Return a SQL boolean expression for the main-feed visibility rule.

    Compose into a WHERE clause with other predicates via ``AND``. Always
    excludes Shorts and library-filed items. ``show_passed`` toggles the
    passed-items clause between "hide passed" (default) and "only passed".

    The ``alias`` parameter is the items-table alias used by the caller
    (every query site in this codebase uses ``i``).
    """
    clauses = [
        f"{alias}.status != 'skipped_short'",
    ]
    if show_passed:
        clauses.append(f"{alias}.passed_at IS NOT NULL")
    else:
        clauses.append(f"{alias}.passed_at IS NULL")
    clauses.append(
        f"{alias}.id NOT IN ("
        "SELECT li.item_id FROM list_items li "
        "JOIN lists l ON l.id = li.list_id "
        "WHERE l.kind = 'library')"
    )
    return " AND ".join(clauses)
