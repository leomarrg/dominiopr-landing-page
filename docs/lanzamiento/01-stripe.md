# 01 - Stripe: cobrar suscripciones y la instalacion

Guia paso a paso para dejar Stripe listo y conectado a la app. Esta pensada para hacerse en una tarde, en orden. Todo lo que se cambia en el servidor se hace desde la consola del navegador de Lightsail con comandos de una linea.

Lo que ya esta hecho en el codigo (no hay que programar nada):

- Checkout alojado por Stripe (la tarjeta nunca toca tu servidor).
- El webhook `https://dominiopr.com/api/stripe/webhook/` activa, actualiza o cancela el cliente en la app.
- El checkout acepta codigos de promocion (`allow_promotion_codes`), asi que un cupon `LANZAMIENTO` funciona sin tocar codigo.
- Si un plan tiene precio de instalacion configurado, se cobra en el mismo checkout junto al primer periodo.
- Sin claves de Stripe en el `.env`, la app sigue funcionando con el flujo de "te contactamos por correo".

Precios de lanzamiento:

| Plan    | Mensual | Anual   | Instalacion y configuracion (una vez) |
|---------|---------|---------|----------------------------------------|
| Starter | $99     | $990    | $500                                   |
| Pro     | $299    | $2,990  | $1,000                                 |
| Scale   | $499    | $4,990  | $2,500                                 |

Todo en USD. Son 9 precios en total y cada uno tiene un ID (`price_...`) que va al `.env`.

---

## 0. Antes de empezar

