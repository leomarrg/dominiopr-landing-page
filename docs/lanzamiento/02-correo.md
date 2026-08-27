# 02 - Correo: buzon, correo transaccional y newsletters

Objetivo: que todo lo que salga de DOMINIO salga desde `@dominiopr.com`, autenticado (SPF, DKIM, DMARC), y que cada tipo de correo use el canal correcto. Hoy la app manda por Gmail SMTP desde `creatudominiopr@gmail.com`; al terminar esta guia manda por Resend desde `hola@dominiopr.com`.

## Resumen: que correo sale por donde

| Tipo de correo                                                    | Sale desde                            | Servicio           | Quien lo manda           |
|-------------------------------------------------------------------|---------------------------------------|--------------------|--------------------------|
| Conversaciones con clientes y prospectos (tu escribes)            | `hola@dominiopr.com` (alias de `leomar@`) | Microsoft 365  | Tu, desde Outlook        |
| Avisos de la app: nuevo lead, onboarding, citas, recordatorios    | `DOMINIO <hola@dominiopr.com>`        | Resend (SMTP)      | Django                   |
| Recibos, pagos fallidos, tarjeta por expirar, renovaciones        | Stripe, reply-to `hola@dominiopr.com` | Stripe             | Stripe                   |
| Newsletter / anuncios a lista con consentimiento                  | `DOMINIO <hola@news.dominiopr.com>`   | Brevo o MailerLite | Tu, desde la herramienta |
| Correo frio (outreach a quien no te conoce)                       | Nunca desde `dominiopr.com`           | Dominio aparte     | Ver seccion 7            |

Estado actual del DNS de `dominiopr.com` (verificado):

- MX -> `dominiopr-com.mail.protection.outlook.com` (Microsoft 365 via GoDaddy). Tu buzon `leomar@dominiopr.com` ya vive ahi.
- SPF raiz: `v=spf1 include:secureserver.net -all`.
- DMARC: `v=DMARC1; p=quarantine; adkim=r; aspf=r; rua=mailto:dmarc_rua@onsecureserver.net;`.

Nada de eso se rompe con esta guia. Se agregan registros para Resend en subdominios y se ajusta el DMARC.

---

## 1. Tu buzon es `leomar@dominiopr.com`; `hola@` se crea como alias (no como buzon)

Tu buzon real en Microsoft 365 ya existe: **`leomar@dominiopr.com`**. No crees un segundo usuario de pago para `hola@`: se agrega como **alias** del mismo buzon. Todo lo que llegue a `hola@` (o a `pagos@`, `soporte@` si los quieres) cae en tu misma bandeja, y puedes contestar "como" `hola@` desde Outlook. Es gratis y es lo normal para un negocio de una persona.

Por que un alias publico y no tu nombre: `hola@` es lo que se imprime en la web, en los recibos de Stripe y en el From de la app; si un dia contratas a alguien, le pasas el alias sin cambiar nada en ningun sistema.

