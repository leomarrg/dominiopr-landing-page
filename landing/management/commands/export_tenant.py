"""Phase 7: export everything one tenant owns as a zip of JSON + CSV files.

    python manage.py export_tenant --client <slug> --out <dir>

Writes <dir>/<slug>-export-YYYYMMDD.zip (an existing file of that name is
replaced) and prints the path and per-table row counts. Staff runs it over
SSH; the same zip is downloadable from the control center client page. No
secrets (widget token, CMS access notes) and no DOMINIO cost figures are
included -- see landing.tenant_export.
"""
import os

from django.core.management.base import BaseCommand, CommandError

from landing import tenant_export
from landing.models import AuditEvent, Client


class Command(BaseCommand):
    help = 'Export one tenant (leads, conversations, bookings, knowledge...) as a zip.'

    def add_arguments(self, parser):
        parser.add_argument('--client', required=True, help='Client slug.')
        parser.add_argument('--out', required=True, help='Directory to write the zip into.')

    def handle(self, *args, **options):
        slug = options['client'].strip()
        client = Client.objects.filter(slug=slug).first()
        if client is None:
            raise CommandError(f'No existe un cliente con slug "{slug}".')
        out_dir = os.path.abspath(options['out'])
        if not os.path.isdir(out_dir):
            raise CommandError(f'El directorio {out_dir} no existe.')

        path = os.path.join(out_dir, tenant_export.filename_for(client))
        tmp = path + '.partial'
        try:
            with open(tmp, 'wb') as fh:
                counts = tenant_export.write_export(client, fh)
            os.replace(tmp, path)  # never leave a half-written zip under the real name
        except Exception:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise
        # The export holds every lead of the tenant: leave a trail (no user over SSH).
        AuditEvent.objects.create(client=client, action='tenant.export',
                                  target=client.slug, result='cli')

        self.stdout.write(self.style.SUCCESS(f'Exportado: {path}'))
        for name, n in counts.items():
            self.stdout.write(f'  {name}: {n}')
