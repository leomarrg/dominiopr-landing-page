# DOMINIO

Sitio y plataforma de DOMINIO, estudio de software y tecnología en Puerto Rico
(https://dominiopr.com). Un solo proyecto Django sirve:

- el sitio público en español (servicios, contacto, `/get-started/`, privacidad y términos);
- **DOMINIO Chat 24/7**: agente de IA multi-tenant que cada negocio cliente instala en su
  página con `/widget.js`, que también atiende por WhatsApp, captura leads, toma citas y
  deja recados;
- el **dashboard del cliente** (CRM de leads, conversaciones, citas, reportes, usuarios,
  auditoría, facturación) y el **asistente interno** para sus empleados;
- el **centro de control** de DOMINIO (staff): agentes, conocimiento, costos y salud;
- el cobro con Stripe (checkout, webhook, portal, cobro de excedentes).

## Stack

- Python 3.10 en el servidor (3.12 sirve en local), Django 5.2, SQLite (WAL).
- Anthropic (SDK `anthropic`) para el agente, la ingesta de conocimiento y el etiquetado.
- Stripe (API REST), Meta WhatsApp Cloud API, Twilio (solo SMS), hCaptcha, Google Analytics 4
  (solo con consentimiento).
- Producción: Lightsail (414 MB RAM + swap), nginx + gunicorn gthread, whitenoise para
  estáticos, despliegue por GitHub Actions al hacer push a `main`.

## Correrlo en local

```bash
python -m venv venv
venv/Scripts/pip install -r requirements.txt      # Windows (Git Bash); en Linux/macOS: venv/bin/pip
cp .env.example .env                              # rellena al menos DJANGO_SECRET_KEY y DJANGO_DEBUG=True
venv/Scripts/python manage.py migrate
venv/Scripts/python manage.py createsuperuser     # login de staff para /dashboard/ y /dashboard/control/
venv/Scripts/python manage.py seed_eval_clients   # datos demo: negocios eval-recepcion y eval-interno
venv/Scripts/python manage.py runserver 127.0.0.1:8765
```

- Puerto **8765**: el 8000 lo usa otro proyecto en esta máquina.
- Sin `ANTHROPIC_API_KEY` el sitio funciona y el chat no se muestra; sin las llaves de Stripe
  `/get-started/` cae al flujo de "te escribimos".
- `.env` es solo local y nunca se commitea; `.env.example` documenta cada variable.
- Usuarios de prueba de la base local: `docs/dev/usuarios-prueba-local.md`.

## Tests

Siempre en serie (en Windows `--parallel` no es estable) y sin llamar a ninguna API real:

```bash
venv/Scripts/python manage.py test landing social --noinput
venv/Scripts/python manage.py test landing.tests_leads landing.tests_crm --noinput   # por módulo
venv/Scripts/python manage.py check
venv/Scripts/python manage.py makemigrations --check --dry-run
```

## Evals del agente

`evals/` tiene los casos por perfil (web, recepcionista, interno, WhatsApp, multi-local,
inyección). Gastan tokens reales: se corren a mano, nunca en CI. Formato, fixtures y comandos
exactos en [`evals/README.md`](evals/README.md).

## Comandos de manage clave

| Comando | Para qué |
| --- | --- |
| `preflight` | Revisa la configuración de producción (llaves, precios de Stripe, correo). |
| `process_knowledge` | Procesa el conocimiento subido (escaneo/archivos) en segundo plano. |
| `tag_conversations` | Etiqueta por tema las conversaciones del día (cron nocturno). |
| `send_booking_reminders` | Recordatorios de citas. |
| `send_followup_digest` | Resumen diario de seguimiento para los equipos (opcional). |
| `purge_expired` | Borra lo que pasó el período de retención de cada negocio. |
| `reconcile_stripe` | Cuadra suscripciones con Stripe y avisa de pagos manuales por vencer. |
| `bill_overage` | Cobra los bloques de excedentes a fin de mes. |
| `stripe_bootstrap` | Crea el catálogo de Stripe desde la tabla de precios del código. |
| `export_tenant --client X --out DIR` | Exporta todo un negocio (zip con JSON y CSV). |
| `delete_tenant --client X [--yes --export DIR]` | Exporta y borra un negocio (ensayo sin `--yes`). |
| `erase_visitor --email X \| --phone Y [--client X] [--yes]` | Derecho de borrado de un visitante (ensayo sin `--yes`). |
| `purge_test_tenant --email X [--yes]` | Borra un negocio creado por una compra de prueba. |
| `wa_simulate` | Simula un mensaje entrante de WhatsApp en local. |
| `agent_eval` / `seed_eval_clients` | Evals del agente y sus fixtures locales. |
| `publish_due` (app `social`) | Publica las piezas de redes programadas. |

Los crons de producción y su horario están en [`DEPLOYMENT.md`](DEPLOYMENT.md).

## Despliegue

Push a `main` = despliegue a producción (GitHub Actions corre las migraciones solo). Servidor,
nginx, crons, respaldos, rollback y operación: [`DEPLOYMENT.md`](DEPLOYMENT.md). No hay
staging todavía.

## Estructura

```
dominio_website/   settings, urls, wsgi
landing/           app principal: sitio, agente (agent.py, turns.py), widget, dashboard,
                   centro de control, pagos (payments.py), WhatsApp, conocimiento (ingest.py,
                   knowledge.py), privacidad (leads.py, tenant_export.py), comandos de manage,
                   plantillas, estáticos y tests (tests_*.py)
social/            publicación programada en redes
deploy/            nginx, gunicorn/systemd, scripts de despliegue y respaldo
evals/             casos de evaluación del agente y resultados
docs/              documentación interna (ver abajo)
marketing/         kit de marketing bilingüe
setup/             instalación de la flota de agentes de Claude Code
tools/             utilidades (fuentes, Stripe)
```

## Roles

| Rol | Ve |
| --- | --- |
| staff (DOMINIO) | Centro de control, todos los negocios, conocimiento, costos. |
| `org_admin` | Su negocio completo: leads, conversaciones, citas, reportes, usuarios, auditoría, instalación, facturación, asistente. |
| `agent` | Leads, conversaciones, citas, reportes y asistente. |
| `employee` | Solo el asistente interno (ocupa un asiento del plan). |

Un login puede tener membresías en varios negocios. Al quitarle la última membresía la cuenta
queda desactivada (no se borra) y una nueva invitación o compra la reactiva.

## Documentación

- [`docs/agente/`](docs/agente/): reglas del agente por perfil.
- [`docs/costos/`](docs/costos/): análisis de costos del agente (PDF y su generador).
- [`docs/precios/`](docs/precios/): propuesta de paquetes.
- [`docs/lanzamiento/`](docs/lanzamiento/): Stripe, correo y checklists de lanzamiento y producción.
- [`docs/dev/`](docs/dev/): usuarios de prueba locales.
- [`docs/phase-report/`](docs/phase-report/): informes de fase del curso.
- Privacidad para visitantes: `/privacy/` (cómo pedir el borrado de datos).
