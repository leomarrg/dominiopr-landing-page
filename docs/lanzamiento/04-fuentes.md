# Fuentes web auto-hospedadas

**Estado:** hecho (2026-08-26). No hay que tocar nada en cada deploy.

## Qué cambió y por qué

Antes cada página cargaba las fuentes desde Google:

```html
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Montserrat…" rel="stylesheet">
```

Eso tenía dos costos:

1. **Rendimiento.** Ese `<link>` bloquea el render: el navegador tiene que ir a
   `fonts.googleapis.com`, bajar 12 KB de CSS, y recién ahí descubrir los `.woff2`
   en `fonts.gstatic.com` — dos conexiones nuevas antes de pintar texto.
2. **Privacidad.** Cada visitante le entregaba su IP y su User-Agent a Google sin
   haber aceptado nada. Con banner de cookies propio, eso era incoherente.

Ahora las fuentes viven en el repo y se sirven desde nuestro dominio:

```html
<link rel="preload" href="…/fonts/montserrat-latin-var.woff2" as="font" type="font/woff2" crossorigin>
<link rel="preload" href="…/fonts/space-grotesk-latin-var.woff2" as="font" type="font/woff2" crossorigin>
<link rel="stylesheet" href="…/css/fonts.css">
```

**Resultado medido:** cero peticiones a terceros en toda la página, y en escritorio
el `load` de la home bajó de 3.18 s a 0.19 s.

## Archivos

| Archivo | Qué es |
|---|---|
| `tools/fetch_fonts.py` | Script que descarga y regenera todo. Única fuente de verdad. |
| `landing/static/landing/css/fonts.css` | Los `@font-face`. **Generado — no editar a mano.** |
| `landing/static/landing/fonts/*.woff2` | 4 archivos, 146 KB en disco. |

Son **fuentes variables**: un archivo por familia y subset cubre los pesos 400–700.
Pedir pesos sueltos (`wght@400;500;600;700`) daría 16 archivos y 585 KB.

Solo se incluyen los subsets `latin` y `latin-ext`. El español de PR (á, é, í, ó, ú,
ñ, ü, ¿, ¡) cae completo dentro de `latin`, así que en la práctica el visitante baja
unos 60 KB — lo mismo que bajaba de Google, pero sin el salto a terceros.

## Cómo regenerarlas

Solo hace falta si cambian las familias o los pesos del diseño:

```bash
venv/Scripts/python.exe tools/fetch_fonts.py   # Windows
venv/bin/python tools/fetch_fonts.py           # POSIX
```

Reescribe `fonts.css`, baja lo que falte y borra los `.woff2` que ya no se usen.
**Commitea el CSS y los `.woff2` juntos.**

Si cambias de familia, edita `CSS_URL` en el script y actualiza también los dos
`<link rel="preload">` en las plantillas (`grep -rn "rel=\"preload\"" landing/templates/`).

### Detalle que rompe el script en silencio

Google decide el formato **según el User-Agent**. Con un UA abreviado devuelve
`.ttf` (unas 3× más pesado) en vez de `.woff2`. Por eso el script manda un UA de
navegador completo y aborta si la respuesta no trae `woff2`.

## Deploy

No requiere pasos nuevos. `collectstatic` con
`CompressedManifestStaticFilesStorage` reescribe las rutas relativas de `fonts.css`
a los nombres con hash:

```
url("../fonts/montserrat-latin-var.311d352d9323.woff2")
```

y nginx ya sirve `/static/` con `Cache-Control: immutable` por un año, así que el
visitante recurrente no vuelve a bajar las fuentes.
