"""Authorized read scopes for financial queries (V1.2).

A ``ReadScope`` says *whose* transactions a query may read. It can only be built by the
functions in this module, which apply ``apps.families.policies`` to the authenticated actor.
Selectors accept a scope instead of a raw user id, so there is no public way to read an
arbitrary user's data. Scopes are read-only: no service accepts them for mutations.

- SELF: the actor's own transactions (any family snapshot).
- FAMILY: transactions whose historical ``Transaction.family`` is the actor's family;
  only for the family ADMIN.
- FAMILY_MEMBER: one member's transactions *recorded in that family*
  (``user=member`` and ``family=family``); only for the family ADMIN, member resolved by
  name among the current members of the ADMIN's family.
"""

from dataclasses import dataclass, field

from django.db.models import QuerySet

from apps.accounts.models import User
from apps.families.models import Family
from apps.families.policies import can_view_family
from apps.families.selectors import (
    find_family_members_by_name,
    get_membership,
    member_display_name,
)

from .models import Transaction

SELF = "SELF"
FAMILY = "FAMILY"
FAMILY_MEMBER = "FAMILY_MEMBER"

_TOKEN = object()


class ScopeDenied(Exception):
    """Read scope not allowed or not resolvable. ``code`` drives the local reply."""

    NO_FAMILY = "NO_FAMILY"
    ADMIN_ONLY = "ADMIN_ONLY"
    MEMBER_NOT_FOUND = "MEMBER_NOT_FOUND"
    MEMBER_AMBIGUOUS = "MEMBER_AMBIGUOUS"

    def __init__(self, code: str, name: str | None = None):
        self.code = code
        self.name = name
        super().__init__(code)


@dataclass(frozen=True)
class ReadScope:
    kind: str
    actor: User
    family: Family | None = None
    member: User | None = None
    _token: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._token is not _TOKEN:
            raise TypeError("ReadScope must be created by apps.transactions.scopes functions.")

    def transactions(self) -> QuerySet[Transaction]:
        """Active transactions visible in this scope."""
        qs = Transaction.objects.active()
        if self.kind == SELF:
            return qs.filter(user=self.actor)
        if self.kind == FAMILY:
            return qs.filter(family=self.family)
        return qs.filter(family=self.family, user=self.member)

    @property
    def member_name(self) -> str | None:
        return member_display_name(self.member) if self.member else None


def own(actor: User) -> ReadScope:
    if not isinstance(actor, User) or actor.pk is None:
        raise TypeError("actor must be a saved User instance.")
    return ReadScope(SELF, actor, _token=_TOKEN)


def family(actor: User) -> ReadScope:
    return ReadScope(FAMILY, actor, family=_admin_family(actor), _token=_TOKEN)


def family_member(actor: User, member_name: str | None) -> ReadScope:
    target_family = _admin_family(actor)  # authorization first: never reveal names to members
    matches = find_family_members_by_name(family=target_family, name=member_name or "")
    if not matches:
        raise ScopeDenied(ScopeDenied.MEMBER_NOT_FOUND, member_name)
    if len(matches) > 1:
        raise ScopeDenied(ScopeDenied.MEMBER_AMBIGUOUS, member_name)
    return ReadScope(FAMILY_MEMBER, actor, family=target_family, member=matches[0], _token=_TOKEN)


def resolve(actor: User, kind: str | None, member_name: str | None = None) -> ReadScope:
    """Scope for a query. ``kind``/``member_name`` are untrusted hints (e.g. from the model)."""
    if kind == FAMILY:
        return family(actor)
    if kind == FAMILY_MEMBER:
        return family_member(actor, member_name)
    return own(actor)


def _admin_family(actor: User) -> Family:
    """The family the actor administers *now* (re-checked on every query, never cached)."""
    membership = get_membership(user=actor)
    if membership is None:
        raise ScopeDenied(ScopeDenied.NO_FAMILY)
    if not can_view_family(actor=actor, family=membership.family):
        raise ScopeDenied(ScopeDenied.ADMIN_ONLY)
    return membership.family
