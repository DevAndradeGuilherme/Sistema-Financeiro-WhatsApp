from common.phone import InvalidPhoneNumber, normalize_phone

from .models import User


def get_active_user_by_phone(raw_phone: str) -> User | None:
    """Resolve an untrusted phone string to an active user, or None."""
    try:
        phone = normalize_phone(raw_phone)
    except InvalidPhoneNumber:
        return None
    return User.objects.filter(phone_number=phone, is_active=True).first()
