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

**One catalog lift, several names.** An FK can be stamped under more than one
name (a staff rename of the catalog entry between two picks, an admin edit).
It is still one lift, keyed ``id:<pk>`` alone, so an FK target is matched under
EVERY name its FK carries in the history being searched, as well as its own:
a free-text "Squat" set counts toward catalog lift X once X has been stamped
"Squat" anywhere in that history, whichever of X's names the target shows.
The persisted estimate (``one_rm.derive_one_rm_values``) goes one step
further and passes X's names explicitly — its stamps plus the athlete's live
rows linked to X — so a refresh target's own name (which may come from a
deleted set's stamp) never decides what ``id:<pk>`` holds.

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
    return (name or "").strip().casefold()


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

    def names_of(self, exercise_id):
        """Every normalized name catalog lift ``exercise_id`` carries here."""
        return {norm_name(entry[1].name) for entry in self._by_fk.get(exercise_id, [])}

    def matching(self, target, names=None):
        """Items whose lift is :func:`same_lift` as ``target``, in input order.

        An FK target is tried under each of its names in this index too (see
        the module docstring), or, when ``names`` is given, under exactly
        those normalized names instead. The buckets only narrow the search
        (every match shares the target's FK or one of those names);
        :func:`same_lift` decides, so the rule lives in one place.
        """
        if target.exercise_id is None:
            names = {norm_name(target.name)}
        elif names is None:
            names = {norm_name(target.name)} | self.names_of(target.exercise_id)
        aliases = [Lift(target.exercise_id, name) for name in names]
        candidates = {}
        for name in names:
            for entry in self._by_name.get(name, []):
                candidates[entry[0]] = entry
        if target.exercise_id is not None:
            for entry in self._by_fk.get(target.exercise_id, []):
                candidates[entry[0]] = entry
        return [
            item
            for _, item_lift, item in sorted(candidates.values(), key=lambda e: e[0])
            if any(same_lift(item_lift, alias) for alias in aliases)
        ]


def representatives(lifts):
    """The distinct lifts in a history, one per row of a records-style list.

    Every catalog FK present is one lift, under the first name seen for it. A
    name-only lift stands alone only when no FK in the history carries its
    name; otherwise it is folded into each such FK (which matches it under
    that name, see :meth:`LiftIndex.matching`). Returned in first-seen order.
    """
    lifts = [item for item in lifts if item is not None]
    fk_names = {norm_name(item.name) for item in lifts if item.exercise_id is not None}
    seen_fks = set()
    seen_names = set()
    result = []
    for item in lifts:
        if item.exercise_id is not None:
            if item.exercise_id not in seen_fks:
                seen_fks.add(item.exercise_id)
                result.append(item)
            continue
        name = norm_name(item.name)
        if name not in fk_names and name not in seen_names:
            seen_names.add(name)
            result.append(item)
    return result
