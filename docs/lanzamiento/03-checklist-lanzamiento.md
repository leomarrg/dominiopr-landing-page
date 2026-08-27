# 03 - Checklist de lanzamiento (go-live)

Orden recomendado. Cada seccion termina con una verificacion; no pases a la siguiente si la verificacion falla. Todos los comandos son de una linea para la consola del navegador de Lightsail (usuario `ubuntu`). Los pasos marcados **[PRODUCCION]** tocan el servidor o cobran dinero real.

Datos fijos del servidor (de `deploy/systemd/gunicorn-dominio.service` y `deploy/scripts/deploy.sh`):

- Carpeta de la app: `/var/www/dominio`
- Python de la app: `/var/www/dominio/venv/bin/python`
- Configuracion: `/var/www/dominio/.env` (no esta en git; sobrevive a los deploys)
- Servicio: `gunicorn-dominio` (usuario `ubuntu`)
- Deploy: push a `main` -> GitHub Actions -> `deploy/scripts/deploy.sh` (hace `git reset --hard`, `pip install`, `migrate`, `collectstatic`, restart)

Prerrequisitos: `01-stripe.md` y `02-correo.md` completados hasta donde dicen "test". Aqui se pasa todo a real.

---

## 0. Antes de tocar el servidor

- [ ] El codigo del lanzamiento esta en `main` y el ultimo workflow **Deploy to AWS Lightsail** en GitHub -> **Actions** esta en verde.
- [ ] Corriste los tests en tu maquina antes del push. El pipeline **no** corre tests (ver `.github/workflows/deploy.yml`); el gate eres tu:

  ```bash
  python manage.py test landing
  ```

- [ ] Tienes a mano, en tu gestor de contrasenas: `sk_live_...`, `whsec_...` (live), los 9 `price_...` (live), la API key de Resend `re_...`, `ANTHROPIC_API_KEY`, `GA_MEASUREMENT_ID`.
- [ ] Ventana de tiempo: 1-2 horas sin interrupciones. Nada de esto se hace un viernes a las 6pm.

---

## 1. Variables de entorno en el servidor **[PRODUCCION]**

Primero respalda el `.env` actual:

```bash
cp /var/www/dominio/.env /var/www/dominio/.env.bak-$(date +%F) && ls -la /var/www/dominio/.env*
```

Abre el editor:

```bash
nano /var/www/dominio/.env
```

El `.env` final debe contener todo esto (Ctrl+W para buscar cada bloque; las que ya existen solo se revisan, las marcadas NUEVA se agregan). Los comentarios `# NUEVA` son solo para esta guia: no los pegues al final de una linea con valor, systemd los tomaria como parte del valor.

```env
# --- Django (ya existen: revisar) ---
DJANGO_SECRET_KEY=<no cambiar: el que ya esta>
DJANGO_DEBUG=False
DJANGO_ALLOWED_HOSTS=.dominiopr.com,localhost,127.0.0.1
DJANGO_CSRF_TRUSTED_ORIGINS=https://dominiopr.com,https://www.dominiopr.com,https://*.dominiopr.com
DJANGO_SITE_URL=https://dominiopr.com
DJANGO_HSTS_SECONDS=3600
DJANGO_LOG_LEVEL=INFO

# --- Correo transaccional via Resend (reemplaza el bloque de Gmail) ---
DJANGO_EMAIL_HOST=smtp.resend.com
DJANGO_EMAIL_PORT=587
DJANGO_EMAIL_USE_TLS=True
DJANGO_EMAIL_HOST_USER=resend
DJANGO_EMAIL_HOST_PASSWORD=re_...
DJANGO_DEFAULT_FROM_EMAIL="DOMINIO <hola@dominiopr.com>"
DJANGO_CONTACT_NOTIFY_EMAIL=leomar@dominiopr.com
# NUEVA
DJANGO_EMAIL_TIMEOUT=10

# --- Analytics y agente (ya existen: revisar) ---
GA_MEASUREMENT_ID=G-...
ANTHROPIC_API_KEY=sk-ant-...
DOMINIO_AGENT_PROVIDER=anthropic
DOMINIO_COST_PER_MTOK=3.0

# --- Stripe LIVE ---
STRIPE_SECRET_KEY=sk_live_...
STRIPE_WEBHOOK_SECRET=whsec_...
STRIPE_PRICE_STARTER_MONTHLY=price_...
STRIPE_PRICE_STARTER_ANNUAL=price_...
STRIPE_PRICE_PRO_MONTHLY=price_...
STRIPE_PRICE_PRO_ANNUAL=price_...
STRIPE_PRICE_SCALE_MONTHLY=price_...
STRIPE_PRICE_SCALE_ANNUAL=price_...
# NUEVAS (cargo unico de instalacion por plan)
STRIPE_PRICE_STARTER_SETUP=price_...
STRIPE_PRICE_PRO_SETUP=price_...
STRIPE_PRICE_SCALE_SETUP=price_...

# --- Twilio (opcional; vacio = sin SMS/WhatsApp, el correo sigue) ---
TWILIO_ACCOUNT_SID=
TWILIO_AUTH_TOKEN=
TWILIO_FROM_SMS=
TWILIO_FROM_WHATSAPP=
```

