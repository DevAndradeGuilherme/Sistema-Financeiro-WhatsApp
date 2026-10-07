from datetime import date

from django.utils import timezone


def today_local() -> date:
    """Today's date in the business timezone (settings.TIME_ZONE), not the server's UTC date."""
    return timezone.localdate()
