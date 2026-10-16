# DOMINIO — Deployment Guide

Deployment a AWS Lightsail con dominio en GoDaddy y CI/CD vía GitHub Actions.

---

## Arquitectura

```
GoDaddy DNS  ───────────►  AWS Lightsail Instance (Ubuntu 22.04)si pude entrar
   *.dominiopr.com               │
                                 ├── Nginx (reverse proxy + SSL)
                                 │     ├── dominiopr.com         → gunicorn-dominio.sock
                                 │     ├── registratepr...com    → gunicorn-registratepr.sock  (futuro)
                                 │     ├── linkea...com          → gunicorn-linkea.sock        (futuro)
                                 │     └── pulsopolitico...com   → gunicorn-pulsopolitico.sock (futuro)
                                 │
                                 ├── Gunicorn (Django, vía systemd)
                                 ├── SQLite (db local)
                                 └── Let's Encrypt (SSL gratis, renueva auto)

GitHub Actions: on push to main → SSH al servidor → ./deploy/scripts/deploy.sh
```

**Costo estimado**: ~$10-12/mes (Lightsail $5 plan basta para arrancar; sube a $10 si necesitas más RAM).

---

## Parte 1 — Una sola vez: configurar el servidor

### 1.1 Crear la instancia en Lightsail

1. Abre [lightsail.aws.amazon.com](https://lightsail.aws.amazon.com/) y haz login.
2. **Create instance**:
   - Región: **us-east-1** (Virginia) — más cercana a PR.
   - Platform: **Linux/Unix**
   - Blueprint: **OS Only → Ubuntu 22.04 LTS**
   - Plan: **$5/mes** (1 vCPU, 1 GB RAM, 40 GB SSD) — suficiente para landing + 3 productos pequeños.
   - Identifier: `dominio-prod`
3. **Create instance** y espera ~2 min.
4. Una vez running:
   - Tab **Networking** → **Create static IP** → adjúntala a la instancia. Anótala (ej. `54.123.45.67`).
   - Tab **Networking** → **Firewall** → abre puertos:
     - SSH (22) — ya viene abierto
     - HTTP (80)
     - HTTPS (443)

### 1.2 Apuntar el dominio en GoDaddy

1. Login en GoDaddy → **My Products** → **DNS** del dominio `dominiopr.com`.
2. Borra los registros A/CNAME existentes que apunten al parking page de GoDaddy.
3. Añade estos registros (TTL: 600):

   | Tipo  | Nombre | Valor                          |
   |-------|--------|--------------------------------|
   | A     | @      | `<IP-estática-de-Lightsail>`   |
   | A     | www    | `<IP-estática-de-Lightsail>`   |
   | A     | *      | `<IP-estática-de-Lightsail>`   |

   El `*` cubre **todos los subdominios futuros** (registratepr, linkea, pulsopolitico). Cuando añadas un producto, no tienes que tocar GoDaddy.

4. Espera 5-30 min para propagación. Verifica con: `nslookup dominiopr.com` debe devolver la IP de Lightsail.

### 1.3 Conectarte a la instancia por SSH

1. En Lightsail → tu instancia → **Connect** tab → **Download default key** (`LightsailDefaultKey-us-east-1.pem`). Guárdala en `~/.ssh/lightsail-dominio.pem`.
2. En tu terminal local:
   ```bash
   chmod 400 ~/.ssh/lightsail-dominio.pem
   ssh -i ~/.ssh/lightsail-dominio.pem ubuntu@<IP-estática>
   ```

### 1.4 Setup inicial del servidor

Una vez dentro por SSH, corre estos comandos (cópialos todos juntos):

```bash
# Update + paquetes base
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3 python3-pip python3-venv git nginx certbot python3-certbot-nginx

# Crear directorio de la app
sudo mkdir -p /var/www/dominio
sudo chown ubuntu:ubuntu /var/www/dominio

# Clonar el repo
cd /var/www
git clone https://github.com/<TU-USUARIO>/dominiopr-landing-page.git dominio
cd dominio

# Crear venv e instalar deps
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# Crear el .env de producción
cp .env.example .env
nano .env   # edita: pon un SECRET_KEY nuevo, DJANGO_DEBUG=False
# Para que el chatbot 'Ask DOMINIO' y su mini CRM funcionen hace falta ademas:
#   ANTHROPIC_API_KEY            (sin ella el widget se oculta, el resto del sitio sigue)
#   DJANGO_EMAIL_HOST/USER/PASSWORD + DJANGO_CONTACT_NOTIFY_EMAIL  (leads y citas por correo)
# Todo lo demas (Stripe, Twilio, META_* de WhatsApp, SOCIAL_*) es opcional y degrada sin romper.
# Genera SECRET_KEY así (en otra ventana o después):
# python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"

# Migrate + collectstatic
python manage.py migrate
python manage.py collectstatic --noinput
deactivate
```

### 1.5 Configurar Gunicorn (systemd)

```bash
# Copiar el service file
sudo cp /var/www/dominio/deploy/systemd/gunicorn-dominio.service /etc/systemd/system/

# Habilitar y arrancar
sudo systemctl daemon-reload
sudo systemctl enable gunicorn-dominio
sudo systemctl start gunicorn-dominio

# Verificar
sudo systemctl status gunicorn-dominio
# Debe decir "active (running)". Si no, revisa logs: sudo journalctl -u gunicorn-dominio -n 50
```

### 1.6 Configurar Nginx

```bash
# Quitar el default
sudo rm /etc/nginx/sites-enabled/default

# Copiar nuestro config
sudo cp /var/www/dominio/deploy/nginx/dominio.conf /etc/nginx/sites-available/
sudo ln -s /etc/nginx/sites-available/dominio.conf /etc/nginx/sites-enabled/

# IMPORTANTE: el config referencia certs de Let's Encrypt que aún no existen.
# Comenta temporalmente las líneas de SSL antes de probar (o salta al paso 1.7 primero).
```

### 1.7 SSL gratis con Let's Encrypt

Antes de hacer esto, **el DNS debe estar propagado** (paso 1.2). Verifica:
```bash
dig +short dominiopr.com  # debe devolver tu IP de Lightsail
```

Luego en el servidor:

```bash
# Genera cert para dominio raíz + www
sudo certbot --nginx -d dominiopr.com -d www.dominiopr.com

# Sigue el prompt: pon tu email, acepta TOS, elige opción 2 (redirect HTTP→HTTPS)
```

Certbot edita el Nginx config automáticamente y crea los certs. **Renovación auto** ya viene configurada vía cron.

Para subdominios futuros (cuando añadas un producto), repites:
```bash
sudo certbot --nginx -d registratepr.dominiopr.com
```

### 1.8 Verificar

Abre en el navegador: `https://dominiopr.com` — debe cargar tu landing con HTTPS verde.

Si algo falla:
- `sudo systemctl status gunicorn-dominio` — Gunicorn corriendo?
- `sudo nginx -t` — config válida?
- `sudo journalctl -u gunicorn-dominio -f` — errores de Django
- `sudo tail -f /var/log/nginx/error.log` — errores de Nginx

---

## Parte 2 — Una sola vez: configurar GitHub Actions

### 2.1 Generar un SSH key dedicado para CI/CD

**En tu máquina local** (no en el servidor):

```bash
ssh-keygen -t ed25519 -f ~/.ssh/dominio-deploy -N "" -C "github-actions@dominio"
```

Esto crea `~/.ssh/dominio-deploy` (privada) y `~/.ssh/dominio-deploy.pub` (pública).

### 2.2 Autorizar la key en el servidor

Copia la pública al servidor:

```bash
cat ~/.ssh/dominio-deploy.pub
# Copia el output
```

En el servidor:

```bash
ssh -i ~/.ssh/lightsail-dominio.pem ubuntu@<IP>
echo "<contenido-de-la-pub-key>" >> ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys
```

### 2.3 Dar permiso a `ubuntu` de reiniciar Gunicorn sin sudo password

El script de deploy hace `sudo systemctl restart gunicorn-dominio`. Para que no pida password en CI:

```bash
sudo visudo -f /etc/sudoers.d/dominio-deploy
```

Añade esta línea:
```
ubuntu ALL=(ALL) NOPASSWD: /bin/systemctl restart gunicorn-dominio
```

Guarda (`Ctrl+O`, Enter, `Ctrl+X`).

### 2.4 Hacer el deploy script ejecutable

```bash
chmod +x /var/www/dominio/deploy/scripts/deploy.sh
```

### 2.5 Añadir secrets en GitHub

Ve a tu repo en GitHub → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**.

Crea estos 3 secrets:

| Nombre               | Valor                                                    |
|----------------------|----------------------------------------------------------|
| `LIGHTSAIL_HOST`     | IP estática de Lightsail (ej. `54.123.45.67`)            |
| `LIGHTSAIL_USER`     | `ubuntu`                                                 |
| `LIGHTSAIL_SSH_KEY`  | Contenido COMPLETO de `~/.ssh/dominio-deploy` (privada)  |

Para copiar la private key correctamente:
```bash
cat ~/.ssh/dominio-deploy
# Copia desde -----BEGIN OPENSSH PRIVATE KEY----- hasta -----END OPENSSH PRIVATE KEY----- INCLUIDOS
```

### 2.6 Probar el workflow

Haz un commit dummy y push a `main`:
```bash
git commit --allow-empty -m "test: trigger first deploy"
git push origin main
```

Ve a tu repo → **Actions** tab → debe correr el workflow "Deploy to AWS Lightsail". Si pasa verde, todo está conectado.

A partir de aquí: **push a `main` = deploy automático**.

---

## Parte 3 — Añadir un producto nuevo (cuando exista)

Cuando RegístratePR / Linkea / Pulso Político estén listos:

1. **Si el producto es Django**: crea su propio repo (por ejemplo `registratepr`).
2. En el servidor:
   ```bash
   cd /var/www
   git clone https://github.com/<usuario>/registratepr.git
   cd registratepr
   python3 -m venv venv && source venv/bin/activate
   pip install -r requirements.txt
   cp .env.example .env  # edita
   python manage.py migrate
   python manage.py collectstatic --noinput
   ```
3. **Service file**: copia `deploy/systemd/gunicorn-dominio.service` como modelo y renombra a `gunicorn-registratepr.service`. Ajusta `WorkingDirectory`, paths, y `--bind unix:/run/gunicorn-registratepr.sock`. Habilita con `systemctl enable/start`.
4. **Nginx**: en `/etc/nginx/sites-available/dominio.conf` descomenta el bloque template (al final del archivo) y ajusta `registratepr` por el nombre del producto.
5. **SSL**: `sudo certbot --nginx -d registratepr.dominiopr.com`
6. **GitHub Actions**: cada producto tiene su propio repo → su propio `.github/workflows/deploy.yml` apuntando al mismo servidor pero con su propio path/servicio.

El DNS wildcard `*.dominiopr.com` ya cubre cualquier subdominio nuevo — no tocas GoDaddy.

---

## Operaciones del día a día

| Acción                         | Comando                                            |
|--------------------------------|----------------------------------------------------|
| Ver logs de la landing         | `sudo journalctl -u gunicorn-dominio -f`           |
| Restart manual                 | `sudo systemctl restart gunicorn-dominio`          |
| Status                         | `sudo systemctl status gunicorn-dominio`           |
| Forzar deploy manual           | `bash /var/www/dominio/deploy/scripts/deploy.sh`   |
| Renovar SSL manualmente        | `sudo certbot renew`                               |
| Re-cargar Nginx tras editar    | `sudo nginx -t && sudo systemctl reload nginx`     |
| Backup SQLite                  | `cp /var/www/dominio/db.sqlite3 ~/backup-$(date +%F).sqlite3` |

---

## Troubleshooting rápido

- **502 Bad Gateway** → Gunicorn cayó. `sudo systemctl status gunicorn-dominio` + journalctl.
- **CSS no carga (404)** → Faltó `collectstatic`. Corre `python manage.py collectstatic --noinput` en el servidor.
- **CSRF errors al enviar el form** → revisa `DJANGO_CSRF_TRUSTED_ORIGINS` en `.env` incluye el dominio HTTPS.
- **SSL expirado** → certbot debería renovar solo. Verifica con `sudo certbot renew --dry-run`.
- **El workflow de GitHub falla** → revisa los Actions logs; el problema más común es el SSH key mal pegado en secrets (debe incluir las líneas BEGIN/END).

## Phase 2 — operación de la plataforma

**Antes de cada deploy** (bloquea si falla — M-09):
```bash
python manage.py test landing social
python manage.py preflight   # Stripe, SMTP, IA y Django reales (--stripe|--email|--ai|--django para uno solo)
```

**Cron del sistema** (usuario de la app):
```cron
# M-05: recordatorios de citas (~24h antes), cada hora
0 * * * * cd /ruta/app && venv/bin/python manage.py send_booking_reminders
# Fase 3a: etiquetar intención/tema de conversaciones inactivas (Haiku, máx 300 por noche).
# Corre ANTES de purge_expired para no gastar en conversaciones que se van a borrar.
# --dry-run lista las candidatas sin llamar al modelo. Sale con error si la API falla.
45 2 * * * cd /ruta/app && venv/bin/python manage.py tag_conversations >> /home/ubuntu/dominio-cron.log 2>&1
# M-10: retención por tenant (conversaciones, leads salvo 'signup', citas), diario; --dry-run solo cuenta
30 3 * * * cd /ruta/app && venv/bin/python manage.py purge_expired
# M-10: backup de SQLite (API .backup, integridad verificada) + copia externa cifrada opcional (ver abajo)
15 4 * * * /var/www/dominio/deploy/backup_db.sh >> /home/ubuntu/dominio-cron.log 2>&1
# M-20: publicar en redes los posts aprobados que ya vencieron, cada 5 min
*/5 * * * * cd /ruta/app && venv/bin/python manage.py publish_due
# Fase 8: escaneos de sitio y archivos subidos -> conocimiento en BORRADOR (nunca activa solo).
# Una fuente por corrida (`--limit 1`): un escaneo completo puede tardar hasta 20 min de rastreo
# (DOMINIO_INGEST_TIME_BUDGET) más el destilado con Sonnet (hasta ~70 llamadas de ~15k tokens),
# y así nunca se mata una segunda fuente a medias. Un candado (65 min) evita dos corridas a la vez.
# Sale con error sin ANTHROPIC_API_KEY. `timeout 3600` es el tope duro de la corrida (cada página
# y cada llamada a la IA ya tienen su límite; esto cubre lo imprevisto). Si lo mata, la fila queda
# "procesando" sin latido y se reintenta sola a los 30 min (luego queda en error); el candado
# expira solo.
* * * * * cd /ruta/app && timeout 3600 venv/bin/python manage.py process_knowledge --limit 1 >> /home/ubuntu/dominio-cron.log 2>&1
# M-01: reconciliación de suscripciones contra Stripe, diario
45 4 * * * cd /ruta/app && venv/bin/python manage.py reconcile_stripe
# Fase C: el día 1 de cada mes factura el exceso de conversaciones del mes anterior como
# InvoiceItems de Stripe (bloques de +100 web $15, +500 WhatsApp $149, asiento extra $9).
# Idempotente (Idempotency-Key + AuditEvent 'billing.overage'); los clientes sin Stripe se
# listan en la salida y por correo para cobrarlos a mano. --dry-run muestra sin cobrar;
# --month YYYY-MM repite un mes. Nunca corta el servicio.
10 3 1 * * cd /ruta/app && venv/bin/python manage.py bill_overage >> /home/ubuntu/dominio-cron.log 2>&1
```

**Conocimiento desde el sitio y archivos (Fase 8; solo staff desde la Fase I):** el módulo
Conocimiento del dashboard (`/dashboard/knowledge/`) es exclusivo del staff de DOMINIO
(`is_staff`): DOMINIO carga y mantiene el conocimiento de cada agente; un administrador de
cliente que entre a esas URLs vuelve a su dashboard con el aviso de escribir a hola@dominiopr.com.
El staff elige el cliente con `?client=<slug>`, puede "Escanear el sitio" (cualquier URL pública)
o "Subir archivo" (PDF, .docx, TXT, MD, CSV, máx. 50 MB, se lee en memoria y no se guarda). El
trabajo pesado lo hace el cron `process_knowledge` de arriba (hasta 500 páginas / 1,200 s de
rastreo por sitio y hasta 4,000,000 caracteres de texto por fuente, `DOMINIO_INGEST_MAX_INPUT_CHARS`;
destila con `DOMINIO_INGEST_MODEL` = Sonnet 5.5 a effort `medium` y salida estructurada; el chat
sigue en Haiku 5.5). Cada llamada queda en el libro de uso como `knowledge_ingest` con el modelo
y su costo. El resultado queda en borrador y DOMINIO recibe un correo para probarlo y activarlo.
La página muestra el tamaño del conocimiento activo, los tokens aproximados y el costo por
pregunta del chat (sin caché y con caché); 8,000 caracteres por fuente y 40,000 en total son
avisos, y solo `DOMINIO_KNOWLEDGE_HARD_MAX_CHARS` (400,000) bloquea una publicación. Para escanear
el sitio de DOMINIO desde el servidor: `venv/bin/python manage.py process_knowledge --scan-site dominio https://dominiopr.com`.
Requiere `pip install -r requirements.txt` (añade `pypdf`), `migrate` (0032, 0033) y recargar nginx
con el `dominio.conf` y el `dominio-limits.conf` actualizados (location
`/dashboard/knowledge/upload/` con `client_max_body_size 51m`, máx. 2 subidas por IP y 1 en total (414 MB de RAM)
con la zona `dominio_upload_total`; sin eso nginx responde 413 a cualquier archivo de más de
256 KB). Copiar ambos archivos juntos y probar con `sudo nginx -t` antes de recargar: el
`dominio.conf` nuevo usa una zona que solo existe en el `dominio-limits.conf` nuevo.
La lectura de PDF/Word corre en un proceso aparte (`landing/extract_worker.py`) con 45 s de tope
(nginx da 65 s al request completo) y, en Linux, 350 MB de memoria y 45 s de CPU; un PDF que no
quepa en eso se rechaza con un mensaje claro y el worker de gunicorn sigue sano. En memoria, una
subida de 50 MB ocupa ~100-150 MB en el worker mientras se lee (nginx permite 4 a la vez).

**Verificación humana (hCaptcha, Fase 5):** apagada mientras `HCAPTCHA_SITEKEY` y
`HCAPTCHA_SECRET` estén vacíos (el sitio se comporta igual que antes; `preflight --ai` avisa en
producción). Para activarla: (1) desplegar el código y esperar 5 min (es el cache de `widget.js`
en los sitios de los clientes; un widget viejo frente a un servidor que ya exige pase no podría
enviar); (2) crear el sitio en dashboard.hcaptcha.com con los hostnames `dominiopr.com` y
`www.dominiopr.com` y activar ahí la restricción de dominio; (3) poner las dos llaves en `.env` y
reiniciar gunicorn; (4) probar en móvil que el reto cabe dentro del widget. Llaves de prueba
públicas para desarrollo (nunca en producción): sitekey `10000000-ffff-ffff-ffff-000000000001`,
secret `0x0000000000000000000000000000000000000000`. `HUMAN_PASS_TTL` (2 h) y
`HUMAN_PASS_MAX_MESSAGES` (30) controlan el pase firmado del chat. Si siteverify no responde
(red/5xx) el chequeo deja pasar y lo registra; un token inválido o un error 4xx siempre rechaza.
`collectstatic` (lo hace el deploy) recoge el `human-check.js` nuevo.

**Correos reales (Fase 4):** `DOMINIO_EMAIL_DNS_CHECK=True` (por defecto) consulta MX/A del
dominio por DNS-over-HTTPS a cloudflare-dns.com (3 s, cache 24 h) además de la lista de dominios
desechables; si la red falla deja pasar y lo registra. Ponlo en `False` si el servidor no tiene
salida HTTPS. Los leads reciben un enlace "Confirma tu correo" (7 días) que solo confirma con
POST; las citas no llevan enlace.

**Exportar los datos de un cliente (Fase 7):** `venv/bin/python manage.py export_tenant --client SLUG --out /ruta/dir`
genera un zip (JSON + CSV) con config sin secretos, leads y notas, conversaciones, citas,
conocimiento, horarios, miembros, encuestas y uso mensual sin costos; queda en la auditoría como
`tenant.export`. El mismo zip se descarga desde el centro de control (staff) en la ficha del cliente.

**Umbrales de infraestructura (cuándo crecer):** el código no usa nada exclusivo de SQLite
(`TruncMonth`, `Count(filter=)`, `Length` funcionan igual en Postgres) y todas las vistas de
control/dashboard hacen un número fijo de consultas. Pasar a Postgres cuando haya decenas de
clientes activos a la vez (escrituras concurrentes del chat) o se necesite más de un servidor;
subir el plan de Lightsail cuando falte RAM (gunicorn 2 workers x 4 hilos + nginx + crons
caben en 1 GB con swap). El tope global `DOMINIO_AGENT_DAILY_CAP` (2000 mensajes/día entre todos
los clientes) se ve en el centro de control; súbelo al sumar clientes.

**Redes sociales (M-20):** app `social/` (Instagram/Facebook via Graph API, TikTok via
Content Posting API; sin dependencias nuevas). Al desplegarla por primera vez:
`migrate` (crea `social_post`), instalar el cron `publish_due` de arriba y poner en `.env`
`SOCIAL_META_ACCESS_TOKEN` (token de pagina de larga duracion), `SOCIAL_IG_USER_ID`,
`SOCIAL_FB_PAGE_ID` y `SOCIAL_TIKTOK_ACCESS_TOKEN`. Sin credenciales el comando no publica
y lo dice en el log; nunca publica a medias. El CSP de `/admin/` permite `img-src https:`
para previsualizar el media de los posts (solo staff, detras de login).

**Stripe (M-01):** configura las claves y precios en `.env` (ver `.env.example`) y
apunta el webhook del dashboard de Stripe a `https://dominiopr.com/api/stripe/webhook/`
con los eventos `checkout.session.completed`, `customer.subscription.updated`,
`customer.subscription.deleted`. Sin claves, el signup sigue coordinándose por email.

**Evaluación del agente (M-19):** antes de activar cambios de conocimiento de un
cliente: `python manage.py agent_eval --client SLUG --file evals/SLUG.txt`
(formato: `pregunta | fragmento esperado`, una por línea).

### WhatsApp (Cloud API de Meta)

Fase G. El chat de WhatsApp de los visitantes va **directo con Meta** (sin Twilio ni BSP):
Meta hace POST a `https://dominiopr.com/api/whatsapp/webhook/` por cada mensaje, Django
resuelve el negocio por el `phone_number_id` del número (`ChannelAccount`), corre el mismo
turno que el widget (mismas herramientas: citas, recados, leads) y responde por la Graph API
dentro del mismo request. Twilio queda solo para SMS. Una sola app y un solo token de DOMINIO
sirven a todos los clientes; por cliente no hay secretos, solo el id de su número.

**Una sola vez (cuenta de DOMINIO):**

1. **App de Meta.** En [developers.facebook.com](https://developers.facebook.com) crear una
   app tipo *Business* ligada al Business Portfolio de DOMINIO y añadirle el producto
   *WhatsApp*. Eso crea una WhatsApp Business Account (WABA) de DOMINIO.
2. **Verificación del negocio.** Meta Business Suite → Configuración → Centro de seguridad →
   *Verificación del negocio* (documentos de DOMINIO). Sin ella la app queda en modo
   desarrollo: solo el número de prueba y hasta 5 destinatarios.
3. **System User y token permanente.** Business Suite → Usuarios → *Usuarios del sistema* →
   crear uno de tipo *Admin*, asignarle la app (control total) y la WABA, y *Generar token*
   sin caducidad con los permisos `whatsapp_business_messaging` y
   `whatsapp_business_management`. Ese token es `META_WA_ACCESS_TOKEN`.
4. **App Secret.** App → Configuración → Básica → *Clave secreta de la app* →
   `META_APP_SECRET`. Firma cada POST del webhook (`X-Hub-Signature-256`); sin ella el
   webhook responde 503.
5. **Clave del hash.** Generar `DOMINIO_PHONE_HASH_KEY` una vez (como la SECRET_KEY) y no
   rotarla nunca: es lo que sustituye al número del visitante en conversaciones y bajas.
6. **Webhook.** App → WhatsApp → Configuración → *Webhook*: Callback URL
   `https://dominiopr.com/api/whatsapp/webhook/`, *Verify token* = el valor inventado que
   pusiste en `META_WA_VERIFY_TOKEN` (reiniciar gunicorn antes de pulsar *Verificar*).
   Suscribir el campo **`messages`** (es el único que se procesa). nginx tiene su propia
   `location = /api/whatsapp/webhook/` con el bucket general: recargar nginx con el
   `dominio.conf` nuevo (`sudo nginx -t` primero) o Meta verá 503 por el límite de `/api/`.
7. **Plantillas de utilidad** (WhatsApp Manager → Plantillas de mensajes → *Crear*,
   categoría **Utilidad**, idioma **Español (es)**). Son obligatorias para escribirle al
   visitante fuera de la ventana de 24 h (recordatorios y cambios de estado desde el
   dashboard); dentro de la ventana el agente responde en texto libre y gratis. Texto
   propuesto (los ejemplos de variables se piden al crearla):
   - `cita_recordatorio` → `META_WA_TEMPLATE_REMINDER`:
     `{{1}}: te recordamos tu cita de mañana, {{2}}, en {{3}}. Si necesitas cambiarla, llámanos.`
     (ejemplos: `Salón Luz` · `21/10 a las 3:00 PM` · `Bayamón`)
   - `cita_estado` → `META_WA_TEMPLATE_STATUS`:
     `{{1}}: tu cita del {{2}} en {{3}} quedó {{4}}. Si quieres otra fecha, escríbenos.`
     (ejemplos: `Salón Luz` · `21/10 a las 3:00 PM` · `Bayamón` · `confirmada`)
   Hasta que estén aprobadas (minutos a horas) y puestas en `.env`, esos avisos salen por
   SMS (Twilio) si está configurado, o no salen. `{{3}}` es el local, o el nombre del negocio
   si no tiene locales (Meta no acepta variables vacías).

**Por cada cliente que contrata WhatsApp (plan Premium o override `channels`):**

1. **Registrar su número en la WABA de DOMINIO.** App → WhatsApp → *Configuración de la
   API* → *Agregar número de teléfono*: nombre visible (el del negocio; Meta lo revisa),
   categoría, y el número fijo o VoIP del negocio. La verificación es por SMS o **llamada**
   (sirve para un fijo). El número no puede estar activo en la app de WhatsApp o WhatsApp
   Business del cliente: si lo está, hay que borrarlo ahí primero. Al terminar, copiar el
   **`phone_number_id`** (no es el número) y el `WABA ID`.
2. **`ChannelAccount` en el admin** (`/admin/landing/channelaccount/`): cliente,
   `phone_number_id`, `waba_id`, teléfono visible. El formulario rechaza negocios en modo
   clínica. Un número activo por cliente.
3. **Comprobar:** `venv/bin/python manage.py preflight --whatsapp` lee cada número con el
   token (nombre verificado y calidad) y `venv/bin/python manage.py wa_simulate --client SLUG
   --from 17875551234 --text "hola" --reply "Hola, dime." --force` recorre el webhook completo
   con el envío simulado (imprime lo que se habría enviado; nada llega a Meta). No es un
   ensayo en seco: la conversación queda en el historial y el conteo del mes del cliente (con
   `--reply` no se registra uso del modelo), por eso en producción exige `--force` y conviene
   usar un cliente de prueba.
4. **Límites de mensajería de Meta:** un número nuevo puede iniciar conversaciones con 250
   contactos distintos por 24 h; sube a 1,000 y 10,000 solo según la calidad (bloqueos y
   reportes de los usuarios). Las respuestas dentro de la ventana no cuentan para ese límite.
   `preflight --whatsapp` muestra la calidad (`GREEN`/`YELLOW`/`RED`).

**Modo de prueba sin número real:** con la app en desarrollo, Meta da un *número de prueba*
y un *token temporal de 24 h* en *Configuración de la API*; se añaden hasta 5 destinatarios
(hay que confirmar cada uno por código). Para recibir el webhook en la laptop:
`ngrok http 8765`, poner la URL de ngrok + `/api/whatsapp/webhook/` como Callback URL con el
mismo `META_WA_VERIFY_TOKEN`, crear el `ChannelAccount` con el `phone_number_id` del número
de prueba y escribirle desde uno de los 5 números. Objetivo antes de dar el canal por bueno:
20 turnos con p95 < 10 s (Meta reintenta el evento si no hay 200 en pocos segundos; la
idempotencia por `wamid` absorbe el reintento).

**Qué guarda el sistema:** la conversación se indexa por un hash (`wa:<hash>:<día>`), nunca
por el número; una conversación = una ventana de 24 h de inactividad y nunca cruza el mes
(es la unidad del bucket de WhatsApp del plan, con sus avisos de 80/100 % y el cobro de
exceso por `bill_overage`). Las citas guardan el número (`Booking.wa_id`) porque el
recordatorio tiene que ir a algún sitio. `STOP`/`BAJA` crea un `ChannelOptOut` (la
plantilla no se envía; el agente deja de responder) y `START`/`ALTA` lo borra. Los topes:
`DOMINIO_WA_CONTACT_DAILY_CAP` por contacto, `daily_message_cap` del cliente en su propio
contador y el tope global `DOMINIO_AGENT_DAILY_CAP` compartido con el widget. Un token
caducado o revocado (401/190) aparece en la auditoría como `whatsapp.send_failed` (una vez
por hora por cliente) y en `preflight --whatsapp`.

## Lanzamiento self-serve (Stripe + correo)

Guias paso a paso, en orden, para dejar cobros y correo listos sin tocar codigo:

1. `docs/lanzamiento/01-stripe.md` — productos y 9 precios (mensual, anual, instalacion por plan), webhook, Customer Portal, correos de Stripe, prueba en test mode y paso a live.
2. `docs/lanzamiento/02-correo.md` — alias `hola@dominiopr.com` sobre el buzon `leomar@dominiopr.com` en Microsoft 365, correo transaccional por Resend (DNS en GoDaddy, API key, `.env`), DMARC y Postmaster Tools, newsletters desde `news.dominiopr.com`.
3. `docs/lanzamiento/03-checklist-lanzamiento.md` — checklist de go-live: `.env` completo, migraciones, reinicio, cron con rutas reales, compra real de $1 con reembolso, entregabilidad, webhook, backups y rollback.

Variables nuevas o que cambian de valor en `/var/www/dominio/.env` (detalle en `.env.example`):

| Variable                        | Valor de lanzamiento                     |
|---------------------------------|------------------------------------------|
| `DJANGO_EMAIL_HOST`             | `smtp.resend.com` (antes `smtp.gmail.com`) |
| `DJANGO_EMAIL_PORT`             | `587`                                    |
| `DJANGO_EMAIL_USE_TLS`          | `True`                                   |
| `DJANGO_EMAIL_HOST_USER`        | `resend` (literal)                       |
| `DJANGO_EMAIL_HOST_PASSWORD`    | API key de Resend (`re_...`)             |
| `DJANGO_DEFAULT_FROM_EMAIL`     | `"DOMINIO <hola@dominiopr.com>"`         |
| `DJANGO_CONTACT_NOTIFY_EMAIL`   | `leomar@dominiopr.com`                   |
| `DJANGO_EMAIL_TIMEOUT`          | `10` (nueva)                             |
| `STRIPE_SECRET_KEY`             | `sk_live_...`                            |
| `STRIPE_WEBHOOK_SECRET`         | `whsec_...` del endpoint live            |
| `STRIPE_PRICE_STARTER_SETUP`    | `price_...` (nueva, cargo unico $500)    |
| `STRIPE_PRICE_PRO_SETUP`        | `price_...` (nueva, cargo unico $1,000)  |
| `STRIPE_PRICE_SCALE_SETUP`      | `price_...` (nueva, cargo unico $2,500)  |

Tras editar `.env` hay que reiniciar: `sudo systemctl restart gunicorn-dominio` (systemd solo lee el archivo al arrancar). Nota para la seccion de cron de arriba: la ruta real es `/var/www/dominio`. La base es SQLite y `deploy/backup_db.sh` hace una copia consistente con la API `.backup` de sqlite3 (gzip, retiene 14 dias).

**Copia externa cifrada del backup (opcional).** El script no lee `.env`; las dos variables van en el crontab del usuario de la app (líneas `VAR=valor` arriba de las entradas) o en la misma línea del cron:

| Variable                  | Valor                                                                 |
|---------------------------|-----------------------------------------------------------------------|
| `BACKUP_S3_URI`           | destino sin `/` final, p. ej. `s3://mi-bucket/dominio-db`             |
| `BACKUP_PASSPHRASE_FILE`  | ruta a un archivo con la frase de cifrado (`chmod 600`, dueño = usuario del cron) |

Con ambas definidas, cada backup se cifra con `gpg --symmetric --cipher-algo AES256`, se sube con `aws s3 cp` y el `.gpg` local se borra; la copia local `.gz` se conserva siempre. Si cifrar o subir falla, el script escribe el error en stderr y sale con código 1 (la copia local ya quedó verificada). Si solo está `BACKUP_S3_URI`, también sale con 1 (nunca sube sin cifrar). Sin `BACKUP_S3_URI` imprime `Copia externa: omitida (BACKUP_S3_URI no configurado)`. Requiere `gpg` y `aws` (CLI v2 con credenciales de un usuario IAM limitado a `s3:PutObject` en ese prefijo). Guarda la frase también fuera del servidor: sin ella los backups externos no se pueden abrir. El procedimiento de restauración desde S3 está comentado dentro de `deploy/backup_db.sh`.
