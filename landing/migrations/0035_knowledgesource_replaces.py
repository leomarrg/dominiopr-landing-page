# Phase 6: knowledge versioning. Nullable FK, no backfill: existing sources
# simply start their own lineage. Adding a nullable column is metadata-only on
# PostgreSQL and a cheap table rebuild on SQLite (tens of rows per tenant).

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('landing', '0034_contactsubmission_email_verified_at'),
    ]

    operations = [
        migrations.AddField(
            model_name='knowledgesource',
            name='replaces',
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name='revisions', to='landing.knowledgesource'),
        ),
    ]
