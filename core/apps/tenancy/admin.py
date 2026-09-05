"""Registro de multi-tenancy en el admin interno."""

from __future__ import annotations

from django.contrib import admin

from apps.tenancy.models import Membership, Organization, Store


class StoreInline(admin.TabularInline):
    model = Store
    extra = 0
    fields = ("name", "code", "city", "is_active")


@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "tax_id", "is_active", "created_at")
    list_filter = ("is_active",)
    search_fields = ("name", "slug", "tax_id")
    prepopulated_fields = {"slug": ("name",)}
    inlines = [StoreInline]


class MembershipInline(admin.TabularInline):
    model = Membership
    extra = 0
    autocomplete_fields = ("user",)
    fields = ("user", "role", "is_active", "is_default")


@admin.register(Store)
class StoreAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "organization", "city", "is_active")
    list_filter = ("is_active", "organization", "state")
    search_fields = ("name", "code", "city")
    inlines = [MembershipInline]


@admin.register(Membership)
class MembershipAdmin(admin.ModelAdmin):
    list_display = ("user", "store", "role", "is_active", "is_default")
    list_filter = ("role", "is_active")
    search_fields = ("user__email", "store__name")
    autocomplete_fields = ("user", "store")
