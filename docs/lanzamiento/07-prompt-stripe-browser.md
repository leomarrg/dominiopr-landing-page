# Prompt para Claude en Chrome — crear el catálogo de Stripe

Copia **todo lo que está entre las líneas de guiones** y pégalo en Claude en Chrome,
con una pestaña abierta en `https://dashboard.stripe.com` y tu sesión iniciada.

Advertencia antes de correrlo: hacerlo por el dashboard **no puede fijar los IDs de
producto** (`dominio_starter`, etc.) que sí fija el comando `manage.py stripe_bootstrap`.
Si después corres ese comando contra la misma cuenta, va a crear 6 productos duplicados.
Escoge un camino y quédate en él. Los *lookup keys* de los precios sí se pueden poner a
mano, y el prompt los incluye — eso es lo que hace que `preflight` pueda verificar.

---

Estás en el dashboard de Stripe de mi cuenta. Necesito que crees el catálogo de
productos y precios de mi negocio (DOMINIO — agentes de IA para negocios en Puerto
Rico). Sigue estos pasos exactamente y no inventes valores.

## Reglas

- **Antes de nada:** confirma en la esquina superior del dashboard que el
  interruptor **Test mode** está ENCENDIDO. Si está apagado, enciéndelo y avísame.
  Todo esto va primero en modo prueba.
- Moneda: **USD** en todos los precios.
- Si un producto o precio con ese nombre ya existe, NO lo dupliques: dime que ya
  existe y sigue con el próximo.
- **No borres ni archives nada.** Si algo no cuadra, para y pregúntame.
- Después de crear cada precio, entra al precio y **copia su ID** (empieza con
  `price_`). Los vas a necesitar todos al final.

## Paso 1 — Los 6 productos y 9 precios

Ve a **Product catalog → Products → + Add product** y crea, uno por uno:

### 1. `DOMINIO Starter`
Descripción: `Agente de IA para tu negocio - plan Starter`
Dos precios en este mismo producto:
| Precio | Monto | Tipo | Lookup key |
|---|---|---|---|
| Mensual | **$99.00** | Recurring, monthly | `starter-monthly` |
| Anual | **$990.00** | Recurring, yearly | `starter-annual` |

### 2. `DOMINIO Pro`
Descripción: `Agente de IA para tu negocio - plan Pro`
| Precio | Monto | Tipo | Lookup key |
|---|---|---|---|
| Mensual | **$299.00** | Recurring, monthly | `pro-monthly` |
| Anual | **$2,990.00** | Recurring, yearly | `pro-annual` |

### 3. `DOMINIO Scale`
Descripción: `Agente de IA para tu negocio - plan Scale`
| Precio | Monto | Tipo | Lookup key |
|---|---|---|---|
| Mensual | **$499.00** | Recurring, monthly | `scale-monthly` |
| Anual | **$4,990.00** | Recurring, yearly | `scale-annual` |

### 4. `Instalacion y configuracion - Starter`
Descripción: `Cargo unico por instalacion, configuracion y entrenamiento inicial del agente`
| Precio | Monto | Tipo | Lookup key |
|---|---|---|---|
| Instalación | **$500.00** | **One-off / One time** | `starter-setup` |

### 5. `Instalacion y configuracion - Pro`
Misma descripción que la #4.
| Precio | Monto | Tipo | Lookup key |
|---|---|---|---|
| Instalación | **$1,000.00** | **One-off / One time** | `pro-setup` |

### 6. `Instalacion y configuracion - Scale`
Misma descripción que la #4.
| Precio | Monto | Tipo | Lookup key |
|---|---|---|---|
| Instalación | **$2,500.00** | **One-off / One time** | `scale-setup` |

**Sobre el lookup key:** al crear o editar el precio, ábrelo y busca la sección
**Advanced** (a veces "More options" o el menú `…` del precio → *Edit price*).
Ahí hay un campo **Lookup key**. Ponlo exactamente como dice la tabla, en
minúsculas y con guion. Si no encuentras el campo en la pantalla de creación,
crea el precio primero y edítalo después. Si de plano no aparece, dímelo y sigue
sin él — pero avísame cuáles quedaron sin lookup key.

Ojo con los montos anuales: **$990, $2,990 y $4,990** son el precio de *todo el
año*, no mensual. El tipo tiene que ser recurring con intervalo **yearly**.

## Paso 2 — Webhook

Ve a **Developers → Webhooks → Add endpoint**.

- Endpoint URL: `https://dominiopr.com/api/stripe/webhook/`
- Descripción: `DOMINIO - activacion de clientes`
- Eventos a escuchar (exactamente estos cuatro):
  - `checkout.session.completed`
  - `checkout.session.async_payment_succeeded`
  - `customer.subscription.updated`
  - `customer.subscription.deleted`

Cuando lo crees, Stripe muestra un **Signing secret** que empieza con `whsec_`.
Haz clic en *Reveal* y **cópialo** — hay que guardarlo ahora.

## Paso 3 — Cupón de lanzamiento

**Product catalog → Coupons → + New**:
- Tipo: **Percentage discount**, **20%**
- Duration: **Repeating**, **3 months**
- Nombre: `Lanzamiento`

Luego crea un **promotion code** para ese cupón con el código `LANZAMIENTO`
(en mayúsculas).

## Paso 4 — Las 3 pantallas de configuración

1. **Settings → Business → Branding:** sube el logo, color de marca, y pon el
   *statement descriptor* (lo que sale en el estado de cuenta del cliente) como
   `DOMINIOPR`.
2. **Settings → Billing → Customer portal:** actívalo. Permite que el cliente
   actualice su método de pago y cancele. Enlaza los términos de
   `https://dominiopr.com/terminos/`.
3. **Settings → Billing → Automatic collection / Subscriptions:** en el manejo de
   pagos fallidos, después de los reintentos escoge **Cancel the subscription**.
   Esto es importante: es lo que hace que mi aplicación desactive al cliente sola.

## Paso 5 — Lo que me tienes que devolver

Cuando termines, dame este bloque de texto lleno con los IDs reales que copiaste.
No lo resumas ni lo reformatees — lo voy a pegar tal cual en un archivo de
configuración:

```
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

Y aparte dime: qué creaste, qué ya existía, y **qué no pudiste hacer**. Si algo te
salió distinto a lo que pide este documento, dilo explícitamente en vez de
asumir que está bien.

Al final necesito también la **llave secreta de test**: ve a
**Developers → API keys**, dale *Reveal test key*, y dámela (empieza con
`sk_test_`). Es la de prueba, no la de producción.

---

## Después (esto lo hago yo)

Pégame el bloque de vuelta en Claude Code. Yo:

1. Lo escribo en el `.env` local y en el del servidor.
2. Corro `python manage.py preflight` — verifica **contra Stripe** que cada monto
   que Stripe va a cobrar es igual al que anuncia la página. Es el gate de go-live.
3. Probamos una compra completa con la tarjeta `4242 4242 4242 4242` y confirmamos
   que el webhook crea el cliente, el dashboard y el correo de bienvenida.
4. Repetimos el Paso 1–3 en **live** (mismo prompt, con Test mode APAGADO) y
   pegamos los `price_` de producción.
