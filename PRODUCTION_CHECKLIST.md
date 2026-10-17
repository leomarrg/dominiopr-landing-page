# Checklist de producción: DOMINIO (sitio + DOMINIO Chat 24/7)

**Revisión:** 17 de octubre de 2026 · rama `chore/dev-setup` sobre `main` (`3dec408`, en producción).
**Stack:** Django 5.2, Python 3.12, SQLite, nginx + gunicorn en AWS Lightsail; agente con la API de
Anthropic (Claude Haiku 5.5); cuentas de usuario (dashboard por roles) y cobro con Stripe.

Estados: ✅ Cumple · ⚠️ Parcial · ❌ Falta · 🔍 Verificar fuera del repo · N/A

## Calidad y despliegue

| Punto | Estado | Evidencia o qué falta |
|---|---|---|
| CI/CD | ✅ | `.github/workflows/deploy.yml`: jobs `test` (check --deploy, makemigrations --check, suite completa), `audit` (pip-audit) y `deploy` solo si ambos pasan. Primera corrida en verde (run 39073145714). |
| Unit tests | ✅ | 1120 tests en `landing/tests*.py` y `social/`, sitio y chatbot. |
| Smoke tests | ✅ | `deploy/scripts/deploy.sh` prueba `/healthz/` y `/widget.js` tras cada reinicio y falla el deploy si no responden. |
| Regression tests | ✅ | La suite corre en cada push y PR y bloquea el deploy; tests de regresión con nombre propio (aislamiento entre negocios, cobro, seguridad). |
| Staging | ❌ | No existe. Requiere una segunda instancia (la actual tiene 414 MB). Ver tarea 5. |
| Plan de rollback | ✅ | `deploy/scripts/rollback.sh` vuelve al commit anterior y restaura la BD si hubo migraciones; backup automático antes de cada `migrate`. Probado en simulación con Docker; no se ha ejecutado en producción. |
| README | ✅ | `README.md` con "Cómo correrlo localmente" (Docker y sin Docker, verificado en un clon limpio), tests, evals, comandos y despliegue. |

## Seguridad

| Punto | Estado | Evidencia o qué falta |
|---|---|---|
| Auth en cada endpoint | ✅ | Toda vista de `/dashboard/` exige login y filtra por negocio; staff con `staff_required`. Las públicas (widget, chat, webhooks, contacto, SEO) tienen token, firma HMAC o límite de uso. Auditoría de seguridad del CRM sin hallazgos críticos ni altos. |
| Auditoría de dependencias | ✅ | `requirements.txt` con las 28 versiones fijas; pip-audit en CI y Dependabot semanal. Hoy: 0 vulnerabilidades. |
| Secretos fuera del repo | ✅ | Escaneo del árbol y de los 104 commits: sin claves de servicios. `.env` en `.gitignore`; `.env.example` solo con ejemplos. |
| Rotar claves filtradas | ✅ | No hay filtraciones en el repo. Si alguna clave se compartió fuera (chat, notas), rotarla igual. |
| Rate limiting | ✅ | Django (`_rate_limited` en login, reset, chat, demo, contacto, alta) y nginx (zonas general, api, auth y subida). Topes diarios por plan, por negocio, por IP y global. |

## Observabilidad

| Punto | Estado | Evidencia o qué falta |
|---|---|---|
| Alertas | ✅ | Errores 500 por correo a ADMINS (con freno de 1 por hora para caídas del modelo); `cron_run.sh` avisa si un cron falla; `cost_alert` avisa del gasto diario; el monitor de disponibilidad avisa por correo de GitHub. Los correos salen por Gmail (ver tarea 2). |
| Monitoreo de rendimiento | ⚠️ | `Server-Timing`, aviso de requests de más de 3 s y latencia del modelo p50/p95 en el centro de control. Falta medición de usuarios reales (Core Web Vitals de campo). |
| Analítica | ✅ 🔍 | GA4 con consentimiento en todas las páginas públicas; eventos `generate_lead`, `begin_checkout` y `purchase`; analítica del chatbot en el dashboard. Verificar en GA4 DebugView que llegan los eventos. |
| Error tracking | ⚠️ | Sentry integrado pero apagado hasta poner `SENTRY_DSN` en el `.env` del servidor. Mientras tanto, correo a ADMINS. Ver tarea 4. |
| Logging | ✅ | journald con formato estructurado, sin PII; log de cron con logrotate (8 semanas). |
| Uptime monitoring | ✅ | `.github/workflows/uptime.yml` cada 10 min contra `/healthz/` y el widget; primera corrida en verde. |