Reglas del archivo:

- Una variable por linea, sin espacios alrededor del `=`.
- No dejes las lineas viejas `DJANGO_EMAIL_HOST_USER=creatudominiopr@gmail.com` activas junto a las nuevas: la ultima gana y confunde. Borralas o ponles `#` delante.
- Comentarios solo en lineas aparte que empiecen con `#`.

Guarda (Ctrl+O, Enter, Ctrl+X).

Verificaciones (no reinicies todavia; estas leen el archivo directamente):

```bash
grep -c '^STRIPE_PRICE_' /var/www/dominio/.env
```

Debe imprimir `9`.

```bash
grep -E '^(DJANGO_EMAIL_HOST|DJANGO_EMAIL_HOST_USER|DJANGO_DEFAULT_FROM_EMAIL|DJANGO_CONTACT_NOTIFY_EMAIL|DJANGO_EMAIL_TIMEOUT)=' /var/www/dominio/.env
```

Debe mostrar 5 lineas con `smtp.resend.com`, `resend`, `hola@dominiopr.com`, `leomar@dominiopr.com` y `10`.

```bash
grep -E '^STRIPE_SECRET_KEY=sk_live_' /var/www/dominio/.env | cut -c1-28
```

Debe imprimir `STRIPE_SECRET_KEY=sk_live_` (si imprime vacio, sigues con clave de test o la linea esta mal).

---

## 2. Migraciones y reinicio **[PRODUCCION]**

Si el ultimo push a `main` ya paso por Actions, las migraciones ya corrieron (`deploy.sh` ejecuta `migrate --noinput`). Confirma que no queda ninguna pendiente:

```bash
cd /var/www/dominio && venv/bin/python manage.py showmigrations landing | grep '\[ \]' || echo "OK: sin migraciones pendientes"
```

Si imprime lineas con `[ ]`, aplicalas:

```bash
cd /var/www/dominio && venv/bin/python manage.py migrate --noinput
```

Reinicia Gunicorn para que lea el `.env` nuevo (systemd solo lo lee al arrancar):

```bash
sudo systemctl restart gunicorn-dominio && sleep 2 && sudo systemctl is-active gunicorn-dominio
```

Debe imprimir `active`. Si imprime `failed`:

```bash
sudo journalctl -u gunicorn-dominio -n 40 --no-pager
```

Causa tipica: una linea del `.env` con comillas sin cerrar o un caracter raro. Restaura el respaldo y vuelve a intentar:

```bash
cp /var/www/dominio/.env.bak-$(date +%F) /var/www/dominio/.env && sudo systemctl restart gunicorn-dominio
```

Verifica que la app leyo la configuracion nueva:

```bash
cd /var/www/dominio && venv/bin/python manage.py shell -c "from django.conf import settings as s; print(len(s.STRIPE_PRICES), s.STRIPE_SECRET_KEY[:8], s.EMAIL_HOST, s.DEFAULT_FROM_EMAIL, s.CONTACT_NOTIFY_EMAIL, s.EMAIL_TIMEOUT)"
```

