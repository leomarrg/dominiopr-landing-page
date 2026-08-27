# 06 - Checklist de producción (infraestructura y operación)

Estado al **2026-08-27**. Complementa `05-checklist-web.md` (la página en sí) y
`03-checklist-lanzamiento.md` (los pasos de go-live paso a paso).

Cada punto dice **quién** lo hace. Lo marcado **[código]** ya está hecho y
verificado. Lo marcado **[tuyo]** necesita tus credenciales o una decisión tuya:
nadie más puede hacerlo por ti.

---

## Hecho y verificado

### HTTPS / SSL **[código + tuyo]**
- HTTP → HTTPS: `return 301` en el bloque `:80` (`deploy/nginx/dominio.conf:7-10`).
- **www vs apex: resuelto.** Antes ambos servían las mismas páginas, que es
  contenido duplicado y parte la autoridad del enlace entre dos URLs. Ahora
  `www.dominiopr.com` hace 301 al apex en su propio bloque, así que la petición
  ni llega a gunicorn.
- **[tuyo]** El certificado lo emite certbot en el servidor. Verifica la
  renovación automática: `sudo certbot renew --dry-run`.

### Security headers **[código]**
| Header | Dónde | Valor |
|---|---|---|
| `Content-Security-Policy` | `landing/middleware.py` | **nuevo** — ver abajo |
| `Strict-Transport-Security` | nginx + Django | 1 año, includeSubDomains |
| `X-Content-Type-Options` | Django + nginx | `nosniff` |
| `X-Frame-Options` | Django + nginx | `DENY` |
| `Referrer-Policy` | Django + nginx | `same-origin` |

La **CSP es nueva**. El sitio tiene mucho `<script>` inline, así que lleva
`'unsafe-inline'` — pero las directivas que de verdad frenan un ataque no
dependen de eso: `default-src 'self'` impide que un script inyectado cargue
código de otro dominio, `connect-src 'self'` impide que se lleve los datos a un
servidor ajeno (que es el objetivo de casi todo XSS), y `object-src 'none'` +
`base-uri 'self'` + `frame-ancestors 'none'` cierran vectores que no usamos.
Verificado en navegador: cero violaciones, el chat y el toggle de precios siguen
funcionando. `/widget.js` va exento a propósito — corre en el sitio del cliente,
bajo la CSP del cliente.

### Secretos de producción **[código, auditado]**
`.env` está en `.gitignore` y no está rastreado. Una búsqueda sobre **todos** los
commits del historial no encontró `sk-ant-api`, `sk_live_`, `whsec_` ni claves de
AWS. `db.sqlite3` nunca se commiteó. Los templates solo reciben el booleano
`AI_CHAT_ENABLED`: ninguna clave llega al JavaScript del cliente.

### Backup + restore **[código, probado de verdad]**
`deploy/backup_db.sh` usa la API de backup en línea de SQLite (copia consistente
aunque gunicorn esté escribiendo), y ahora:
- **se verifica a sí mismo**: descomprime la copia, la abre, corre
  `PRAGMA integrity_check` y cuenta clientes y leads. Una copia truncada se ve
  idéntica a una buena hasta el día que la necesitas;
- guarda con **700/600** en vez de 755/644 — esos archivos tienen los leads y las
  conversaciones de todos los clientes.

**Drill de restauración ejecutado el 2026-08-27**: se restauró un backup y la app
levantó contra él (home y `/get-started/` en 200, 3 clientes, 8 leads y 7
conversaciones intactos). Repítelo mensualmente y anota la fecha.

**[tuyo]** Falta la copia fuera del servidor. Un backup que solo vive en la
máquina que protege no es un backup. Descomenta la línea `aws s3 cp` del script,
o activa los snapshots automáticos de Lightsail (más simple).

### Dependencias y vulnerabilidades **[código]**
`pip-audit` encontró **8 vulnerabilidades conocidas en Django 5.2.8** y 5 en
sqlparse. Actualizado a **Django 5.2.17** y **sqlparse 0.6.0**; `requirements.txt`
fijado. Los 134 tests pasan con las versiones nuevas. Auditoría actual:
**cero vulnerabilidades conocidas**.

Repite antes de cada despliegue grande:
```
venv/bin/python -m pip install pip-audit
venv/bin/python -m pip_audit
```

### Protección de formularios **[código]**
- **CSRF**: activo en todos los formularios; el webhook de Stripe está exento a
  propósito porque se autentica con la firma HMAC, no con la cookie.
- **Rate limiting**: login 10/5min, password reset 5/15min, chat 12/min,
  demo 15/min, y ahora `/admin/login/` 5/5min (antes no tenía **ninguno**, y un
  superusuario ahí ve los datos de todos los clientes).
