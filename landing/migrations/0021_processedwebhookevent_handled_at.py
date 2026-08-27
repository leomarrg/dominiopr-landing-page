from django.db import migrations, models


def mark_existing_as_handled(apps, schema_editor):
    """Rows that already exist were, under the old scheme, only written after
    the handler had run — so they are genuinely handled. Leaving them NULL would
    make every one of them eligible for reprocessing."""
    ProcessedWebhookEvent = apps.get_model('landing', 'ProcessedWebhookEvent')
    ProcessedWebhookEvent.objects.filter(handled_at__isnull=True).update(
        handled_at=models.F('created_at'))


class Migration(migrations.Migration):

    dependencies = [
        ('landing', '0020_selfserve_provisioning'),
    ]

    operations = [
        migrations.AddField(
            model_name='processedwebhookevent',
            name='handled_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunPython(mark_existing_as_handled, migrations.RunPython.noop),
    ]