Esperado: `9 sk_live_ smtp.resend.com DOMINIO <hola@dominiopr.com> leomar@dominiopr.com 10`.

Y que el sitio responde:

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://dominiopr.com/
```

Debe imprimir `200`.

---

## 3. Cron jobs **[PRODUCCION]**

`DEPLOYMENT.md` (seccion Phase 2) lista los jobs con `/ruta/app`. La ruta real es `/var/www/dominio`. La app usa **SQLite** (`dominio_website/settings.py:152`); `deploy/backup_db.sh` ya hace la copia consistente con la API `.backup` (equivale a la linea inline de abajo — puedes usar cualquiera de las dos).

Crea la carpeta de backups una sola vez:

```bash
sudo mkdir -p /var/backups/dominio && sudo chown ubuntu:ubuntu /var/backups/dominio && ls -ld /var/backups/dominio
```

Abre el crontab del usuario `ubuntu`:

```bash
crontab -e
```

(Si pregunta por editor, elige `nano`.) Pega estas lineas al final:

```cron
# DOMINIO - jobs de plataforma (rutas reales del servidor)
# Recordatorios de citas (~24h antes), cada hora
0 * * * * cd /var/www/dominio && venv/bin/python manage.py send_booking_reminders >> /home/ubuntu/dominio-cron.log 2>&1
# Retencion de conversaciones por tenant, diario 3:30am
30 3 * * * cd /var/www/dominio && venv/bin/python manage.py purge_expired >> /home/ubuntu/dominio-cron.log 2>&1
# Backup SQLite consistente (API .backup, seguro con la app corriendo), diario 4:15am, retiene 14 dias
15 4 * * * cd /var/www/dominio && venv/bin/python -c "import sqlite3,datetime; s=sqlite3.connect('db.sqlite3'); d=sqlite3.connect('/var/backups/dominio/db-'+datetime.date.today().strftime('\%Y\%m\%d')+'.sqlite3'); s.backup(d); d.close(); s.close()" >> /home/ubuntu/dominio-cron.log 2>&1 && find /var/backups/dominio -name 'db-*.sqlite3' -mtime +14 -delete
# Reconciliacion de suscripciones contra Stripe, diario 4:45am
45 4 * * * cd /var/www/dominio && venv/bin/python manage.py reconcile_stripe >> /home/ubuntu/dominio-cron.log 2>&1
```

Nota: en crontab el caracter `%` es especial y hay que escribirlo `\%`; por eso el backup lleva `\%Y\%m\%d`. Si lo corres a mano fuera de cron, usa `%Y%m%d` sin barras (como en el comando de prueba de abajo).

Guarda y verifica que quedo:

```bash
crontab -l | grep -c '/var/www/dominio'
```

Debe imprimir `4`.

Prueba cada job a mano una vez, ahora, para no descubrir errores a las 4am:

```bash
cd /var/www/dominio && venv/bin/python manage.py send_booking_reminders && venv/bin/python manage.py purge_expired && venv/bin/python manage.py reconcile_stripe && echo "OK jobs"
```

```bash
cd /var/www/dominio && venv/bin/python -c "import sqlite3,datetime; s=sqlite3.connect('db.sqlite3'); d=sqlite3.connect('/var/backups/dominio/db-'+datetime.date.today().strftime('%Y%m%d')+'.sqlite3'); s.backup(d); d.close(); s.close(); print('backup OK')" && ls -la /var/backups/dominio/
```

Si `reconcile_stripe` falla con error de autenticacion, la `STRIPE_SECRET_KEY` esta mal.

---

## 4. Compra real de $1 en modo live, con cupon y reembolso **[PRODUCCION - COBRA DINERO REAL]**

Es la unica prueba que valida checkout, webhook, activacion del cliente, recibo de Stripe y correo de onboarding con dinero de verdad. Usa una tarjeta tuya.

### 4.1 Cupon de prueba (en Stripe, modo live)

1. Stripe -> **Product catalog** -> **Coupons** -> **+ New**.
2. Name `Prueba interna`, Type **Fixed amount**, Amount `598.00` USD, Duration **Once**.
3. **Use customer-facing coupon codes** -> code `PRUEBA1`. Redemption limit: `1`. Expiration: hoy.
4. Save.

Starter mensual + instalacion = $99 + $500 = $599. Con $598 de descuento el total debe quedar en **$1.00**.

### 4.2 Compra

1. Ventana de incognito -> `https://dominiopr.com/get-started/` -> plan **Starter mensual**.
2. En el checkout escribe el codigo `PRUEBA1` en **Add promotion code**.
3. **Si el total no dice exactamente $1.00, no pagues.** Cierra, revisa el cupon y vuelve.
4. Paga con tu tarjeta real. Usa un correo tuyo que no sea `hola@` (por ejemplo tu Gmail) para ver lo que ve el cliente.