- **Anti-spam**: honeypot en los formularios públicos, más topes de gasto diario
  por tenant, por visitante y global.
- **[tuyo]** CAPTCHA: no hay. No lo pondría todavía — el honeypot y los límites
  están frenando lo que hay, y un CAPTCHA cuesta conversiones. Añádelo solo si
  ves spam real pasando.

### Logs y monitoreo de errores **[código]**
- Logs con timestamp, nivel y logger a journald (`journalctl -u gunicorn-dominio`).
- **Nuevo**: los errores 500 y los fallos de `landing` (provisioning, Stripe)
  ahora **te llegan por email** vía `ADMINS` + `AdminEmailHandler` de Django. Antes
  un error en producción era una línea que nadie leía: el primer aviso era un
  cliente quejándose. Sin servicio externo ni dependencia nueva.
- **PII fuera de los logs**: los leads y las citas ya no escriben nombre y email a
  stdout — journald no está cubierto por la política de retención que sí purga
  las conversaciones.
- **[tuyo, opcional]** Si más adelante quieres trazas agrupadas y contexto,
  Sentry tiene plan gratis. No es necesario para lanzar.

### Datos estructurados **[código]**
`index.html` ahora publica un `@graph` de schema.org validado:
- **Organization / ProfessionalService** — nombre, logo, email, área servida
  (Puerto Rico + EE.UU.), idiomas y los servicios que ofreces;
- **WebSite** — con el publisher enlazado;
- **Service "DOMINIO Chat 24/7"** — con `AggregateOffer` $99–$499;
- **FAQPage** — las 6 preguntas de la home (ya existía).

`sameAs` se deja fuera a propósito: no hay perfiles sociales que enlazar, y
poner unos falsos es peor que no poner ninguno.

### Cross-browser **[código, probado]**
Probado en **Chromium (Chrome/Edge), Firefox y WebKit (Safari)**: H1, loader,
navegación, logo, fuentes, chat, planes, toggle de precio y ausencia de scroll
horizontal a 375 px. **Los tres motores pasan todo.** WebKit importa
especialmente: Safari en iPhone es una parte grande del tráfico móvil en PR.

### Conexiones lentas **[código, probado]**
Con compresión igual a producción:

| Conexión | Titular visible | CTA usable |
|---|---|---|
| 4G lento | 523 ms | 587 ms |
| 3G malo | 771 ms | 1.0 s |
| 2G / borde | 11.9 s | 12.3 s |

Un detalle que salió midiendo: hacer `preload` de **las dos** fuentes competía
con el CSS que bloquea el render. Dejando solo la del titular (el elemento LCP)
mejoró 4G y 3G, y el cuerpo entra un instante después con `font-display: swap`.

**Ojo con medir en local**: el servidor de desarrollo de Django no comprime y
nginx sí. Sin compresión, 3G daba 8.5 s; con compresión, 0.77 s. Mide siempre
con gzip o creerás que el sitio está roto.

### Staging vs producción **[código]**
No hay `noindex` colgado de ninguna página pública. Los `noindex, nofollow` están
solo donde corresponde: dashboard, login, recuperación de contraseña y
`/bienvenida/`. El `robots.txt` y el `sitemap.xml` se generan desde
`landing/seo.py` con `SITE_URL`, nunca desde el `Host` de la petición, así que un
hit por IP o por www no puede envenenar las URLs canónicas.

### Rollback **[documentado]**
`03-checklist-lanzamiento.md` §9. En resumen: `git reset --hard <commit-bueno>` +
`deploy.sh`, y para apagar los cobros de golpe, vaciar `STRIPE_SECRET_KEY` y
reiniciar (la app vuelve al flujo de contacto por correo).

---

## Pendiente — necesita tus credenciales o tu decisión

Ninguno de estos es código. Están en orden de lo que más duele si falta.

### 1. Pagos en producción **[tuyo]** — bloqueante
El `.env` **no tiene ninguna clave de Stripe**, así que el flujo de pago no se ha
probado nunca, ni siquiera en modo test. Sigue `01-stripe.md` (es paso a paso) y
después:
```
cd /var/www/dominio && ./venv/bin/python manage.py preflight --stripe
```
Ese comando compara **el monto que Stripe va a cobrar contra el que anuncia la
página**, y verifica que el cargo de instalación sea one-time y no recurrente.
No anuncies nada mientras muestre un `[FAIL]`.

Después haz la compra real de $1 con cupón y reembolso (`03`, §4), y prueba
también: webhook, reembolso y **una tarjeta rechazada**.

