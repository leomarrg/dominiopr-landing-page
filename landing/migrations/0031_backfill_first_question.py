"""Backfill Conversation.first_question from the stored position-0 user turn.

Clinic-mode clients are skipped (their first_question stays blank by design).
Idempotent: only touches rows whose first_question is still blank, in batches.
"""
from django.db import migrations

BATCH = 500


def backfill(apps, schema_editor):
    Conversation = apps.get_model('landing', 'Conversation')
    ChatMessage = apps.get_model('landing', 'ChatMessage')
    rows = (ChatMessage.objects
            .filter(position=0, role='user',
                    conversation__first_question='',
                    conversation__client__clinic_mode=False)
            .order_by('conversation_id')
            .values_list('conversation_id', 'content'))
    batch = []
    for conv_id, content in rows.iterator(chunk_size=BATCH):
        text = (content or '').strip()[:300]
        if not text:
            continue
        batch.append(Conversation(pk=conv_id, first_question=text))
        if len(batch) >= BATCH:
            Conversation.objects.bulk_update(batch, ['first_question'])
            batch = []
    if batch:
        Conversation.objects.bulk_update(batch, ['first_question'])


class Migration(migrations.Migration):

    dependencies = [
        ('landing', '0030_conversation_tagging'),
    ]

    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