### 4.3 Verificar todo lo que debe pasar

- [ ] Stripe -> **Payments**: pago de $1.00 succeeded.
- [ ] Stripe -> **Developers** -> **Webhooks** -> endpoint live -> `checkout.session.completed` con **200**.
- [ ] `https://dominiopr.com/dashboard/clients/`: el cliente nuevo aparece activo con plan Starter.
- [ ] Tu Gmail: recibo de Stripe (con logo y `hola@dominiopr.com` como reply-to) y el correo de bienvenida/onboarding de la app desde `DOMINIO <hola@dominiopr.com>`. Abre **Show original**: SPF, DKIM, DMARC en PASS.
- [ ] Tu bandeja (`leomar@dominiopr.com`): aviso interno de nuevo cliente y nada en la carpeta de spam.
- [ ] Servidor sin errores:

  ```bash
  sudo journalctl -u gunicorn-dominio --since '15 min ago' --no-pager | grep -i -E 'error|traceback' || echo "OK: sin errores"
  ```

### 4.4 Deshacer la compra

1. Stripe -> **Customers** -> tu cliente de prueba -> **Subscriptions** -> la suscripcion -> **Cancel subscription** -> **Immediately** -> confirma.
2. Verifica webhook: `customer.subscription.deleted` con **200**, y el cliente en tu dashboard queda desactivado.
3. Stripe -> **Payments** -> el pago de $1.00 -> **Refund** -> full refund -> reason "Requested by customer". Verifica que te llega el correo de reembolso.
4. **Product catalog** -> **Coupons** -> `Prueba interna` -> **Archive** (o Delete).
5. En tu dashboard, deja el cliente de prueba desactivado o borralo si hay opcion; no lo dejes contando como cliente.

Customer Portal en live: abre el enlace del portal que la app o el recibo le dan al cliente y confirma que carga con tu marca y las opciones (facturas, metodo de pago, cancelar). No cambies la tarjeta ahi: en live solo acepta tarjetas reales.

---

## 5. Entregabilidad de correo

- [ ] `send_mail` de prueba desde el servidor imprime `1` (comando en `02-correo.md`, 2.4).
- [ ] mail-tester.com desde el servidor: **9/10 o mas** (comando en `02-correo.md`, 5).
- [ ] Formulario de contacto real desde incognito: el aviso llega a `leomar@dominiopr.com`, Reply-To es el correo del prospecto, el boton al dashboard funciona.
- [ ] Desde Outlook (From `hola@`, buzon `leomar@`) a tu Gmail: SPF, DKIM, DMARC PASS.
- [ ] Resend -> **Emails**: los envios de prueba aparecen como **Delivered**, ninguno **Bounced**.
- [ ] Las lineas de Gmail ya no estan activas en el `.env`. Cuando todo funcione con Resend durante una semana, revoca el App Password de Gmail en `myaccount.google.com/apppasswords` (asi no queda una credencial viva sin uso).

---

## 6. Webhook de Stripe: estado sano

- [ ] Stripe -> **Developers** -> **Webhooks** -> endpoint live: Status **Enabled**, URL `https://dominiopr.com/api/stripe/webhook/`, 3 eventos.
- [ ] Pestana de entregas: todos los intentos de la prueba de $1 con **200**. Cero `4xx`/`5xx`.
- [ ] Stripe manda un correo automatico a la cuenta si el endpoint falla repetidamente; confirma que el correo de la cuenta Stripe es uno que lees (Settings -> Team and security -> tu usuario).
- [ ] Si algun evento quedo en rojo: corrige la causa y usa **Resend** en ese evento dentro de Stripe. La app ignora eventos duplicados (guarda cada `event_id` procesado), asi que reenviar es seguro.

