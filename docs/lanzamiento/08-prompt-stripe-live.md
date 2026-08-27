# Prompt para Claude en Chrome — las 3 pantallas que solo existen en LIVE

En el sandbox esto quedó bloqueado: son settings **de la cuenta**, no del modo, y
Stripe los esconde o los deshabilita en pruebas. Hay que hacerlos en la cuenta real.

**El catálogo NO va aquí.** Los 6 productos, los 9 precios, el IVU y el cupón los
crea `manage.py stripe_bootstrap` contra la llave live — más rápido y sin riesgo
de que un monto se desvíe del que anuncia la página. Este prompt cubre solo lo
que la API no puede tocar, más el webhook (para que el `whsec_` lo copies tú y no
pase por ningún chat).

Copia lo que está entre las líneas de guiones y pégalo en Claude en Chrome.

---

Estás en el dashboard de Stripe de mi cuenta. Necesito que configures la cuenta
**de producción** de DOMINIO (agentes de IA para negocios en Puerto Rico).

## Reglas

- **Antes de nada:** confirma que NO estás en un sandbox. Si ves el banner
  "You are testing in a sandbox" o la etiqueta *Sandbox* junto al nombre de la
  cuenta, sal de ahí a la cuenta real y dime que lo hiciste. Esto es dinero de
  verdad: si no estás seguro de en qué cuenta estás, **para y pregúntame**.
- **No crees productos, precios, cupones ni tax rates.** Eso lo hace un comando
  aparte. Si ves que ya existen, es correcto, déjalos.
- **No borres ni archives nada.**
- No pegues llaves ni secretos en el chat. Cuando algo sea secreto, dime dónde
  está para copiarlo yo.

## 1. Webhook

**Developers → Webhooks → Add endpoint**:

- Endpoint URL: `https://dominiopr.com/api/stripe/webhook/`
- Descripción: `DOMINIO - activacion de clientes`
- Destination name (si lo pide): `dominio-activacion-clientes`
- Eventos, exactamente estos cuatro y ninguno más:
  - `checkout.session.completed`
  - `checkout.session.async_payment_succeeded`
  - `customer.subscription.updated`
  - `customer.subscription.deleted`

Dame el **Destination ID** (`we_...`, no es secreto) y la URL de la página donde
puedo revelar el *Signing secret*. El `whsec_` lo copio yo.

## 2. Statement descriptor

**Settings → Business → Public business information** (o *Customer support* /
*Branding*, según cómo te salga el menú):

- Statement descriptor: `DOMINIOPR`
- Sube el logo y pon el color de marca si están vacíos.

Esto es lo que el cliente ve en el estado de cuenta de su tarjeta. En sandbox
Stripe lo rechazó con *"Only live keys can access this method"*, por eso va aquí.

## 3. Enlace a los términos en el portal

En **Public business information**, llena los enlaces públicos:

- Terms of service: `https://dominiopr.com/terminos/`
- Privacy policy: `https://dominiopr.com/privacidad/`

El customer portal los muestra desde ahí. En sandbox esa sección salía vacía con
el aviso *"Settings that affect your live integration are hidden in test mode"*.

## 4. Pagos fallidos → cancelar la suscripción (el importante)

**Settings → Billing → Subscriptions and emails** (o *Automatic collection* /
*Revenue recovery*, según la versión):

En el manejo de pagos fallidos (*"If all retries for a payment fail"* o
*Smart Retries*), después de agotar los reintentos escoge:

**→ Cancel the subscription**

No lo dejes en *Mark the subscription as unpaid* ni en *Leave the subscription
as-is*. Esto no es cosmético: mi aplicación desactiva al cliente cuando Stripe
manda `customer.subscription.deleted`. Con cualquier otra opción, alguien que
dejó de pagar se queda con el agente corriendo indefinidamente.

En sandbox este control salía **deshabilitado**, así que nadie lo ha confirmado
nunca. Dime explícitamente en qué estaba antes y en qué lo dejaste.

## 5. Customer portal

**Settings → Billing → Customer portal**:

- Actualizar método de pago: ON
- Cancelar suscripción: ON, en **"at end of billing period"** (no inmediata — el
  cliente ya pagó ese mes y le toca usarlo)
- Facturas / historial: ON
- No hace falta activar el "no-code link": mi app crea las sesiones por API.

## 6. Qué me tienes que devolver

- El `we_...` del webhook y el enlace donde revelo el `whsec_`
- Confirmación de que NO era un sandbox
- **Qué no pudiste hacer**, y en qué estado quedó cada cosa que sí tocaste. Si
  algo te salió distinto a lo que dice este documento, dilo explícitamente en
  vez de asumir que está bien.

---

## Lo que corre en paralelo (esto lo hago yo)

Con la llave `sk_live_` en `.env.live`:

```
manage.py stripe_bootstrap --yes --tax-rate 11.5 --tax-name IVU --coupon LANZAMIENTO
```

Crea en la cuenta real los 6 productos, los 9 precios, el IVU de 11.5% y el
cupón, y devuelve las 10 líneas `price_...` + `txr_...` para el `.env` **del
servidor**. Después `manage.py preflight` con esa llave verifica contra Stripe
que cada monto cuadra con lo que anuncia la página, y que el IVU es *exclusive*.

Al final, en el servidor: pegar las 11 líneas (10 + el `whsec_`), reiniciar
gunicorn, y correr `preflight` allí — donde `DEBUG`, cookies y `ALLOWED_HOSTS`
también tienen que salir en verde.
