# Phase E: internal assistant for employees.
#   * KnowledgeSource.audience - which assistant a source feeds (public widget,
#     internal assistant, or both). Existing rows stay 'public'.
#   * Membership.role gains 'employee' (internal assistant only).

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('landing', '0039_receptionist'),
    ]

    operations = [
        migrations.AddField(
            model_name='knowledgesource',
            name='audience',
            field=models.CharField(
                choices=[('public', 'Público (widget del sitio)'),
                         ('internal', 'Solo equipo interno'),
                         ('both', 'Ambos')],
                default='public', max_length=8, verbose_name='Audiencia',
                help_text=('Público: solo el agente del sitio web. Solo equipo interno: solo el '
                           'asistente interno de los empleados (nunca llega al widget). '
                           'Ambos: los dos.')),
        ),
        migrations.AlterField(
            model_name='membership',
            name='role',
            field=models.CharField(
                choices=[('org_admin', 'Administrador'), ('agent', 'Agente'),
                         ('employee', 'Empleado')],
                default='org_admin', max_length=12),
        ),
    ]
