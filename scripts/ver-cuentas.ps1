$raiz = Split-Path -Parent $PSScriptRoot
Set-Location $raiz
docker compose exec -T core python manage.py shell -c @"
from django.contrib.auth import get_user_model
from apps.tenancy.models import Membership, Store, Organization
U = get_user_model()
for u in U.objects.all().order_by('date_joined'):
    ms = Membership.objects.filter(user=u).select_related('store')
    roles = ', '.join(f'{m.role}@{m.store.name}' for m in ms)
    print(f'{u.email:<40} admin={u.is_platform_admin} activo={u.is_active} | {roles}')
print()
print('Tiendas:', [(str(s.id)[:8], s.name, s.code) for s in Store.objects.all()])
"@