## Datos y costos

| Punto | Estado | Evidencia o qué falta |
|---|---|---|
| Backups de la base | ⚠️ | Backup diario verificado (14 días) y uno antes de cada deploy, pero todo dentro del mismo servidor. Si se pierde la instancia, se pierden los backups. Ver tarea 1. |
| Probar un restore | ✅ | Simulacro el 17 de octubre de 2026: copia íntegra, conteos iguales a la base viva, Django la lee. Repetir cada mes. |
| Topes de gasto y alertas de facturación | 🔍 | En código: topes diarios por plan, por negocio, por IP y global, más `cost_alert`. Falta en las consolas: límite mensual en Anthropic y presupuesto con alerta en AWS. Ver tarea 3. |

## Dominio y correo

| Punto | Estado | Evidencia o qué falta |
|---|---|---|
| Dominio y SSL | ✅ | HTTPS con certificado válido hasta el 21 de diciembre de 2026 y renovación automática de certbot; HSTS de 1 año; www redirige al dominio raíz. |
| SPF / DKIM / DMARC | ❌ | DMARC en cuarentena, pero el SPF no incluye a Microsoft 365 y no hay DKIM: los correos de `hola@dominiopr.com` pueden ir a spam. La plataforma envía desde Gmail. Ver tarea 2. |

## Experiencia de usuario y SEO

| Punto | Estado | Evidencia o qué falta |
|---|---|---|
| SEO y AEO | ✅ | `robots.txt`, `sitemap.xml` con `lastmod`, datos estructurados (Organization, Service, FAQ), `llms.txt`, canonical y OG. Lighthouse SEO 100 en inicio y planes. |
| 404, 500, vacíos y carga | ✅ | Páginas 400/403/404/500/CSRF en español; `50x.html` servida por nginx si la app cae; estados vacíos en todo el dashboard; JSON de error para el widget. |
| Varios dispositivos | ⚠️ | Probado emulando móvil (Lighthouse) y en escritorio. Falta un iPhone y un Android reales, sobre todo para el widget y el dashboard. |
| Redes lentas | ✅ | Inicio con 4G lenta y CPU 4 veces más lenta: LCP 2.4 s y CLS 0. |
| Accesibilidad | ⚠️ | Lighthouse accesibilidad 100 en inicio y planes. El dashboard tiene skip link, `aria-current` y tablas alternativas en las gráficas, pero no se ha auditado con lector de pantalla. |
| Imágenes OG y favicon | ✅ | og-image 1200×630, favicons 16/32, apple-touch, manifest y `/favicon.ico`. |
| Optimizar imágenes | ✅ | Imágenes de marca de 0.3 a 9 KB; los originales pesados salieron de lo publicado (`assets/brand-src/`). |
| Caching | ✅ | Estáticos con hash e `immutable` por 1 año, gzip; `widget.js` 5 min; el HTML no se cachea (lleva CSRF). |

## IA

| Punto | Estado | Evidencia o qué falta |
|---|---|---|
| Evals | ✅ | `manage.py agent_eval` con 8 archivos de casos y resultados guardados en `evals/results/` (última ronda: 140/140 y repeticiones del 15–17 de octubre). |
| Prueba de prompt injection | ✅ | Casos por perfil y vía conocimiento escaneado: 16/16 el 17 de octubre, sin fugas. El historial que manda el visitante se ignora. |
| Fallback si cae la API del modelo | ✅ | Timeout de 25 s por llamada y 50 s por turno; mensajes amables en el widget y en WhatsApp; las alertas no inundan el correo. |
| Límites de tokens y contexto | ✅ | Máximo 12 turnos de historial, 1500 tokens de salida, rondas de herramientas acotadas y techo de 160,000 caracteres de conocimiento por negocio. |

## Cuentas de usuario y legal