### 2. SPF / DKIM / DMARC y entrega de correo **[tuyo]** — bloqueante
Hoy la app manda desde Gmail (`creatudominiopr@gmail.com`), que tiene tope de
~500 correos al día y es un imán de spam cuando el sitio es `dominiopr.com`. Como
la contraseña temporal del cliente viaja **solo** por ese correo, si cae en spam
el cliente pagó y no puede entrar. Migra a Resend siguiendo `02-correo.md`.

Prueba después con una cuenta de **Gmail, Outlook y Yahoo**, abre "Mostrar
original" y confirma SPF, DKIM y DMARC en PASS. mail-tester.com debe dar 9+/10.

### 3. Uptime monitoring **[tuyo]**
No hay nada avisando si el sitio se cae. UptimeRobot (gratis) sobre
`https://dominiopr.com/` cada 5 minutos, con alerta al teléfono. La caja es de
512 MB sin swap y ya se cayó antes por memoria, así que esto importa aquí más de
lo normal.

### 4. DNS **[tuyo]**
Verifica `A`/`CNAME` apuntando a la IP de Lightsail, y que los `MX`/`TXT` de
Microsoft 365 sigan intactos al tocar el DNS — si los borras, pierdes el correo.
**Activa la renovación automática del dominio**: un dominio vencido es una caída
total que además puede costarte el nombre.

### 5. Google Search Console **[tuyo]**
Registra el dominio, envía `https://dominiopr.com/sitemap.xml` y revisa la
cobertura a la semana. Registra el apex (que es donde ahora redirige todo).

### 6. Permisos de administración y 2FA **[tuyo]**
Revisa quién tiene acceso a: Lightsail/AWS, el registrador del dominio, GitHub,
Stripe, Google Analytics y Microsoft 365. **Activa 2FA en todos.** Stripe y el
registrador son los críticos: uno es tu dinero, el otro es todo lo demás.

Además, el admin de Django (`/admin/`) deja ver los datos de todos los clientes.
Ya tiene rate limiting, pero considera restringirlo por IP en nginx:
```nginx
location /admin/ { allow TU.IP.FIJA; deny all; proxy_pass http://unix:/run/gunicorn-dominio.sock; }
```

### 7. Pruebas en dispositivos reales **[tuyo]**
Yo probé emulado (375 px, con touch, en tres motores). Eso no sustituye tener un
iPhone y un Android en la mano: abre la home, el chat y `/get-started/`, y llena
el formulario con el teclado del teléfono.

### 8. 301 desde un sitio anterior **[tuyo]**
Solo aplica si estás reemplazando URLs que ya estaban indexadas. Si dominiopr.com
es nuevo, no hay nada que redirigir.

### 9. Plan de recuperación **[abajo]**

---

## Plan de recuperación

Qué hacer cuando algo se cae. Guárdalo donde puedas leerlo **sin** el sitio.

| Cae | Cómo lo notas | Qué haces |
|---|---|---|
| **Sitio web** | UptimeRobot te avisa | `sudo systemctl status gunicorn-dominio` → `restart`. Si no levanta: `journalctl -u gunicorn-dominio -n 100`. Si es memoria (pasó antes en esta caja de 512 MB), reinicia la instancia desde la consola de Lightsail. |
| **Base de datos** | Errores 500 en todo | Restaura el último backup: parar gunicorn, **copiar la DB actual a un lado antes de tocar nada**, `gunzip -c` encima, arrancar. Procedimiento completo en `deploy/backup_db.sh`. |
| **DNS** | El dominio no resuelve | Revisa el registrador. Si expiró, renuévalo de inmediato. Si alguien tocó los registros, restaura `A` (Lightsail) y `MX` (Microsoft 365). |
| **Correo** | Los clientes no reciben nada | Revisa el panel de Resend (rebotes, límites). Mientras tanto, la contraseña se recupera con "¿Olvidaste tu contraseña?" y los leads siguen guardándose en la base. |
| **API / agente de IA** | El chat contesta con el mensaje de respaldo | Verifica `ANTHROPIC_API_KEY` y el saldo de la cuenta. El sitio no se cae: el widget degrada a un mensaje que manda al formulario de contacto. |
| **Stripe** | Nadie puede pagar | Panel de estado de Stripe. Para apagar cobros: vacía `STRIPE_SECRET_KEY` y reinicia; el alta vuelve al flujo por correo y no pierdes ningún lead. |

Después de cualquier caída con pagos de por medio, corre:
```
./venv/bin/python manage.py reconcile_stripe
```
Ahora también busca **checkouts pagados sin agente aprovisionado** y te avisa por
correo — el fallo que de otro modo no notarías nunca.
