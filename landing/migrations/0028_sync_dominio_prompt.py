from django.db import migrations


def sync_dominio_prompt(apps, schema_editor):
    """DOMINIO's own agent reads Client.system_prompt, seeded ONCE by 0005 from
    agent.DOMINIO_BUSINESS. Later edits to that constant (the full IT offer,
    2026-09-11) never reached the database, so the live agent kept telling
    visitors DOMINIO doesn't do on-site IT support. Re-sync it."""
    from landing.agent import DOMINIO_BUSINESS

    Client = apps.get_model('landing', 'Client')
    Client.objects.filter(slug='dominio').update(system_prompt=DOMINIO_BUSINESS)


class Migration(migrations.Migration):
    dependencies = [
        ('landing', '0027_usageevent'),
    ]
    operations = [
        migrations.RunPython(sync_dominio_prompt, migrations.RunPython.noop),
    ]
