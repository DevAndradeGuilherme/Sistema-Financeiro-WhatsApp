from django.db import transaction

from .models import User


@transaction.atomic
def create_user(
    *,
    username: str,
    phone_number: str | None = None,
    first_name: str = "",
    last_name: str = "",
    email: str = "",
    password: str | None = None,
) -> User:
    """Create a user, normalizing the phone number. Without password the user cannot log in."""
    user = User(
        username=username,
        phone_number=phone_number,
        first_name=first_name,
        last_name=last_name,
        email=email,
    )
    if password:
        user.set_password(password)
    else:
        user.set_unusable_password()
    user.full_clean()
    user.save()
    return user
