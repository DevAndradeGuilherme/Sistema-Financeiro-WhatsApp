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
    wanted = _normalize(name)
    if not wanted:
        return []
    members = User.objects.filter(family_membership__family=family, is_active=True).order_by("pk")
    matches = []
    for member in members:
        candidates = {
            _normalize(member_display_name(member)),
            _normalize(member.first_name),
            _normalize(member.username),
        }
        if wanted in candidates - {""}:
            matches.append(member)
    return matches


def _normalize(value: str | None) -> str:
    text = unicodedata.normalize("NFKD", value or "")
    text = "".join(char for char in text if not unicodedata.combining(char))
    return " ".join(text.lower().split())
