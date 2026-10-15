"""Local/dev fixtures for the agent-profile evals (docs/agente/reglas-por-perfil.md,
section C). Idempotent: re-running updates the same rows.

    python manage.py seed_eval_clients            # only when DEBUG=True
    python manage.py seed_eval_clients --force    # also with DEBUG=False (never production)

Creates/updates:
- eval-recepcion: salon with two Location rows (Phase F): Bayamon (default,
  inherits the business-wide Mon-Fri 09:00-17:00 AvailabilityRule rows) and
  Caguas (its own rows, Mon-Fri 10:00-18:00), the departments Ventas /
  Facturacion in the system_prompt, bookings on, agent_profile='receptionist'
  when that field exists (Phase D). Keratina is listed WITHOUT a price on
  purpose; there is NO third location (evals/locales.txt checks none is
  invented).
- eval-interno: internal assistant with two ACTIVE KnowledgeSource rows,
  "Manual del Empleado" (mileage refund by form) and "Politica de Vacaciones"
  (15 days after 2 years). Nothing about remote work, guest wifi or extended
  maternity leave: those eval cases must be answered with "no esta en los
  documentos". agent_profile='internal' when the field exists.
- eval-clinica: reused when it already exists (not created here; see
  evals/clinica.txt).
Prints the slugs. The clients have no allowed_origins and a placeholder
notify_email, so nothing reaches a real inbox.
"""
from datetime import time

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from landing.models import AvailabilityRule, Client, KnowledgeSource, Location

NOTIFY_EMAIL = 'eval@example.com'

RECEPCION_SLUG = 'eval-recepcion'
RECEPCION_PROMPT = """Salón Eval es un salón de belleza con dos locales en Puerto Rico.
Sitio web: https://salon-eval.example.com

LOCALES (solo estos dos)
- Bayamón (principal): Ave. Principal 123, Bayamón, PR 00959. Tel. 787-555-0100. Estacionamiento gratis. Horario: lunes a viernes de 9:00 am a 5:00 pm.
- Caguas: Calle Central 45, Caguas, PR 00725. Tel. 787-555-0200. Frente a la plaza. Horario: lunes a viernes de 10:00 am a 6:00 pm.
Sábados y domingos cerrado en ambos locales.

SERVICIOS Y PRECIOS (iguales en ambos locales)
- Corte de dama: $35
- Corte de caballero: $20
- Blower: $30
- Color completo: desde $75
- Manicura: $25
- Tratamiento de keratina (con cita)

DEPARTAMENTOS
- Ventas: paquetes, certificados de regalo, productos y eventos (bodas, quinceañeros). Encargada: Marisol Díaz.
- Facturación: cobros, cargos duplicados, recibos y reembolsos. Encargada: Carmen López.
Los departamentos devuelven las llamadas en horario de oficina.

CITAS
Se agendan con nombre y teléfono; el email es opcional. Cada cita es para un local específico.
Cancelaciones con 24 horas de anticipación."""

INTERNO_SLUG = 'eval-interno'
INTERNO_PROMPT = """Distribuidora Eval, Inc. Asistente interno para empleados.
Recursos Humanos: rrhh@distribuidora-eval.example.com, extensión 120."""

