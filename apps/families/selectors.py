import unicodedata

from apps.accounts.models import User

from .models import Family, FamilyMember


def get_membership(*, user: User) -> FamilyMember | None:
    return FamilyMember.objects.select_related("family").filter(user=user).first()


def member_display_name(user: User) -> str:
    """Name shown in replies: full name, else first name, else username. Never the phone."""
    full = " ".join(part for part in (user.first_name, user.last_name) if part).strip()
    return full or user.username


def find_family_members_by_name(*, family: Family, name: str) -> list[User]:
    """Current members of ``family`` whose name matches ``name`` (accent/case-insensitive).

    Matches the full name, the first name or the username exactly. Never searches outside
    the given family. Returns every match so callers can detect ambiguity.
    """
    wanted = normalize_name(name)
    if not wanted:
        return []
    members = User.objects.filter(family_membership__family=family, is_active=True).order_by("pk")
    matches = []
    for member in members:
        candidates = {
            normalize_name(member_display_name(member)),
            normalize_name(member.first_name),
            normalize_name(member.username),
        }
        if wanted in candidates - {""}:
            matches.append(member)
    return matches


def other_member_names(*, user: User) -> set[str]:
    """Normalized first and full names of the other members of ``user``'s current family.

    A member without a first name is known by the username (``member_display_name``).
    """
    membership = get_membership(user=user)
    if membership is None:
        return set()
    names = set()
    for member in User.objects.filter(family_membership__family=membership.family).exclude(
        pk=user.pk
    ):
        names.add(normalize_name(member.first_name))
        names.add(normalize_name(f"{member.first_name} {member.last_name}"))
        if not member.first_name:
            names.add(normalize_name(member.username))
    return {name for name in names if len(name) >= 2}


def normalize_name(value: str | None) -> str:
    """Lowercase, accent-free, single-spaced (for name matching)."""
    text = unicodedata.normalize("NFKD", value or "")
    text = "".join(char for char in text if not unicodedata.combining(char))
    return " ".join(text.lower().split())