1. Entra a [admin.microsoft.com](https://admin.microsoft.com) con `leomar@dominiopr.com` (o desde GoDaddy -> **My Products** -> **Email & Office** -> **Manage** -> el usuario `leomar`).
2. **Users** -> **Active users** -> clic en `leomar@dominiopr.com` -> pestana **Account** -> **Username and email** -> **Manage username and email**.
3. En **Aliases**: escribe `hola`, dominio `dominiopr.com` -> **Add** -> **Save changes**. Repite para `pagos` si lo quieres.
4. Espera 5-15 min. Mandate un correo a `hola@dominiopr.com` desde tu Gmail: debe llegar a la bandeja de `leomar@`.
5. Para contestar "como" `hola@` en Outlook: al redactar, campo **From** -> escribe `hola@dominiopr.com` (Outlook lo recuerda). Opcional: en Outlook web -> Settings -> Mail -> Compose and reply -> puedes dejar `hola@` como remitente por defecto.
6. Configura `leomar@dominiopr.com` en el telefono (app Outlook) para ver todo en un solo sitio.

Si GoDaddy no te deja llegar al centro de administracion de Microsoft (algunos planes de GoDaddy lo ocultan), en el panel de GoDaddy: **Email & Office** -> usuario `leomar` -> **Aliases** -> **Add alias**. Es el mismo resultado.

Verificacion (importante, 5 minutos):

1. Desde Outlook (`leomar@dominiopr.com`, From `hola@dominiopr.com`) mandate un correo a tu Gmail personal.
2. En Gmail abre el correo -> tres puntos -> **Show original** (Mostrar original).
3. Arriba debe decir `SPF: PASS`, `DKIM: PASS` y `DMARC: PASS`.
   - Si `SPF` falla: en GoDaddy DNS edita el TXT `@` de SPF y dejalo asi (una sola linea, un solo registro SPF):
     `v=spf1 include:secureserver.net include:spf.protection.outlook.com -all`
   - Si `DKIM` dice PASS pero con `d=...onmicrosoft.com` en vez de `d=dominiopr.com`, activa DKIM para tu dominio en [admin.microsoft.com](https://admin.microsoft.com) -> **Security** (Defender) -> **Email & collaboration** -> **Policies & rules** -> **Threat policies** -> **DKIM** -> `dominiopr.com` -> **Create DKIM keys**. Te da dos CNAME (`selector1._domainkey` y `selector2._domainkey`) que agregas en GoDaddy DNS y luego activas el switch. Con GoDaddy a veces ya viene hecho; por eso primero se verifica.

Firma sugerida en Outlook: Leomar Rodriguez, "DOMINIO - Estudio de Software y Tecnologia", `hola@dominiopr.com`, `dominiopr.com`. Sin imagenes pesadas.

---

## 2. Resend: correo transaccional de la app

Resend es gratis hasta 3,000 correos al mes con un tope de 100 por dia. Para leads, onboarding y recordatorios de citas sobra. Si un dia lo superas, el plan pago empieza en $20/mes.

### 2.1 Cuenta y dominio

1. Crea cuenta en [resend.com](https://resend.com) con `leomar@dominiopr.com` (asi la cuenta queda atada al negocio, no a un Gmail).
2. Menu **Domains** -> **Add Domain**.
   - Domain: `dominiopr.com` (el raiz; el From sera `hola@dominiopr.com`).
   - Region: **US East (N. Virginia)**.
3. Resend te muestra una pantalla con los registros DNS exactos. **Copia los valores desde esa pantalla, no desde este documento**: la clave DKIM es unica para tu cuenta y el subdominio de retorno puede variar.

Lo que vas a ver, y donde va en GoDaddy:

| Tipo | Nombre (en GoDaddy)   | Valor (copiar de Resend)                        | Prioridad | TTL     |
|------|-----------------------|-------------------------------------------------|-----------|---------|
| TXT  | `resend._domainkey`   | `p=MIGfMA0GCSq...` (la clave DKIM larga)         | -         | 1 hora  |
| MX   | `send`                | `feedback-smtp.us-east-1.amazonses.com`         | 10        | 1 hora  |
| TXT  | `send`                | `v=spf1 include:amazonses.com ~all`             | -         | 1 hora  |

Notas para no equivocarte en GoDaddy:

- En GoDaddy el campo **Name** lleva solo la parte antes de `.dominiopr.com`. Escribe `send`, no `send.dominiopr.com`. Si Resend te muestra el nombre completo, recorta el `.dominiopr.com`.
- El MX y el TXT de `send` conviven sin problema: son tipos distintos con el mismo nombre.
- El SPF del raiz (`@`, `include:secureserver.net`) **no se toca**. Resend usa `send.dominiopr.com` como dominio de retorno, asi que la comprobacion SPF la hace contra el TXT de `send`, y la alineacion con DMARC la da la firma DKIM (`d=dominiopr.com`). Por eso funciona sin tocar el raiz.
- Unico caso en que el raiz cambiaria: si alguna vez desactivas el "custom return path" en Resend y el rebote pasa a salir desde `dominiopr.com` a secas. En ese caso el SPF raiz tendria que ser exactamente `v=spf1 include:secureserver.net include:amazonses.com -all`. Un dominio solo puede tener **un** registro SPF; si creas dos, ambos fallan.

Como agregar en GoDaddy:

1. **My Products** -> al lado de `dominiopr.com` -> **DNS** (o **Manage DNS**).
2. **Add New Record** -> Type, Name, Value (y Priority en el MX) como en la tabla -> **Save**.
3. Repite para los tres.
4. Vuelve a Resend -> **Domains** -> `dominiopr.com` -> **Verify DNS Records**. Suele verificar en minutos; puede tardar hasta 1 hora. Estado esperado: **Verified** en los tres.

### 2.2 API key

1. Resend -> **API Keys** -> **Create API Key**.
   - Name: `dominio-prod`
   - Permission: **Sending access**
   - Domain: `dominiopr.com`
2. Copia la clave (`re_...`). **Se muestra una sola vez.** Si la pierdes, borras esa y creas otra.

### 2.3 Poner Resend en el servidor

Consola de Lightsail:

```bash
cp /var/www/dominio/.env /var/www/dominio/.env.bak-$(date +%F)
```

```bash
nano /var/www/dominio/.env
```

Busca (Ctrl+W) `DJANGO_EMAIL` y deja el bloque asi. Borra o comenta con `#` las lineas viejas de Gmail:

```env
DJANGO_EMAIL_HOST=smtp.resend.com
DJANGO_EMAIL_PORT=587
DJANGO_EMAIL_USE_TLS=True
DJANGO_EMAIL_HOST_USER=resend
DJANGO_EMAIL_HOST_PASSWORD=re_TU_API_KEY_AQUI
DJANGO_DEFAULT_FROM_EMAIL="DOMINIO <hola@dominiopr.com>"
DJANGO_CONTACT_NOTIFY_EMAIL=leomar@dominiopr.com
DJANGO_EMAIL_TIMEOUT=10
```

Detalles:

- El usuario SMTP es literalmente la palabra `resend`. La contrasena es la API key.
- Puerto 587 con STARTTLS. Resend tambien acepta 465 con SSL, pero eso requiere `EMAIL_USE_SSL` que hoy no esta expuesto en settings; en el servidor 587 funciona, no lo cambies sin motivo.
- Las comillas en `DJANGO_DEFAULT_FROM_EMAIL` estan bien: systemd y python-dotenv las quitan. El nombre `DOMINIO` es lo que la gente ve en la bandeja.
- `DJANGO_EMAIL_TIMEOUT=10`: si Resend no responde en 10 segundos, la app abandona el envio en vez de dejar colgado el formulario. El contacto se guarda igual en la base de datos.
- El From **tiene** que ser del dominio verificado. `hola@dominiopr.com` cumple. Un `@gmail.com` como From seria rechazado por Resend.

Guarda (Ctrl+O, Enter, Ctrl+X) y reinicia:

```bash
sudo systemctl restart gunicorn-dominio && sudo systemctl is-active gunicorn-dominio
```

### 2.4 Prueba desde el servidor

Debe imprimir `1` (un correo enviado) y llegarte a tu bandeja (`leomar@`, via el alias `hola@`) en menos de un minuto:

```bash
cd /var/www/dominio && venv/bin/python manage.py shell -c "from django.core.mail import send_mail; print(send_mail('Prueba DOMINIO','ok','DOMINIO <hola@dominiopr.com>',['hola@dominiopr.com']))"
```

Si falla:

- `Authentication failed` / `535`: la API key esta mal pegada o no tiene permiso de envio para ese dominio.
- `Domain is not verified` / `403`: vuelve a Resend -> Domains y espera a que los tres registros esten Verified.
- `timed out`: raro en Lightsail; confirma que el firewall de la instancia no bloquea salida (por defecto no bloquea).
- Ver el detalle: `sudo journalctl -u gunicorn-dominio -n 50 --no-pager`.

Tambien puedes ver cada envio en Resend -> **Emails** (estado delivered, bounced, complained). Es tu registro de auditoria de correo transaccional.

Prueba de punta a punta: llena el formulario de contacto en `https://dominiopr.com` con tus datos. Debe llegar el aviso de lead a `leomar@dominiopr.com`, con Reply-To al correo del prospecto y el boton al dashboard.

### 2.5 Alternativa: Postmark

Si prefieres pagar por soporte y estadisticas mas finas, Postmark ($15/mes por 10,000 correos) es la referencia en correo transaccional: mismo esquema (verificas el dominio con un DKIM y un Return-Path CNAME, SMTP en `smtp.postmarkapp.com` puerto 587 con usuario y contrasena iguales al Server API Token). Para cambiar de proveedor solo se cambian `DJANGO_EMAIL_HOST`, `DJANGO_EMAIL_HOST_USER` y `DJANGO_EMAIL_HOST_PASSWORD`; el resto del `.env` y el codigo quedan igual. No hace falta decidirlo ahora.

---

## 3. DMARC: que los reportes te lleguen a ti y endurecer la politica

Hoy los reportes DMARC van a `dmarc_rua@onsecureserver.net` (GoDaddy). Nunca los ves. Cambia el destino:

1. GoDaddy -> DNS de `dominiopr.com` -> busca el registro TXT con Name `_dmarc` -> **Edit**.
2. Valor nuevo (una linea):

   ```
   v=DMARC1; p=quarantine; adkim=r; aspf=r; rua=mailto:leomar@dominiopr.com;
   ```

   Los reportes llegan como XML comprimido, uno por dia por cada proveedor grande. Son ilegibles a ojo. Opcion mejor y gratis: registra el dominio en un monitor DMARC gratuito (por ejemplo **Postmark DMARC Digests** en `dmarc.postmarkapp.com`, que te manda un resumen semanal legible). Te dara una direccion tipo `abc123@dmarc.postmarkapp.com`; entonces el valor seria:

   ```
   v=DMARC1; p=quarantine; adkim=r; aspf=r; rua=mailto:abc123@dmarc.postmarkapp.com,mailto:leomar@dominiopr.com;
   ```

3. **Save**.

Plan de endurecimiento:

- Semanas 1-4: politica `p=quarantine` (la que ya tienes). Revisa los reportes: todo lo que sale de Outlook, Resend y Brevo debe aparecer con SPF o DKIM alineado. Si ves una fuente legitima fallando (por ejemplo un formulario de GoDaddy que manda "en nombre de" tu dominio), arreglala o dejala de usar.
- Despues de 2-4 semanas limpias: cambia a `p=reject`. A partir de ahi cualquier correo que se haga pasar por `@dominiopr.com` sin firma es rechazado, lo cual protege tu reputacion.
- Regla practica desde ya: **nunca** configures Gmail para "enviar como" `hola@dominiopr.com`. Con `p=reject` eso se rechaza, y con `quarantine` cae en spam.

---

## 4. Google Postmaster Tools y Microsoft SNDS

**Google Postmaster Tools** (gratis, obligatorio si vas a mandar volumen a Gmail):

1. Entra en [postmaster.google.com](https://postmaster.google.com) con tu cuenta Google.
2. **+** -> Domain `dominiopr.com` -> te da un TXT de verificacion (`google-site-verification=...`).
3. GoDaddy DNS -> **Add New Record** -> TXT, Name `@`, Value el string -> Save -> vuelve y **Verify**.
4. Repite para `news.dominiopr.com` cuando exista (seccion 6).

Ahi ves tasa de spam, reputacion de dominio y errores de autenticacion. Los datos solo aparecen cuando mandas cierto volumen; al principio estara vacio, es normal. El numero que vigilas: **Spam rate por debajo de 0.3%** (por encima, Gmail empieza a filtrar todo tu dominio).

**Microsoft SNDS** ([sendersupport.olc.protection.outlook.com/snds](https://sendersupport.olc.protection.outlook.com/snds)): funciona por **direccion IP**, no por dominio. Con Resend o Brevo en plan gratis tus correos salen desde IPs compartidas que no son tuyas, asi que no puedes registrarlas. Solo aplica si algun dia contratas IP dedicada. Anotalo y sigue; la reputacion con Outlook la ves indirectamente en los reportes DMARC y en la tasa de rebote de la herramienta.

---

## 5. Verificar entregabilidad con mail-tester

1. Abre [mail-tester.com](https://www.mail-tester.com). Te muestra una direccion unica tipo `test-abc123@srv1.mail-tester.com`.
2. Manda un correo a esa direccion **desde el servidor** (asi pruebas el canal real de la app), sustituyendo la direccion:

   ```bash
   cd /var/www/dominio && venv/bin/python manage.py shell -c "from django.core.mail import send_mail; print(send_mail('Prueba de entregabilidad DOMINIO','Hola, esta es una prueba del sistema de avisos de DOMINIO. Si tienes preguntas responde a este correo.','DOMINIO <hola@dominiopr.com>',['test-abc123@srv1.mail-tester.com']))"
   ```

3. Vuelve a mail-tester y pulsa **Then check your score**. Objetivo: **9/10 o mas**. Lo que suele restar puntos y como arreglarlo:
   - SPF/DKIM/DMARC en rojo: registros de la seccion 2.1 sin propagar o mal pegados.
   - "Your message is not signed with DKIM": Resend no verificado todavia.
   - Falta enlace de baja (List-Unsubscribe): solo aplica a newsletters; en transaccional se ignora.
   - Dominio en lista negra: raro con dominio nuevo; si pasa, aparece el enlace para pedir la salida.
4. Repite la misma prueba desde Outlook (From `hola@dominiopr.com`) y, cuando exista, desde Brevo/MailerLite.

---

## 6. Newsletters y anuncios: Brevo o MailerLite desde `news.dominiopr.com`

Regla base: el correo masivo **no sale del mismo dominio que los avisos de la app ni que tu buzon**. Si una campana genera quejas, el dano se queda en `news.dominiopr.com` y no arrastra los recibos, los recordatorios de citas ni tu Outlook.

Herramientas:

- **Brevo** (antes Sendinblue): gratis hasta 300 correos/dia, contactos ilimitados. Suficiente para empezar.
- **MailerLite**: gratis hasta 1,000 suscriptores y 12,000 correos/mes, editor mas agradable.

Elige una. Los pasos son casi iguales:

1. Crea la cuenta con `leomar@dominiopr.com`.
2. **Senders & domains** (Brevo: Settings -> Senders, Domains & Dedicated IPs; MailerLite: Account -> Domains) -> **Add a domain** -> `news.dominiopr.com`.
3. La herramienta te muestra sus registros. Tipicamente:

   | Tipo  | Nombre (en GoDaddy)                | Valor                                    |
   |-------|------------------------------------|------------------------------------------|
   | TXT   | `news`                             | codigo de verificacion de la herramienta |
   | TXT   | `mail._domainkey.news` (o similar) | clave DKIM de la herramienta             |
   | TXT   | `news` (SPF)                       | `v=spf1 include:<su dominio> ~all`       |
   | CNAME | `news` o un subdominio de tracking | lo que indiquen (opcional)               |

   Copia **exactamente** los que te muestre la herramienta. Son registros del subdominio `news`, no tocan el `@` ni los de `send`.
4. Verifica en la herramienta. Espera hasta 1 hora.
5. Remitente por defecto: `DOMINIO <hola@news.dominiopr.com>`, **Reply-To** `hola@dominiopr.com` (asi las respuestas te llegan a Outlook).
6. DMARC del subdominio: hereda el `p=quarantine` del raiz. No hace falta registro aparte.
7. Registra `news.dominiopr.com` tambien en Google Postmaster Tools.

Requisitos que no son opcionales (Gmail y Yahoo los exigen desde 2024 a cualquier remitente masivo; CAN-SPAM aplica en Puerto Rico):

- **Consentimiento explicito**: solo entran a la lista quienes marcaron una casilla (no premarcada) o confirmaron por doble opt-in. Activa el doble opt-in en la herramienta.
- Los leads del formulario de contacto de la web **no** son suscriptores de newsletter. Te dieron permiso para responder a su solicitud, no para campanas. Si quieres invitarlos, mandales un correo personal desde Outlook con un enlace de suscripcion.
- **Baja en un clic** (List-Unsubscribe header y enlace visible): la herramienta lo pone sola; no lo quites del footer.
- Procesar las bajas en 2 dias como maximo (la herramienta lo hace al instante).
- **Direccion postal fisica** en el footer de cada envio (CAN-SPAM). Un apartado postal vale.
- Asunto honesto, sin "RE:" falsos ni mayusculas gritando.
- **Tasa de quejas de spam por debajo de 0.3%** en Postmaster Tools. Con listas pequenas una sola queja pesa mucho: manda solo a quien lo pidio.
- SPF, DKIM y DMARC alineados (lo que configuraste arriba).

Buenas practicas que si mueven la aguja:

- Calienta el subdominio: los primeros envios a 20-50 personas que seguro abren (clientes, amigos del negocio), luego sube.
- Limpia la lista: quien no abre en 6 meses sale o recibe un "quieres seguir?".
- Un correo util cada 2-4 semanas gana a uno promocional semanal.
- Manda como persona ("Leo de DOMINIO") con texto mayormente plano, pocas imagenes y pocos enlaces.

Que NO hacer:

- No comprar ni "conseguir" listas. No raspar correos de directorios o Google Maps.
- No mandar campanas desde `hola@dominiopr.com` ni desde Outlook a mas de un punado de gente en BCC.
- No usar el dominio principal para correo frio.
- No esconder el enlace de baja ni pedir login para darse de baja.
- No reenviar la misma campana a los que no abrieron mas de una vez.

---

## 7. Correo frio (outreach)

Si algun dia decides escribir a negocios que no te conocen, hazlo desde un **dominio aparte** que no sea `dominiopr.com` (por ejemplo `dominiopr.co` o `holadominio.com`), con su propio buzon, calentado durante 2-3 semanas, con volumen bajo (20-40 al dia por buzon), personalizado y con una forma clara de decir "no me escribas mas". Asi, si ese dominio se quema, tu marca, tus recibos y tu Outlook siguen limpios. No lo montes ahora; es otra decision.

---

## 8. Nota honesta sobre la pestana "Principal" de Gmail

Nadie puede garantizarte llegar a Principal en vez de Promociones. Gmail decide por usuario, segun como ese usuario y gente parecida interactuan con correos parecidos. Un proveedor que te lo prometa te esta vendiendo humo.

Lo que si mueve la aguja, en orden:

1. **Que la persona espere el correo.** Confirmaciones, recibos, recordatorios de cita, respuestas a algo que pidio: eso cae en Principal casi siempre. Es lo que manda la app.
2. **Autenticacion perfecta** (SPF, DKIM, DMARC alineados). Sin esto todo lo demas da igual.
3. **Que parezca correo de una persona**: nombre real, texto plano o casi, sin plantillas de tres columnas, un solo enlace o ninguno, sin pixel de tracking cuando no hace falta.
4. **Interaccion**: respuestas, aperturas repetidas, que te agreguen a contactos. Pide a cada cliente nuevo que responda al correo de bienvenida; una respuesta vale mas que cualquier truco.
5. **Volumen estable** desde el mismo dominio, sin picos.
6. **Reputacion limpia**: quejas cerca de cero, rebotes cerca de cero (lista limpia).

Lo que no funciona: palabras magicas en el asunto, pedir que te muevan a Principal en el primer correo, mandar desde Gmail personal "para que parezca humano", o cambiar de proveedor cada mes.

---

## 9. Checklist de correo

- [ ] Alias `hola@dominiopr.com` agregado al buzon `leomar@dominiopr.com`; el buzon configurado en el telefono.
- [ ] Correo de prueba desde Outlook a Gmail: SPF, DKIM y DMARC en PASS con `d=dominiopr.com`.
- [ ] Resend: dominio `dominiopr.com` **Verified** (DKIM `resend._domainkey`, MX y TXT de `send`).
- [ ] API key `re_...` creada con permiso de envio y guardada en el gestor de contrasenas.
- [ ] `.env` del servidor con el bloque Resend; lineas de Gmail eliminadas o comentadas; `DJANGO_EMAIL_TIMEOUT=10`.
- [ ] Gunicorn reiniciado; el `send_mail` de prueba imprime `1` y el correo llega.
- [ ] Formulario de contacto probado: el aviso llega a `hola@` con Reply-To correcto.
- [ ] mail-tester: 9/10 o mas desde el servidor.
- [ ] DMARC `rua` apuntando a `leomar@dominiopr.com` o al monitor gratuito; fecha anotada para revisar y pasar a `p=reject` en 2-4 semanas.
- [ ] Google Postmaster Tools con `dominiopr.com` verificado.
- [ ] Decidido Brevo o MailerLite; `news.dominiopr.com` verificado; doble opt-in activado; direccion postal en el footer.
- [ ] Gmail **no** configurado para "enviar como" `@dominiopr.com`.