---

## 7. Backups: uno hecho y uno restaurado

Un backup que nunca se ha restaurado no cuenta.

- [ ] Existe al menos un archivo en `/var/backups/dominio/` (seccion 3).
- [ ] Prueba de restauracion (no toca la base real; abre la copia y cuenta filas):

  ```bash
  cd /var/www/dominio && venv/bin/python -c "import sqlite3,glob; f=sorted(glob.glob('/var/backups/dominio/db-*.sqlite3'))[-1]; c=sqlite3.connect(f); print(f, 'integrity:', c.execute('pragma integrity_check').fetchone()[0], 'clientes:', c.execute('select count(*) from landing_client').fetchone()[0])"
  ```

  Debe imprimir la ruta, `integrity: ok` y un numero de clientes coherente. Si la tabla se llama distinto, lista las tablas con `select name from sqlite_master where type='table'`.

- [ ] Copia fuera del servidor: Lightsail -> tu instancia -> pestana **Snapshots** -> **Automatic snapshots** -> **Enable**, hora 05:00 (despues del backup). Guarda los ultimos 7 dias. Es lo mas simple para un solo servidor; cuesta centavos al mes. Como restaurar: **Create new instance from snapshot**, adjuntar la IP estatica a la nueva instancia. Anota la fecha en que probaste esto.
- [ ] Copia manual antes de este lanzamiento (por si acaso):

  ```bash
  cp /var/www/dominio/db.sqlite3 /home/ubuntu/db-prelanzamiento-$(date +%F).sqlite3 && ls -la /home/ubuntu/db-prelanzamiento-*
  ```

---

## 8. Observabilidad minima

No hay endpoint `/health/` en la app; usa la raiz.