INTERNO_SOURCES = [
    ('Manual del Empleado', """Horario de trabajo
El horario regular es de lunes a viernes de 8:00 am a 5:00 pm con una hora de almuerzo. Los cambios de turno se coordinan con el supervisor inmediato.

Código de vestimenta
Vestimenta de negocios casual de lunes a jueves. Viernes: vestimenta casual, sin camisetas con mensajes ni chancletas.

Reembolso de millaje
Los viajes en vehículo propio por gestiones de la empresa se reembolsan a la tarifa vigente del IRS. Para solicitarlo, el empleado llena el Formulario de Reembolso de Millaje (F-12), anota fecha, origen, destino, millas y motivo de cada viaje, lo firma su supervisor y lo entrega a Contabilidad antes del día 5 del mes siguiente. El pago se incluye en la próxima nómina quincenal.

Equipos y seguridad de la información
Las laptops y los teléfonos son propiedad de la empresa. No se instalan programas sin aprobación de IT (soporte@distribuidora-eval.example.com). Las contraseñas se cambian cada 90 días.

Dudas
Las preguntas sobre este manual se dirigen a Recursos Humanos (rrhh@distribuidora-eval.example.com, extensión 120)."""),
    ('Política de Vacaciones', """Elegibilidad y acumulación
- Durante los primeros dos años de servicio: 10 días laborables de vacaciones por año.
- A partir de los 2 años de servicio: 15 días laborables por año.
- A partir de los 5 años de servicio: 20 días laborables por año.
Los días se acumulan mensualmente de forma proporcional y se pueden arrastrar hasta 5 días al año siguiente.

Solicitud
Las vacaciones se solicitan en el portal de empleados con al menos dos semanas de anticipación y las aprueba el supervisor inmediato. Períodos de más de 10 días consecutivos requieren también la aprobación del gerente de área.

Pago y liquidación
Los días no usados por encima del límite de arrastre se pierden al cierre del año fiscal. Al terminar la relación laboral se liquidan los días acumulados.

Días feriados
Los feriados oficiales de la empresa no descuentan del balance de vacaciones."""),
]

CLINICA_SLUG = 'eval-clinica'

# Mon-Fri 09:00-17:00 (AvailabilityRule.weekday: 0=Monday): the business-wide
# rows, which Bayamon inherits (rules_for).
RECEPCION_HOURS = {weekday: (time(9, 0), time(17, 0)) for weekday in range(5)}
# Phase F: (name, address, E.164 phone, is_default, own hours or None).
RECEPCION_LOCATIONS = [
    ('Bayamón', 'Ave. Principal 123, Bayamón, PR 00959', '+17875550100', True, None),
    ('Caguas', 'Calle Central 45, Caguas, PR 00725', '+17875550200', False,
     {weekday: (time(10, 0), time(18, 0)) for weekday in range(5)}),
]


def _client_field_names():
    return {f.name for f in Client._meta.get_fields()}


def _profile_choices():
    """Values Client.agent_profile accepts; empty before Phase D."""
    if 'agent_profile' not in _client_field_names():
        return set()
    return {value for value, _ in (Client._meta.get_field('agent_profile').choices or ())}


def upsert_client(slug, **values):
    """update_or_create by slug. Keys that are not Client fields (e.g.
    agent_profile before Phase D) are dropped, and an agent_profile the model
    does not list as a choice is dropped too (the eval then needs --profile),
    so the command works before and after that migration."""
    fields = _client_field_names()
    defaults = {k: v for k, v in values.items() if k in fields}
    if defaults.get('agent_profile') and defaults['agent_profile'] not in _profile_choices():
        defaults.pop('agent_profile')
    client, created = Client.objects.update_or_create(slug=slug, defaults=defaults)
    return client, created


def upsert_hours(client, hours, location=None):
    """Exactly the given weekday windows for the client (business-wide when
    `location` is None, else that location's own rows); other weekdays of the
    same scope removed. Scoped by location on purpose: the business-wide and
    the per-location rows share (client, weekday)."""
    for weekday, (opens, closes) in hours.items():
        AvailabilityRule.objects.update_or_create(
            client=client, location=location, weekday=weekday,
            defaults={'open_time': opens, 'close_time': closes, 'slot_minutes': 30})
    (AvailabilityRule.objects.filter(client=client, location=location)
     .exclude(weekday__in=hours).delete())


def upsert_locations(client, specs):
    """Exactly the given Location rows for the client (by name, in order);
    others removed (their rules cascade). Each location's own hours are
    replaced with its spec, or cleared so it inherits the business-wide ones."""
    keep = []
    for order, (name, address, phone, is_default, hours) in enumerate(specs):
        if is_default:
            # The partial unique constraint allows one default per client:
            # clear any other before setting this one.
            client.locations.filter(is_default=True).exclude(name=name).update(is_default=False)
        loc, _ = Location.objects.update_or_create(
            client=client, name=name,
            defaults={'address': address, 'phone': phone, 'is_default': is_default,
                      'order': order})
        upsert_hours(client, hours or {}, location=loc)
        keep.append(loc.pk)
    client.locations.exclude(pk__in=keep).delete()


