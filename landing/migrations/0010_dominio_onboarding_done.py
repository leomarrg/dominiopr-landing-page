from django.db import migrations


def mark_dominio_onboarded(apps, schema_editor):
    """DOMINIO is client #1 of its own product; it must never auto-onboard
    itself (no scoped non-staff login, no 'your agent is live' email to its own
    inbox). Marking onboarding_sent=True keeps the activation block from firing
    for the dominio slug."""
    Client = apps.get_model('landing', 'Client')
    Client.objects.filter(slug='dominio').update(onboarding_sent=True)


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('landing', '0009_membership'),
    ]

    operations = [
        migrations.RunPython(mark_dominio_onboarded, noop),
    ]