1. Cuenta en [dashboard.stripe.com](https://dashboard.stripe.com). Si es nueva, completa **Activate your account** (datos del negocio, cuenta bancaria, identidad). Sin esto puedes probar en modo test pero no cobrar de verdad.
2. Entiende el switch **Test mode** (arriba a la derecha del dashboard). Modo test y modo live son dos mundos separados: productos, precios, webhooks y claves distintas. Todo lo que hagas en test tienes que repetirlo en live (o copiarlo).
3. Ten a mano el logo de DOMINIO (PNG cuadrado y horizontal) y el color de marca en hex.
4. Impuestos: si tu contador te dice que aplica IVU a tus servicios, eso se configura aparte (Stripe Tax o linea manual). Esta guia no cubre impuestos.

---

## 1. Marca y datos publicos (lo que el cliente ve en recibos y checkout)

1. **Settings** (engranaje arriba a la derecha) -> **Business** -> **Branding**.
   - Icon: logo cuadrado. Logo: version horizontal. Brand color y Accent color.
   - Guarda.
2. **Settings** -> **Business** -> **Public details**.
   - Business name: `DOMINIO`
   - Support email: `hola@dominiopr.com`
   - Support phone y website: `https://dominiopr.com`
   - Statement descriptor: `DOMINIOPR` (es lo que aparece en el estado de cuenta de la tarjeta; maximo 22 caracteres, sin acentos).
   - Guarda.

---

## 2. Crear los productos y precios

Haz esto primero con **Test mode activado**. Al final (seccion 8) lo repites en live.

Recomendacion: un producto por plan con sus 2 precios recurrentes, y un producto aparte por cada cargo de instalacion. Razon: en el recibo el cliente ve el **nombre del producto**, no el nombre del precio. Si pones la instalacion como tercer precio dentro de "DOMINIO Starter", el recibo dice "DOMINIO Starter" dos veces y confunde. Con producto aparte dice "Instalacion y configuracion - Starter", que es lo que quieres. Para la app da igual: solo necesita los 9 IDs de precio.

### 2.0 Atajo: crearlos con un comando (recomendado)

Las secciones 2 y 3 se pueden hacer solas. El comando lee la tabla de precios del
propio codigo (`views.PLANS`), crea los 6 productos y 9 precios en Stripe y te
imprime el bloque del `.env` ya lleno. Es idempotente: si lo corres dos veces no
duplica nada, y si un monto no cuadra con el codigo te lo dice.

En tu maquina (o en el servidor), con la clave **de test** en el `.env`:

```bash
python manage.py stripe_bootstrap --dry-run
```

Si lo que lista es correcto, quitale el `--dry-run`. Puedes crear tambien el
webhook y el cupon en la misma corrida (el `whsec_` solo se puede leer en el
momento de crear el endpoint, asi que guardalo enseguida):

```bash
python manage.py stripe_bootstrap --webhook-url https://dominiopr.com/api/stripe/webhook/ --coupon LANZAMIENTO
```

Para live es el mismo comando con la clave `sk_live_` en el `.env` y `--yes`.
Lo que el comando NO hace (son pantallas del dashboard): branding (seccion 1),
customer portal (seccion 5) y correos automaticos (seccion 6).

Si prefieres hacerlo a mano, sigue con 2.1.

### 2.1 Productos de suscripcion (3)

Para cada plan (Starter, Pro, Scale):

1. Menu izquierdo **Product catalog** -> boton **+ Add product** (arriba a la derecha).
2. Name: `DOMINIO Starter` (luego `DOMINIO Pro`, `DOMINIO Scale`).
3. Description (opcional, aparece en checkout): una linea, por ejemplo `Agente de IA para tu negocio - plan Starter`.
4. Bloque **Price**:
   - Pricing model: **Recurring** (o "Flat rate" + Recurring segun la version del dashboard).
   - Amount: `99.00` USD.
   - Billing period: **Monthly**.
   - Price description (interno): `starter-monthly`.
5. **Add another price**:
   - Recurring, Amount `990.00` USD, Billing period **Yearly**, Price description `starter-annual`.
6. Boton **Add product** (o **Save product**).

Montos por plan:

| Producto        | Precio mensual | Precio anual |
|-----------------|----------------|--------------|
| DOMINIO Starter | 99.00          | 990.00       |
| DOMINIO Pro     | 299.00         | 2,990.00     |
| DOMINIO Scale   | 499.00         | 4,990.00     |

### 2.2 Productos de instalacion (3)

Para cada plan:

1. **Product catalog** -> **+ Add product**.
2. Name: `Instalacion y configuracion - Starter` (luego `- Pro`, `- Scale`).
3. Description: `Cargo unico por instalacion, configuracion y entrenamiento inicial del agente`.
4. Price: Pricing model **One-off** (o **One time**), Amount `500.00` USD. Price description `starter-setup`.
5. **Add product**.

| Producto                                | Precio unico |
|-----------------------------------------|--------------|
| Instalacion y configuracion - Starter   | 500.00       |
| Instalacion y configuracion - Pro       | 1,000.00     |
| Instalacion y configuracion - Scale     | 2,500.00     |

Al terminar debes tener 6 productos y 9 precios.

---

## 3. Copiar los 9 IDs de precio al `.env`

Como encontrar un ID:

1. **Product catalog** -> clic en el producto.
2. En la tabla **Pricing** aparece cada precio. Clic en el precio (o en los tres puntos `...` -> **Copy price ID**).
3. El ID empieza con `price_` y tiene unos 30 caracteres. Copialo completo.

Tabla que tienes que llenar (guardala en un lugar privado, no en el repo):

| Variable en `.env`            | Producto                              | Precio          |
|-------------------------------|---------------------------------------|-----------------|
| `STRIPE_PRICE_STARTER_MONTHLY`| DOMINIO Starter                       | $99 mensual     |
| `STRIPE_PRICE_STARTER_ANNUAL` | DOMINIO Starter                       | $990 anual      |
| `STRIPE_PRICE_STARTER_SETUP`  | Instalacion y configuracion - Starter | $500 una vez    |
| `STRIPE_PRICE_PRO_MONTHLY`    | DOMINIO Pro                           | $299 mensual    |
| `STRIPE_PRICE_PRO_ANNUAL`     | DOMINIO Pro                           | $2,990 anual    |
| `STRIPE_PRICE_PRO_SETUP`      | Instalacion y configuracion - Pro     | $1,000 una vez  |
| `STRIPE_PRICE_SCALE_MONTHLY`  | DOMINIO Scale                         | $499 mensual    |
| `STRIPE_PRICE_SCALE_ANNUAL`   | DOMINIO Scale                         | $4,990 anual    |
| `STRIPE_PRICE_SCALE_SETUP`    | Instalacion y configuracion - Scale   | $2,500 una vez  |

Como ponerlos en el servidor (consola de Lightsail, un comando a la vez):

```bash
cp /var/www/dominio/.env /var/www/dominio/.env.bak-$(date +%F)
```

```bash
nano /var/www/dominio/.env
```

En `nano`: busca el bloque de Stripe (Ctrl+W, escribe `STRIPE`), pega los valores, guarda con Ctrl+O, Enter, sal con Ctrl+X. Si alguna de las tres lineas `STRIPE_PRICE_*_SETUP` no existe, agregala.

Nada de esto se activa hasta reiniciar Gunicorn (seccion 7).

---

## 3.5 IVU: el impuesto va SOBRE el precio anunciado

Los precios de la pagina (`$99`, `$299`, `$499`, y las instalaciones) **no incluyen
IVU**. El impuesto se calcula y se suma en el Checkout. Sin esto configurado,
Stripe factura el precio pelado, nadie ve un error, y el impuesto no se puede
cobrar despues.

Se crea con el mismo comando:

```
python manage.py stripe_bootstrap --tax-rate 11.5 --tax-name IVU
```

Imprime `STRIPE_TAX_RATE_ID=txr_...`; pegalo en el `.env` junto a los 9 precios.
La tasa (11.5% general, o 4% si aplica la tasa especial de servicios entre
comerciantes) es una determinacion de tu contable, no del codigo.

Tres cosas verificadas contra Stripe el 2026-08-27, para que no haya que
adivinarlas:

- El IVU se aplica a **los dos** line items: la suscripcion y el cargo de
  instalacion. Starter mensual pasa de $599.00 a **$667.89**.
- Queda **pegado a la suscripcion**, asi que las renovaciones tambien lo cobran
  (Checkout lo dice: *"Then $110.39 per month"* en vez de $99.00).
- Los precios quedaron en `tax_behavior=exclusive`. Esto solo se puede fijar una
  vez por precio; el comando lo arregla en sitio si estaban en `unspecified`.

Con `STRIPE_TAX_RATE_ID` configurado, `/get-started/` muestra sola la linea
"Los precios no incluyen IVU". Sin configurar, no la muestra — la pagina nunca
promete un cargo que el Checkout no hace.

`manage.py preflight` avisa con `[warn]` si la tasa no esta puesta, y con
`[FAIL]` si esta archivada o si es *inclusive* (que la restaria del precio en
vez de sumarla).

## 4. Clave secreta y webhook

### 4.1 Clave secreta

1. **Developers** (abajo a la izquierda, o en el menu superior) -> **API keys**.
2. En **Secret key** haz clic en **Reveal test key** y copiala. En test empieza por `sk_test_`, en live por `sk_live_`.
3. Va en `.env` como `STRIPE_SECRET_KEY=sk_test_...`.

Nunca pegues esta clave en un chat, en un issue de GitHub ni en el codigo. Si se te escapa, en la misma pantalla puedes hacer **Roll key** para invalidarla y generar otra.

### 4.2 Webhook

1. **Developers** -> **Webhooks** -> **+ Add endpoint** (en dashboards nuevos: **Add destination** -> **Events from your account** -> **Continue**).
2. Selecciona exactamente estos eventos:
   - `checkout.session.completed`
   - `customer.subscription.updated`
   - `customer.subscription.deleted`
3. Destination type: **Webhook endpoint**. Endpoint URL:

   ```
   https://dominiopr.com/api/stripe/webhook/
   ```

   Con la barra final. Sin `www`.
4. **Create destination** / **Add endpoint**.
5. En la pagina del endpoint: **Signing secret** -> **Reveal** -> copia el valor `whsec_...`. Va en `.env` como `STRIPE_WEBHOOK_SECRET=whsec_...`.

El webhook de test y el de live son endpoints distintos con secrets distintos. Cuando pases a live tienes que crear el endpoint otra vez.

---

## 5. Customer Portal (donde el cliente cambia tarjeta, ve facturas y cancela)

La app ya sabe abrir el portal para un cliente; solo hay que activarlo en Stripe.

1. **Settings** -> **Billing** -> **Customer portal**.
2. Activa:
   - **Invoice history**: on.
   - **Customer information**: permitir actualizar email y direccion.
   - **Payment methods**: permitir actualizar metodo de pago.
   - **Cancellations**: on. Opcion recomendada: **Cancel at end of billing period** (el cliente sigue con servicio hasta que termina lo que pago). Activa "Ask for cancellation reason" si quieres el dato.
   - **Subscriptions - Update subscriptions** (opcional pero util): on, y en **Products** agrega DOMINIO Starter, DOMINIO Pro y DOMINIO Scale con sus precios mensual y anual. Asi el cliente puede subir o bajar de plan solo. No agregues los productos de instalacion aqui.
   - Proration: **Prorate changes** (por defecto).
3. **Business information**: Headline `DOMINIO`, Terms of service `https://dominiopr.com/terms/`, Privacy policy `https://dominiopr.com/privacy/`.
4. **Save changes**.

---

## 6. Correos automaticos de Stripe

Estos correos salen desde Stripe (no desde tu servidor) y usan la marca del paso 1.

1. **Settings** -> **Emails** (o **Settings** -> **Business** -> **Customer emails**):
   - **Successful payments** (recibos): on.
   - **Refunds**: on.
   - Reply-to: `hola@dominiopr.com`.
2. **Settings** -> **Billing** -> **Subscriptions and emails**:
   - **Manage failed payments**: Smart Retries on. Al agotar los reintentos: **Cancel the subscription**. Esto importa: cuando Stripe cancela, manda `customer.subscription.deleted` y la app desactiva el cliente sola. Si eliges "mark as unpaid" la app no se entera.
   - **Send emails about failed payments**: on, con enlace para actualizar la tarjeta (usa la pagina de factura alojada por Stripe).
   - **Send emails about expiring cards**: on.
   - **Send emails about upcoming renewals**: on (aplica a los planes anuales; Stripe avisa unos dias antes de cobrar).
   - **Send a reminder when a free trial is about to end**: no aplica, no hay trials.
3. Guarda.

---

## 7. Probar en modo test

### 7.1 Poner las claves de test en el servidor

En la consola de Lightsail:

```bash
nano /var/www/dominio/.env
```

Asegurate de que estas 11 lineas tengan valor (todas de test por ahora):

```env
STRIPE_SECRET_KEY=sk_test_...
STRIPE_WEBHOOK_SECRET=whsec_...
STRIPE_PRICE_STARTER_MONTHLY=price_...
STRIPE_PRICE_STARTER_ANNUAL=price_...
STRIPE_PRICE_STARTER_SETUP=price_...
STRIPE_PRICE_PRO_MONTHLY=price_...
STRIPE_PRICE_PRO_ANNUAL=price_...
STRIPE_PRICE_PRO_SETUP=price_...
STRIPE_PRICE_SCALE_MONTHLY=price_...
STRIPE_PRICE_SCALE_ANNUAL=price_...
STRIPE_PRICE_SCALE_SETUP=price_...
```

Reinicia y verifica que la app las leyo (debe imprimir `9`, la lista de 9 nombres y `True True`):

```bash
sudo systemctl restart gunicorn-dominio && sudo systemctl is-active gunicorn-dominio
```

```bash
cd /var/www/dominio && venv/bin/python manage.py shell -c "from django.conf import settings as s; print(len(s.STRIPE_PRICES), sorted(s.STRIPE_PRICES)); print(bool(s.STRIPE_SECRET_KEY), bool(s.STRIPE_WEBHOOK_SECRET))"
```

Si imprime menos de 9, una linea del `.env` esta vacia o mal escrita.

### 7.2 Hacer una compra de prueba

1. Con **Test mode** activado en Stripe, abre `https://dominiopr.com/get-started/` en una ventana de incognito.
2. Elige un plan y llega al checkout de Stripe.
3. Tarjeta de prueba: `4242 4242 4242 4242`, fecha cualquiera en el futuro, CVC `123`, ZIP `00901`.
4. Confirma que el total incluye el primer periodo mas la instalacion (por ejemplo Starter mensual: $99 + $500 = $599).
5. Completa el pago.

Verifica tres cosas:

- Stripe -> **Payments**: aparece el pago.
- Stripe -> **Developers** -> **Webhooks** -> tu endpoint -> pestana de eventos: `checkout.session.completed` con respuesta **200**. Si sale 400 o 500, casi siempre es `STRIPE_WEBHOOK_SECRET` equivocado (mira `sudo journalctl -u gunicorn-dominio -n 50 --no-pager`).
- Tu dashboard en `https://dominiopr.com/dashboard/clients/`: el cliente existe y esta activo.

Luego prueba la cancelacion: Stripe -> **Customers** -> el cliente de prueba -> su suscripcion -> **Cancel subscription** -> **Immediately**. Vuelve al webhook: debe llegar `customer.subscription.deleted` con 200, y el cliente en tu dashboard debe quedar desactivado.

### 7.3 Probar un cupon

1. **Product catalog** -> **Coupons** -> **+ New**.
2. Name `Lanzamiento`, Type **Percentage**, `20` %, Duration **Repeating** por `3` months (o lo que decidas).
3. Marca **Use customer-facing coupon codes** y crea el codigo `LANZAMIENTO`. Puedes ponerle fecha de expiracion y limite de usos.
4. Repite la compra de prueba, escribe `LANZAMIENTO` en el campo "Add promotion code" del checkout y confirma que el total baja.

### 7.4 Opcional: Stripe CLI en tu maquina

Solo si quieres probar el webhook contra tu Django local:

```bash
stripe login
stripe listen --forward-to localhost:8000/api/stripe/webhook/
```

El comando imprime un `whsec_...` temporal que pones en tu `.env` local mientras pruebas. No es necesario para el servidor.

---

## 8. Pasar a live

1. Desactiva **Test mode** en el dashboard.
2. Confirma que la cuenta esta activada (sin banner amarillo de "Activate your account").
3. Productos y precios: abre cada producto en test mode; si ves el boton **Copy to live mode**, usalo. Si no aparece, crea los 6 productos y 9 precios a mano en live siguiendo la seccion 2. Los IDs `price_` de live son distintos de los de test: vuelve a llenar la tabla de la seccion 3.
4. Webhook: repite la seccion 4.2 en live y copia el nuevo `whsec_`.
5. Clave: **Developers** -> **API keys** -> **Reveal live key** -> `sk_live_...`.
6. Cupon `LANZAMIENTO`: crealo de nuevo en live (los cupones de test no se copian).
7. Portal y correos (secciones 5 y 6): revisa que esten activados en live; algunas opciones se configuran por modo.
8. En el servidor:

   ```bash
   cp /var/www/dominio/.env /var/www/dominio/.env.bak-$(date +%F)
   ```

   ```bash
   nano /var/www/dominio/.env
   ```

   Reemplaza las 11 lineas de Stripe con los valores live. Guarda.

   ```bash
   sudo systemctl restart gunicorn-dominio && sudo systemctl is-active gunicorn-dominio
   ```

   ```bash
   cd /var/www/dominio && venv/bin/python manage.py shell -c "from django.conf import settings as s; print(len(s.STRIPE_PRICES), s.STRIPE_SECRET_KEY[:8])"
   ```

   Debe imprimir `9 sk_live_`.

9. Haz la compra real de $1 con reembolso descrita en `03-checklist-lanzamiento.md`, seccion 4. No saltes este paso: es la unica forma de saber que el flujo completo (checkout, webhook, activacion, recibo) funciona con dinero real.

Rollback: si algo sale mal en live, vuelve al `.env` anterior y reinicia:

```bash
cp /var/www/dominio/.env.bak-AAAA-MM-DD /var/www/dominio/.env && sudo systemctl restart gunicorn-dominio
```

Sin claves de Stripe la app cae al flujo de contacto por correo; nadie se queda sin poder pedir el servicio.

---

## 9. Checklist final de Stripe

- [ ] Cuenta activada (datos bancarios e identidad aprobados).
- [ ] Branding, support email `hola@dominiopr.com` y statement descriptor `DOMINIOPR`.
- [ ] 6 productos y 9 precios en **live**, montos verificados contra la tabla de arriba.
- [ ] 9 `STRIPE_PRICE_*` + `STRIPE_SECRET_KEY` (`sk_live_`) + `STRIPE_WEBHOOK_SECRET` en `/var/www/dominio/.env`.
- [ ] Gunicorn reiniciado y el comando de verificacion imprime `9 sk_live_`.
- [ ] Webhook live apuntando a `https://dominiopr.com/api/stripe/webhook/` con los 3 eventos.
- [ ] Customer portal activado (facturas, metodo de pago, cancelar, cambiar plan).
- [ ] Emails de recibos, reembolsos, pagos fallidos, tarjeta por expirar y renovacion activados; reply-to `hola@dominiopr.com`.
- [ ] Pagos fallidos terminan en **Cancel the subscription** (para que la app desactive al cliente).
- [ ] Cupon `LANZAMIENTO` creado en live.
- [ ] Compra real de $1 hecha, reembolsada y suscripcion de prueba cancelada.
- [ ] Copia de respaldo del `.env` (`.env.bak-FECHA`) guardada en el servidor.