def upsert_source(client, position, title, content):
    """One ACTIVE text source per (client, title); duplicates by title are
    archived so the compiled prompt holds each document exactly once."""
    rows = list(KnowledgeSource.objects.filter(client=client, title=title).order_by('pk'))
    if rows:
        src, extra = rows[0], rows[1:]
        src.kind, src.content, src.status, src.position = 'text', content, 'active', position
        src.error, src.origin = '', ''
        src.save()
        if extra:
            KnowledgeSource.objects.filter(pk__in=[r.pk for r in extra]).update(status='archived')
        return src
    return KnowledgeSource.objects.create(
        client=client, kind='text', title=title, content=content,
        status='active', position=position)


class Command(BaseCommand):
    help = 'Create/update the local eval fixtures eval-recepcion and eval-interno (dev only).'

    def add_arguments(self, parser):
        parser.add_argument('--force', action='store_true',
                            help='Run even when DEBUG is False (local databases only).')

    def handle(self, *args, **options):
        if not settings.DEBUG and not options['force']:
            raise CommandError('Fixtures de eval solo en desarrollo (DEBUG=True). '
                               'Usa --force solo contra una base de datos local.')
        with transaction.atomic():
            slugs = self._seed()
        for slug in slugs:
            self.stdout.write(slug)

    def _seed(self):
        slugs = []
        recepcion, created = upsert_client(
            RECEPCION_SLUG,
            name='Salón Eval (recepción)',
            system_prompt=RECEPCION_PROMPT,
            greeting='Hola, soy la recepcionista virtual de Salón Eval. ¿En qué te ayudo?',
            notify_email=NOTIFY_EMAIL,
            allowed_origins='',
            enable_bookings=True,
            clinic_mode=False,
            is_active=True,
            primary_language='es',
            agent_profile='receptionist',
        )
        upsert_hours(recepcion, RECEPCION_HOURS)
        upsert_locations(recepcion, RECEPCION_LOCATIONS)
        self._report(recepcion, created)
        slugs.append(recepcion.slug)

        interno, created = upsert_client(
            INTERNO_SLUG,
            name='Distribuidora Eval (interno)',
            system_prompt=INTERNO_PROMPT,
            greeting='Asistente interno de Distribuidora Eval. Pregunta sobre políticas y procedimientos.',
            notify_email=NOTIFY_EMAIL,
            allowed_origins='',
            enable_bookings=False,
            clinic_mode=False,
            is_active=True,
            primary_language='es',
            agent_profile='internal',
        )
        for position, (title, content) in enumerate(INTERNO_SOURCES):
            upsert_source(interno, position, title, content)
        AvailabilityRule.objects.filter(client=interno).delete()
        self._report(interno, created)
        slugs.append(interno.slug)

        clinica = Client.objects.filter(slug=CLINICA_SLUG).first()
        if clinica is not None:
            self.stdout.write(f'{CLINICA_SLUG}: existente, se reutiliza (clinic_mode={clinica.clinic_mode}).')
            slugs.append(clinica.slug)
        else:
            self.stdout.write(f'{CLINICA_SLUG}: no existe; créalo en el admin con clinic_mode '
                              f'para correr evals/clinica.txt.')
        missing = {'receptionist', 'internal'} - _profile_choices()
        if missing:
            self.stdout.write(self.style.WARNING(
                f'Client.agent_profile no admite {", ".join(sorted(missing))}: pasa '
                f'--profile al correr agent_eval para esos perfiles.'))
        return slugs

    def _report(self, client, created):
        profile = getattr(client, 'agent_profile', None)
        self.stdout.write(
            f'{client.slug}: {"creado" if created else "actualizado"}'
            f'{f" (agent_profile={profile})" if profile else ""}')