- [ ] Monitor externo gratuito: [uptimerobot.com](https://uptimerobot.com) -> **Add New Monitor** -> HTTP(s) -> `https://dominiopr.com/` -> intervalo 5 min -> alerta a `leomar@dominiopr.com` y a tu telefono. Ademas agrega un monitor de tipo **Keyword** que busque una palabra del hero de la pagina, para detectar una pagina de error que devuelva 200 con contenido roto.
- [ ] Disco y memoria ahora mismo (anota los numeros como linea base):

  ```bash
  df -h / | tail -1 && free -m | head -2
  ```

  Con 512 MB de RAM y sin swap, la app se ha caido antes por memoria. Si `free` muestra menos de 100 MB disponibles con la app en reposo, considera el plan de 1 GB antes de anunciar.

- [ ] Certificado TLS:

  ```bash
  sudo certbot renew --dry-run 2>&1 | tail -3
  ```

  Debe decir que la simulacion fue exitosa. UptimeRobot tambien avisa de certificados por expirar si activas **SSL monitoring** en el monitor.

- [ ] Errores de la app: por ahora `journalctl`. Revisalo una vez al dia la primera semana:

  ```bash
  sudo journalctl -u gunicorn-dominio --since yesterday --no-pager | grep -c -i traceback
  ```

  Debe ser `0`. Si quieres captura de errores con alerta por correo sin instalar nada mas, Django puede mandar los tracebacks a `ADMINS`; eso es un cambio de codigo y no entra en este checklist.

- [ ] Log de cron: `tail -20 /home/ubuntu/dominio-cron.log` al dia siguiente; no debe haber tracebacks.

---

## 9. Rollback (leer antes de necesitarlo)

Configuracion (`.env`):

```bash
ls /var/www/dominio/.env.bak-*
```

```bash
cp /var/www/dominio/.env.bak-AAAA-MM-DD /var/www/dominio/.env && sudo systemctl restart gunicorn-dominio && sudo systemctl is-active gunicorn-dominio
```

Codigo: la forma segura es revertir en GitHub (el boton **Revert** en el commit o el PR) y dejar que Actions despliegue. Un `git reset` a mano en el servidor se pierde en el proximo push porque `deploy.sh` hace `reset --hard origin/main`. Solo como parche de emergencia:

```bash
cd /var/www/dominio && git log --oneline -5
```

```bash
cd /var/www/dominio && git reset --hard <sha-anterior> && sudo systemctl restart gunicorn-dominio
```

Base de datos: parar la app, reemplazar el archivo, arrancar:

```bash
sudo systemctl stop gunicorn-dominio && cp /var/backups/dominio/db-AAAAMMDD.sqlite3 /var/www/dominio/db.sqlite3 && sudo systemctl start gunicorn-dominio && sudo systemctl is-active gunicorn-dominio
```

Ojo: restaurar la base pierde todo lo que entro despues del backup (leads, clientes, pagos registrados). Antes de restaurar, copia la actual:

```bash
cp /var/www/dominio/db.sqlite3 /home/ubuntu/db-antes-de-restaurar-$(date +%F-%H%M).sqlite3
```

Stripe: si hay que apagar los cobros de golpe, vacia `STRIPE_SECRET_KEY` en el `.env` y reinicia. La app vuelve al flujo de contacto por correo; las suscripciones ya creadas siguen cobrandose en Stripe hasta que las canceles alli.

---

## 9.5 Verificacion automatica: `preflight`

Antes de mandar trafico real, corre esto en el servidor. Revisa de una sola vez
lo que falla en silencio: DEBUG, cookies, la clave y el webhook de Stripe, si el
SMTP acepta tus credenciales, y —lo mas importante— **si el monto que Stripe va a
cobrar es el mismo que el sitio anuncia**. Solo hace lecturas: no escribe en
Stripe ni manda correo a nadie.

```
cd /var/www/dominio && ./venv/bin/python manage.py preflight
```

Cada linea sale como `[ ok ]`, `[warn]` o `[FAIL]`. **No anuncies nada mientras
haya un `[FAIL]`.**

Por que importa: casi todas las integraciones aqui degradan en silencio a
proposito. Si un Price id de Stripe falta o esta mal, `price_id_for()` devuelve
vacio y el checkout **se cae al flujo de correo sin mostrar ningun error** — el
cliente no ve nada raro y tu nunca te enteras. Peor: si pegas el Price id
equivocado, el sitio dice $99 y Stripe cobra otra cosa. `preflight` compara cada
Price contra `PLANS` en `landing/views.py` (monto, intervalo, moneda, activo) y
verifica que el cargo de instalacion sea one-time y no recurrente.

Para revisar solo una parte: `--stripe`, `--email`, `--django`, `--ai`.

---

## 10. Checklist final (marca todo antes de anunciar)

- [ ] `manage.py preflight` sin ningun `[FAIL]`.
- [ ] `.env` completo con Resend + Stripe live + `DJANGO_EMAIL_TIMEOUT`; respaldo `.env.bak-FECHA` guardado.
- [ ] Gunicorn `active`; verificacion imprime `9 sk_live_ smtp.resend.com ...`.
- [ ] Sin migraciones pendientes.
- [ ] 4 cron jobs instalados con la ruta real y probados a mano.
- [ ] Backup SQLite diario funcionando y restauracion probada; snapshots automaticos de Lightsail activados.
- [ ] Compra real de $1 completada, webhook 200, cliente activado, recibo y onboarding recibidos, reembolso hecho, suscripcion cancelada, cupon archivado.
- [ ] Cupon `LANZAMIENTO` real creado en live con fecha de fin.
- [ ] mail-tester 9+/10; SPF/DKIM/DMARC PASS desde servidor y desde Outlook.
- [ ] DMARC `rua` apuntando a ti; fecha anotada para pasar a `p=reject`.
- [ ] Google Postmaster Tools verificado.
- [ ] UptimeRobot activo con alerta al telefono.
- [ ] Disco, memoria y `certbot renew --dry-run` revisados.
- [ ] App Password de Gmail pendiente de revocar en una semana (poner recordatorio).
- [ ] `leomar@dominiopr.com` (con el alias `hola@`) en el telefono; sabes donde ver Resend -> Emails, Stripe -> Webhooks y `journalctl` cuando algo falle.
