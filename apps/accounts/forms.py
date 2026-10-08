from django import forms
from django.contrib.auth.forms import AdminUserCreationForm as BaseUserCreationForm
from django.contrib.auth.forms import UserChangeForm as BaseUserChangeForm

from common.phone import InvalidPhoneNumber, normalize_phone

from .models import User


class PhoneField(forms.CharField):
    """Normalize before ModelForm length and uniqueness validation, using the domain helper."""

    def to_python(self, value):
        value = super().to_python(value)
        if not value:
            return None
        try:
            return normalize_phone(value)
        except InvalidPhoneNumber as exc:
            raise forms.ValidationError(str(exc)) from exc


class UserCreationForm(BaseUserCreationForm):
    phone_number = PhoneField(label="Telefone", required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["usable_password"].initial = "false"
        self.fields[
            "usable_password"
        ].help_text = (
            "Usuários do WhatsApp não precisam de senha. Habilite somente para login no Django."
        )

    class Meta(BaseUserCreationForm.Meta):
        model = User
        fields = ("username", "phone_number", "first_name", "last_name", "email")


class UserChangeForm(BaseUserChangeForm):
    phone_number = PhoneField(label="Telefone", required=False)

    class Meta(BaseUserChangeForm.Meta):
        model = User
        fields = "__all__"
