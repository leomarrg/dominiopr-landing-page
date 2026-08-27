# 05 - Checklist web (SEO, accesibilidad, rendimiento)

Estado al **2026-08-26**. Complementa `03-checklist-lanzamiento.md`, que cubre
servidor, Stripe y correo. Este cubre la página en sí.

## Resumen

Los 20 puntos están hechos. Lo único que queda abierto es una decisión de diseño
(ver "Pendiente consciente" al final), no una tarea.

## Los 20 puntos

| # | Punto | Estado | Dónde |
|---|---|---|---|
| 1 | Política de privacidad | Hecho | `/privacy/` |
| 2 | Términos | Hecho | `/terms/` |
| 3 | CTAs claros en cada sección | Hecho | `index.html` |
| 4 | FAQ | Hecho | 6 en home `#faq`, 4 en get-started |
| 5 | `robots.txt` | Hecho | `landing/seo.py` |
| 6 | `sitemap.xml` | Hecho | `landing/seo.py` (django.contrib.sitemaps) |
| 7 | Página 404 y 500 | Hecho | `templates/404.html`, `500.html` |
| 8 | `alt` en todas las imágenes | Hecho | decorativas con `alt="" aria-hidden` |
| 9 | Analítica con consentimiento | Hecho | GA4 + banner en español |
| 10 | Meta title/description por página | Hecho | `_seo.html` |
| 11 | Open Graph / Twitter cards | Hecho | `_seo.html` + `og-image.png` 1200×630 |
| 12 | Favicons + webmanifest | Hecho | `images/brand/`, `site.webmanifest` |
| 13 | Canonical | Hecho | `_seo.html` |
| 14 | Banner de cookies | Hecho | traducido a español |
| 15 | Móvil sin overflow | Hecho | verificado a 375 px |
| 16 | Accesibilidad | Hecho | skip-link, aria-labels, foco, contraste AA |
| 17 | Formularios etiquetados | Hecho | `aria-label` en todos |
| 18 | Sin enlaces rotos | Hecho | crawl: 4 páginas, 0 rotos, 0 errores de consola |
| 19 | Datos estructurados | Hecho | JSON-LD FAQPage + ProfessionalService |
| 20 | Core Web Vitals | Hecho | ver abajo |

## Rendimiento

Medido con `.claude/skills/run-dominiopr-landing/cwv.mjs` (móvil = 375 px,
CPU ×4, red 1.6 Mbps).

| Métrica | Antes | Ahora |
|---|---|---|
| `load` escritorio (home) | 3.18 s | **0.19 s** |
| `load` móvil (home) | 4.9 s | **1.9 s** |
| CLS (home) | 0.093 | **0.000** |
| TBT escritorio | 93 ms | **3 ms** |
| TBT móvil (home) | 531 ms | **322 ms** |
| TBT móvil (get-started) | — | **175 ms** |
| Peticiones a terceros | 3 | **0** |

Tres cambios lo explican:

1. **Fuentes: un solo `preload`.** Precargar las dos familias competía con el CSS
   que bloquea el render. Dejando solo la del titular (el elemento LCP) mejoró 4G
   y 3G; el cuerpo entra un instante después vía `font-display: swap`.
2. **Logos redimensionados.** `logo_home.PNG` y `profile_pic@4x.PNG` eran de
   8004×8000 px (~350 KB cada uno) y se mostraban a 36–150 px: 714 KB por carga
   para dos imágenes diminutas, y ~256 MB de bitmap decodificado en RAM. Ahora
   son `brand/logo-wordmark.png` (300 px) y `brand/logo-mark.png` (112 px) — 2× el
   tamaño de render, **11.5 KB en total**. Los originales siguen en
   `images/logo/` como fuente; ninguna plantilla los referencia.
   Regenerar: `.claude/skills/run-dominiopr-landing/resize_logos.mjs`.
