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

- Python 3.12 (la versión del servidor, Ubuntu 24.04), Django 5.2, SQLite (WAL).
- Anthropic (SDK `anthropic`) para el agente, la ingesta de conocimiento y el etiquetado.
- Stripe (API REST), Meta WhatsApp Cloud API, Twilio (solo SMS), hCaptcha, Google Analytics 4
  (solo con consentimiento).
- Producción: Lightsail (414 MB RAM + swap), nginx + gunicorn gthread, whitenoise para
  estáticos, despliegue por GitHub Actions al hacer push a `main`.

## Cómo correrlo localmente

Requisitos: **Git** y, según la opción, **Docker Desktop** (opción A) o **Python 3.12** (opción B).
No hace falta ninguna clave para arrancar: sin `ANTHROPIC_API_KEY` el sitio funciona y el chat
no contesta; sin Stripe, `/get-started/` usa el flujo de "te escribimos"; los correos salen en la
consola.

### Opción A: con Docker (recomendada)

1. Clona el repo y entra en la carpeta:
   ```bash
   git clone https://github.com/leomarrg/dominiopr-landing-page.git
   cd dominiopr-landing-page
   ```
2. (Opcional) Crea tu configuración: `cp .env.example .env` y deja solo lo que necesites; el
   bloque de arriba de `.env.example` explica las tres variables de desarrollo. Sin `.env`
   también arranca.
3. Levanta todo con un solo comando:
   ```bash
   docker compose up
   ```
   La primera vez construye la imagen (unos minutos). Aplica las migraciones solo y arranca dos
   servicios: `web` (la app) y `worker` (procesa los escaneos de conocimiento cada minuto).
4. Abre **http://localhost:8765**. El estado se ve en http://localhost:8765/healthz/.
5. Crea tu usuario de staff (en otra terminal) y, si quieres, datos de demostración:
   ```bash
   docker compose exec web python manage.py createsuperuser
   docker compose exec web python manage.py seed_eval_clients
   ```
   Entra en http://localhost:8765/dashboard/.
6. Para detener: `Ctrl+C` o `docker compose down`. Los datos quedan en el volumen
   `dominio_data`; `docker compose down -v` los borra.

Otro puerto: `APP_PORT=9000 docker compose up` (o `APP_PORT=9000` en `.env`).

### Opción B: sin Docker

1. Clona el repo y entra en la carpeta (paso 1 de la opción A).
2. Crea el entorno virtual con Python 3.12 e instala las dependencias:
   ```bash
   python -m venv venv
   # Windows (PowerShell):  venv\Scripts\activate
   # Windows (Git Bash):    source venv/Scripts/activate
   # Linux / macOS:         source venv/bin/activate
   pip install -r requirements.txt
   ```
3. Crea `.env` con lo mínimo para desarrollo:
   ```bash
   cp .env.example .env
   ```
   y cambia en `.env`: `DJANGO_DEBUG=True` y `DJANGO_ALLOWED_HOSTS=*` (o borra esas líneas:
   con `DEBUG=True` el valor por defecto ya es `*`).
4. Prepara la base de datos (SQLite, se crea sola) y la tabla de caché:
   ```bash
   python manage.py migrate
   python manage.py createcachetable
   python manage.py createsuperuser
   python manage.py seed_eval_clients      # opcional: datos de demostración
   ```
5. Arranca el servidor:
   ```bash
   python manage.py runserver 0.0.0.0:8765
   ```
   y abre http://localhost:8765.

### Entrar desde otra máquina de la red

1. Averigua la IP de tu computadora (`ipconfig` en Windows, `ip addr` o `ifconfig` en Linux/macOS),
   por ejemplo `192.168.1.50`.
2. Desde el otro equipo abre `http://192.168.1.50:8765`. Las dos opciones escuchan en `0.0.0.0`.
3. Si no abre: permite el puerto 8765 en el firewall de tu computadora (Windows pregunta la
   primera vez) y revisa que `DJANGO_ALLOWED_HOSTS` sea `*` o incluya esa IP.

Notas: el puerto por defecto es **8765** porque el 8000 lo usa otro proyecto en la máquina de
desarrollo. `.env` nunca se sube a git; `.env.example` documenta cada variable. Las cuentas de
prueba de la base local de Leomar están en `docs/dev/usuarios-prueba-local.md`, que no se sube.

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