| Punto | Estado | Evidencia o qué falta |
|---|---|---|
| Privacidad y términos | ✅ 🔍 | `/privacy/` y `/terms/` con proveedores (Anthropic, Meta, Stripe, Twilio, Google) y cómo pedir el borrado. Falta revisión de un abogado. |
| Consentimiento de cookies | ✅ | Banner con aceptación explícita; GA4 solo carga si se acepta; reabrible desde el pie. |
| Borrar cuenta y datos | ⚠️ | Leads: borrar y anonimizar desde el dashboard. Visitantes y negocios: por pedido a `hola@dominiopr.com`, que DOMINIO ejecuta con `erase_visitor` y `delete_tenant`. No hay autoservicio para el cliente. |
| Reset de contraseña | ✅ | Flujo completo con límite de uso y verificación humana; tests en verde. La entrega depende del correo (tarea 2). |
| Verificación de email | ✅ | Doble opt-in de leads y activación de cuentas con enlace de 7 días. |

## Las 5 tareas más importantes que quedan

### 1. Copia de los backups fuera del servidor (pérdida de datos)
1. En AWS Lightsail, abre la instancia y entra en **Snapshots**.
2. Activa **Automatic snapshots** a las 05:00 (hora del servidor).
3. Confirma al día siguiente que aparece el primer snapshot.
4. Opcional, más barato y con 30 días: crea un bucket S3 privado y un usuario IAM con solo
   `s3:PutObject`, guarda la frase de cifrado en un archivo `chmod 600` del servidor y añade
   `BACKUP_S3_URI` y `BACKUP_PASSPHRASE_FILE` al crontab (DEPLOYMENT.md, "Copia externa").
5. Haz un simulacro restaurando desde ese snapshot o desde S3 y anota la fecha.

### 2. Correo autenticado con el dominio (seguridad y entrega)
1. En GoDaddy, DNS de `dominiopr.com`, cambia el SPF a
   `v=spf1 include:secureserver.net include:spf.protection.outlook.com -all`.
2. En Microsoft 365 Defender, sección DKIM, crea las llaves de `dominiopr.com` y añade en
   GoDaddy los dos CNAME `selector1._domainkey` y `selector2._domainkey` que te dé.
3. Crea una cuenta en Amazon SES o Postmark (comparación en la sección 16 del PDF de costos),
   verifica el dominio con sus registros DKIM y saca SES del modo de prueba.
4. En el `.env` del servidor pon `DJANGO_EMAIL_HOST`, `DJANGO_EMAIL_HOST_USER`,
   `DJANGO_EMAIL_HOST_PASSWORD` y `DJANGO_DEFAULT_FROM_EMAIL=DOMINIO <notificaciones@dominiopr.com>`,
   y reinicia: `sudo systemctl restart gunicorn-dominio`.
5. Envía una prueba a mail-tester.com y apunta a 9/10 o más.

### 3. Límites de gasto en las consolas (costos)
1. En la consola de Anthropic, **Settings → Limits**, pon un límite mensual de gasto y alertas
   por correo al 50 % y al 80 %.
2. Idealmente usa una clave dedicada a esta app y rota la actual.
3. En AWS, **Billing → Budgets**, crea un presupuesto mensual con alerta al 80 %.
4. En el `.env` del servidor ajusta `DOMINIO_DAILY_COST_ALERT_USD` si $5 al día no es tu umbral.

### 4. Activar Sentry (error tracking)
1. Crea un proyecto Django gratuito en sentry.io.
2. Copia el DSN en el `.env` del servidor como `SENTRY_DSN=...`.
3. Reinicia gunicorn y provoca un error de prueba desde el shell de Django.
4. Configura en Sentry una alerta por correo para errores nuevos.

### 5. Entorno de staging (riesgo de cada deploy)
1. Crea una segunda instancia pequeña de Lightsail con el subdominio `staging.dominiopr.com`.
2. Usa el sandbox de Stripe y un número de prueba de Meta; nunca datos de clientes reales.
3. Añade al workflow un job que despliegue la rama `staging` en esa instancia.
4. Antes de cada cambio grande, pruébalo ahí y luego haz el merge a `main`.

Después de esas cinco: probar en un iPhone y un Android reales, revisión legal de privacidad y
términos, auditoría del dashboard con lector de pantalla y autoservicio de borrado para clientes.
