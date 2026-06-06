from django.conf import settings
from django.db import migrations


def seed_dominio(apps, schema_editor):
    """Create DOMINIO as client #1 and link existing leads to it."""
    from landing.agent import DOMINIO_BUSINESS

    Client = apps.get_model('landing', 'Client')
    ContactSubmission = apps.get_model('landing', 'ContactSubmission')

    notify = getattr(settings, 'CONTACT_NOTIFY_EMAIL', '') or 'creatudominiopr@gmail.com'
    client, _ = Client.objects.get_or_create(
        slug='dominio',
        defaults={
            'name': 'DOMINIO',
            'system_prompt': DOMINIO_BUSINESS,
            'greeting': '',
            'notify_email': notify,
            'allowed_origins': 'dominiopr.com, www.dominiopr.com, localhost:8000, 127.0.0.1:8000',
            'primary_color': '#34d6c8',
            'is_active': True,
        },
    )
    ContactSubmission.objects.filter(client__isnull=True).update(client=client)


def unseed(apps, schema_editor):
    Client = apps.get_model('landing', 'Client')
    Client.objects.filter(slug='dominio').delete()


class Migration(migrations.Migration):
    dependencies = [
        ('landing', '0004_client_contactsubmission_client'),
    ]
    operations = [
        migrations.RunPython(seed_dominio, unseed),
    ]
