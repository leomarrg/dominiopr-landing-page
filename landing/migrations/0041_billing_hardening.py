# Phase C billing hardening (audit follow-up):
#   * Conversation (client, started_at) index - month usage / bill_overage
#     filter by tenant and start month.
#   * AuditEvent partial unique (client, action, target) for 'allowance.*' -
#     one 80% / 100% warning per client, bucket and month even under a race.
# Both are additive; on SQLite/Postgres they build without rewriting rows.

from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('landing', '0040_internal_assistant'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddIndex(
            model_name='conversation',
            index=models.Index(fields=['client', 'started_at'], name='conv_client_started_idx'),
        ),
        migrations.AddConstraint(
            model_name='auditevent',
            constraint=models.UniqueConstraint(condition=models.Q(('action__startswith', 'allowance.')), fields=('client', 'action', 'target'), name='uniq_allowance_audit_per_month'),
        ),
    ]
