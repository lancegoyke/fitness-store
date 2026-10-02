"""Which lift a logged set was performed as, and when two lifts are the same (#708).

A ``LoggedSet`` stamps its lift at write time (``LoggedSet.exercise`` /
``exercise_name``), the way #600 stamps ``unit``: the anchor slot's catalog FK
and the name the coach saw on the row at that moment. History reads (1RM, PRs,
"last time", the agent's recent logs) group by that stamp, so an agent swap or
a coach rename changes the plan from then on and leaves what was performed
alone. ``LoggedSet.lift`` resolves the stamp, falling back to the anchor slot's
live identity for a row old code wrote without one.

**The rule** (:func:`same_lift`). Two lifts are the same when

- both carry a catalog FK and the FKs are equal (the names don't matter), or
- at least one has no FK and their case-folded, stripped names are equal.

Two different FKs never match, even under one name. The second clause is what
keeps linking a free-text name to the catalog from acting like a swap: a coach
who typed "Back Squat" and later picks catalog "Back Squat" (#696) keeps the
athlete's free-text history under the linked lift. It is symmetric, so
unlinking a row doesn't orphan its catalog-stamped history either.

The rule isn't transitive (a name-only set matches two different FKs that share
its name), so it can't be a dict key. Targets are matched against an index
(:class:`LiftIndex`), and an untargeted grouping (the records panel) uses
:func:`representatives`. The stored string key stays the FK-first
``one_rm.key_str`` of whichever lift is being looked up.

Accepted trade-off (Lance, 2026-10-02): a coach fixing a typo in a free-text
name after sets are logged leaves those sets under the old spelling.
"""

from collections import defaultdict
from typing import NamedTuple


class Lift(NamedTuple):
    """A lift identity: catalog ``Exercise`` pk (or ``None``) plus display name.

    Carries the same two attributes ``one_rm.refresh_one_rms`` reads off a
    ``Prescription`` or ``ExerciseSlot``, so it can stand in for either.
    """

    exercise_id: object
    name: str


def norm_name(name):
    """The comparison form of a lift name: stripped and case-folded."""
    return (name or "").strip().lower()


def lift_of(obj):
    """``obj``'s ``(exercise_id, name)`` as a :class:`Lift` (a cell, slot or Lift)."""
    return Lift(obj.exercise_id, obj.name)


def same_lift(a, b):
    """Whether lifts ``a`` and ``b`` are the same lift — see the module docstring."""
    if a.exercise_id is not None and b.exercise_id is not None:
        return a.exercise_id == b.exercise_id
    return norm_name(a.name) == norm_name(b.name)


class LiftIndex:
    """Items indexed by their lift, for :func:`same_lift` lookups by target.

    ``matching(target)`` returns every item whose lift matches ``target``, in
    the order the items were given, so a newest-first input stays newest-first.
    """

    def __init__(self, items, lift):
        self._by_fk = defaultdict(list)
        self._by_name = defaultdict(list)
        for position, item in enumerate(items):
            item_lift = lift(item)
            if item_lift is None:
                continue
            entry = (position, item_lift, item)
            if item_lift.exercise_id is not None:
                self._by_fk[item_lift.exercise_id].append(entry)
            self._by_name[norm_name(item_lift.name)].append(entry)

    def matching(self, target):
        """Items whose lift is :func:`same_lift` as ``target``, in input order."""
        named = self._by_name.get(norm_name(target.name), [])
        if target.exercise_id is None:
            hits = named
        else:
            hits = self._by_fk.get(target.exercise_id, []) + [
                entry for entry in named if entry[1].exercise_id is None
            ]
        return [item for _, _, item in sorted(hits, key=lambda entry: entry[0])]


def representatives(lifts):
    """The distinct lifts in a history, one per row of a records-style list.

    Every catalog FK present is one lift. A name-only lift stands alone only
    when no FK-stamped lift shares its name; otherwise it is folded into each
    such FK (it matches every one of them under :func:`same_lift`). Returned
    in first-seen order. An FK's name is the first one seen for it.
    """
    by_fk = {}
    fk_names = set()
    name_only = {}
    for item in lifts:
        if item is None:
            continue
        if item.exercise_id is not None:
            by_fk.setdefault(item.exercise_id, item)
            fk_names.add(norm_name(item.name))
        else:
            name_only.setdefault(norm_name(item.name), item)
    return list(by_fk.values()) + [
        item for name, item in name_only.items() if name not in fk_names
    ]