3. **Fuentes auto-hospedadas.** Ver `04-fuentes.md`.
4. **Trabajo pesado fuera del camino crítico.** La medición de las tarjetas
   flippables (`measureFaces`) forzaba un reflow sincrónico por tarjeta al cargar;
   ahora corre en `requestIdleCallback` con medición perezosa de respaldo. El
   cálculo inicial del dominó 3D del hero se movió a `requestAnimationFrame`.

## Accesibilidad del chat

Auditoría completa del widget "Ask DOMINIO" y del widget embebible que se instala
en sitios de clientes (`landing/templates/landing/widget.js`). Corregido en ambos:

- **Focus trap** en el panel abierto (antes se podía tabular al fondo de la página).
- **El foco vuelve al launcher** al cerrar (antes caía a `<body>`).
- `aria-modal`, `aria-expanded`, `role="log"` y `aria-label` en el input.
- **Contraste AA**: placeholder 3.77:1 → 5.96:1, disclaimer 3.46:1 → 5.9:1,
  borde de los chips 2.44:1 → 3:1+.
- **Tap targets 44×44** en enviar, cerrar y chips (eran 42×42, 24×34 y 34 de alto).
- Indicador de foco visible en el input (antes solo cambiaba 1 px de borde).
- "Escribiendo…" se anuncia a lectores de pantalla (antes eran 3 spans vacíos).
- `prefers-reduced-motion` en el widget embebible (su CSS se inyecta por JS y no
  lo alcanzaba la regla global del sitio).

Verificado en navegador: el foco no escapa en 14 tabs, Escape cierra y devuelve el
foco, los tap targets miden 44×44, y el texto de estado mide 1×1 px.

## Verificado end-to-end en navegador (2026-08-27)

No basta con que el código exista: esto se ejecutó contra el sitio corriendo.

**Formularios — 14/14, sin errores de consola.** Contacto (envía y confirma, y
rechaza un email inválido), alta (ahora exige escoger plan y confirma al
visitante), login (rechaza la contraseña mala, entra con la buena), las cinco
páginas del dashboard, el formulario de instalación (guarda y **reaparece al
volver** — antes se borraba), y recuperación de contraseña. No hay campo de
subida de archivos: el conocimiento del agente se pega como texto o URL.

**Accesibilidad — sin problemas.** Se corrigió: faltaba `<main>` en las cuatro
páginas, el skip-link solo existía en la home, los dos campos del demo no tenían
etiqueta, y `cookieSettings` estaba duplicado como id (HTML inválido). Contraste
AA calculado sobre los colores reales renderizados. Foco visible en 12 paradas de
teclado por página. Tap targets subidos a 44×44 en el menú móvil, el banner de
cookies y los enlaces del pie; quedan tres enlaces inline dentro de párrafos, que
WCAG 2.5.8 exceptúa expresamente.

**Cross-browser — los 3 motores pasan todo.** Chromium (Chrome/Edge), Firefox y
**WebKit (Safari)**, este último importante porque Safari en iPhone es una parte
grande del tráfico móvil en PR.

**Conexiones lentas** (medido con compresión, como producción):
4G lento 523 ms · 3G malo 771 ms · 2G 11.9 s hasta ver el titular.
Cuidado al medir en local: el servidor de desarrollo no comprime y nginx sí — sin
compresión, 3G daba 8.5 s en vez de 0.77 s.

## Pendiente consciente (decisión de diseño, no tarea)

El **TBT móvil de la home sigue en ~322 ms** (el umbral "bueno" es 200 ms). El
costo está en el dominó 3D del hero: son 59 nodos con transforms 3D, y cambiar
`--spin`/`--break` invalida el estilo de todo el subárbol. Ya se difirió lo que se
podía sin tocar el diseño.

Bajarlo más implica una decisión de producto — simplificar el dominó, o cargarlo
solo en pantallas grandes. **No se tocó**: el hero animado es parte de la marca.
`/get-started/`, que es la página a la que va el tráfico pagado, ya está en 175 ms.

Nota aparte: `manage.py check --deploy` sugiere `SECURE_HSTS_PRELOAD`. No se activó
porque enviar el dominio a la lista de preload de los navegadores es difícil de
revertir; vale la pena decidirlo a propósito, no de pasada.
