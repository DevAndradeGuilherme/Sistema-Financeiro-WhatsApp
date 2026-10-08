from django import forms
from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Count, Prefetch
from django.http import Http404, HttpResponseRedirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils.html import format_html, format_html_join

from apps.accounts.models import User

from .models import Family, FamilyMember
from .services import (
    add_family_member,
    create_family,
    remove_family_member,
    require_family_operator,
    transfer_family_admin,
)


class FamilyCreationForm(forms.Form):
    name = forms.CharField(label="Nome", max_length=120)
    admin_user = forms.ModelChoiceField(
        label="ADMIN inicial",
        queryset=User.objects.none(),
        help_text="Papel familiar; não concede acesso ao Django Admin.",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["admin_user"].queryset = User.objects.filter(
            family_membership__isnull=True
        ).order_by("username")


class MemberOperationForm(forms.Form):
    user = forms.ModelChoiceField(label="Usuário", queryset=User.objects.none())

    def __init__(self, *args, family, operation, **kwargs):
        super().__init__(*args, **kwargs)
        users = User.objects.all()
        if operation == "add-member":
            users = users.filter(family_membership__isnull=True)
        else:
            users = users.filter(family_membership__family=family)
            if operation == "transfer-admin":
                users = users.filter(family_membership__role=FamilyMember.Role.MEMBER)
                self.fields["user"].label = "Novo ADMIN"
        self.fields["user"].queryset = users.order_by("username")


@admin.register(Family)
class FamilyAdmin(admin.ModelAdmin):
    list_display = ("name", "current_admin", "member_count", "created_at", "updated_at")
    search_fields = ("name", "memberships__user__username", "memberships__user__first_name")
    readonly_fields = (
        "current_admin",
        "member_count",
        "members",
        "operations",
        "created_at",
        "updated_at",
    )
    fields = (
        "name",
        "current_admin",
        "member_count",
        "members",
        "operations",
        "created_at",
        "updated_at",
    )

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .annotate(total_members=Count("memberships", distinct=True))
            .prefetch_related(
                Prefetch(
                    "memberships",
                    queryset=FamilyMember.objects.select_related("user").order_by(
                        "role", "user__username"
                    ),
                )
            )
        )

    @admin.display(description="ADMIN atual")
    def current_admin(self, obj):
        return next((m.user for m in obj.memberships.all() if m.is_admin), "—")

    @admin.display(description="membros", ordering="total_members")
    def member_count(self, obj):
        return obj.total_members

    @admin.display(description="membros e papéis")
    def members(self, obj):
        return format_html_join(
            "",
            "<p>{} ({}) — {}</p>",
            (
                (m.user.get_full_name() or m.user.username, m.user.username, m.role)
                for m in obj.memberships.all()
            ),
        )

    @admin.display(description="gestão de membros")
    def operations(self, obj):
        return format_html_join(
            " | ",
            '<a href="{}">{}</a>',
            (
                (reverse(f"admin:families_family_{operation}", args=[obj.pk]), label)
                for operation, label in self.operation_labels.items()
            ),
        )

    operation_labels = {
        "add-member": "Adicionar MEMBER",
        "remove-member": "Remover MEMBER",
        "transfer-admin": "Transferir administração",
    }
    operation_permissions = {
        "add-member": "families.add_familymember",
        "remove-member": "families.delete_familymember",
        "transfer-admin": "families.change_familymember",
    }

    def get_urls(self):
        return [
            path(
                f"<int:object_id>/{operation}/",
                self.admin_site.admin_view(self.member_operation_view),
                {"operation": operation},
                name=f"families_family_{operation}",
            )
            for operation in self.operation_labels
        ] + super().get_urls()

    def add_view(self, request, form_url="", extra_context=None):
        if not (
            self.has_add_permission(request) and request.user.has_perm("families.add_familymember")
        ):
            raise PermissionDenied
        form = FamilyCreationForm(request.POST if request.method == "POST" else None)
        if request.method == "POST" and form.is_valid():
            try:
                with transaction.atomic():
                    family = create_family(
                        name=form.cleaned_data["name"], admin=form.cleaned_data["admin_user"]
                    )
                    self.log_addition(
                        request,
                        family,
                        f"Família criada; ADMIN user_id={form.cleaned_data['admin_user'].pk}.",
                    )
            except ValidationError as exc:
                form.add_error(None, "; ".join(exc.messages))
            else:
                self.message_user(
                    request, "Família criada com exatamente um ADMIN.", messages.SUCCESS
                )
                return HttpResponseRedirect(
                    reverse("admin:families_family_change", args=[family.pk])
                )
        return self.operation_response(request, form, "Criar família")

    def member_operation_view(self, request, object_id, operation):
        family = self.get_object(request, str(object_id))
        if family is None:
            raise Http404
        if not self.has_change_permission(request, family):
            raise PermissionDenied
        require_family_operator(
            actor=request.user, membership_permission=self.operation_permissions[operation]
        )
        form = MemberOperationForm(
            request.POST if request.method == "POST" else None,
            family=family,
            operation=operation,
        )
        if request.method == "POST" and form.is_valid():
            user = form.cleaned_data["user"]
            try:
                # The operation AND its Admin LogEntry commit or roll back together.
                with transaction.atomic():
                    if operation == "transfer-admin":
                        previous_id = transfer_family_admin(
                            actor=request.user, family=family, new_admin_user=user
                        )
                        message = (
                            f"Administração transferida: user_id={previous_id} → user_id={user.pk}."
                        )
                    elif operation == "add-member":
                        add_family_member(family=family, user=user)
                        message = f"MEMBER adicionado: user_id={user.pk}."
                    else:
                        remove_family_member(actor=request.user, family=family, user=user)
                        message = f"MEMBER removido: user_id={user.pk}."
                    self.log_change(request, family, message)
            except ValidationError as exc:
                form.add_error(None, "; ".join(exc.messages))
            else:
                self.message_user(request, "Operação concluída.", messages.SUCCESS)
                return HttpResponseRedirect(
                    reverse("admin:families_family_change", args=[family.pk])
                )
        return self.operation_response(request, form, self.operation_labels[operation], family)

    def operation_response(self, request, form, title, family=None):
        return TemplateResponse(
            request,
            "admin/families/operation.html",
            {
                **self.admin_site.each_context(request),
                "opts": self.model._meta,
                "title": title,
                "form": form,
                "original": family,
                "media": self.media + form.media,
            },
        )

    def has_delete_permission(self, request, obj=None):
        # Family is a historical identity; no cascade of memberships via the Admin.
        return False


@admin.register(FamilyMember)
class FamilyMemberAdmin(admin.ModelAdmin):
    """Diagnostics only. Membership mutations go through FamilyAdmin and services."""

    list_display = ("user", "family", "role", "created_at")
    list_filter = ("role",)
    search_fields = ("user__username", "user__phone_number", "family__name")
    list_select_related = ("user", "family")
    readonly_fields = ("user", "family", "role", "created_at", "management_hint")

    @admin.display(description="gestão")
    def management_hint(self, obj):
        return format_html(
            "Transfira a administração antes de remover este usuário. "
            '<a href="{}">Gerenciar pela família</a>',
            reverse("admin:families_family_change", args=[obj.family_id]),
        )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
