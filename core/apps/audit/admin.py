"""Auditoria en el admin: solo lectura.

``has_add_permission`` y ``has_change_permission`` devuelven False. Un registro
de auditoria editable desde un panel no sirve como evidencia.
"""

from __future__ import annotations

from django.contrib import admin

from apps.audit.models import AuditEvent


@admin.register(AuditEvent)
class AuditEventAdmin(admin.ModelAdmin):
    list_display = ("created_at", "action", "actor_email", "store", "new_state")
    list_filter = ("action", "created_at")
    search_fields = ("actor_email", "object_id", "correlation_id")
    date_hierarchy = "created_at"
    readonly_fields = [f.name for f in AuditEvent._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
