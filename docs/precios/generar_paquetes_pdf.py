"""Propuesta de paquetes DOMINIO Chat 24/7 (rangos de precio, capacidad, instalación).

Uso:  python -I generar_paquetes_pdf.py Propuesta-paquetes-DOMINIO.pdf
Costos de IA medidos (docs/costos/Analisis-costos-agente-IA.pdf, sección 13);
tarifas de WhatsApp y competidores investigadas el 13 de octubre de 2026.
"""
import os
import sys

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (CondPageBreak, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table,
                                TableStyle)

OUT = sys.argv[1]

# ---------------------------------------------------------------- supuestos de costo
AI_PER_CONV = 0.0015       # web/WhatsApp: 6 mensajes, 7 llamadas, Haiku 5.5 low (medido)
AI_PER_INTERNAL_MSG = 0.0005  # asistente interno: prompt más largo (manuales), caché
WA_REPLIES_PER_CONV = 6    # respuestas del agente por conversación de WhatsApp
WA_MSGS_PER_CONV = 12      # entrantes + salientes (Twilio cobra ambos)
WA_META_SERVICE = 0.0113   # USD por respuesta de servicio, PR = "Rest of Latin America" (desde 1 oct 2026)
WA_FREE_SERVICE = 1000     # respuestas de servicio gratis por número al mes
WA_TWILIO = 0.005          # USD por mensaje (entrante y saliente)
WA_UTILITY = 0.0113        # plantilla de utilidad (recordatorio fuera de la ventana de 24 h)


def wa_cost(convs, reminders=0):
    replies = convs * WA_REPLIES_PER_CONV
    meta = max(0, replies - WA_FREE_SERVICE) * WA_META_SERVICE + reminders * WA_UTILITY
    twilio = (convs * WA_MSGS_PER_CONV + reminders) * WA_TWILIO
    return meta + twilio


# ---------------------------------------------------------------- paquetes
# (escalón, precio mensual, conversaciones web, conversaciones WhatsApp, asientos internos)
PACKAGES = [
    {
        'id': 'S', 'name': 'Chat Básico', 'size': 'Small', 'for': 'Negocios pequeños que quieren contestar '
        'preguntas y no perder clientes fuera de horario (barberías, tiendas, servicios a domicilio).',
        'steps': [('Entrada', 49.99, 150, 0, 0), ('Medio', 74.99, 300, 0, 0), ('Tope', 99.99, 450, 0, 0)],
        'setup': ('Instalación Express', 199, '48 horas'),
    },
    {
        'id': 'M', 'name': 'Recepcionista', 'size': 'Medium', 'for': 'Negocios que viven de citas y '
        'llamadas (oficinas profesionales, clínicas, salones, talleres): el agente atiende, agenda y toma recados.',
        'steps': [('Entrada', 149.00, 500, 0, 0), ('Medio', 219.00, 1000, 0, 0), ('Tope', 299.00, 1500, 0, 0)],
        'setup': ('Instalación Recepcionista', 499, '3–5 días laborables'),
    },
    {
        'id': 'L', 'name': 'Recepcionista + Equipo', 'size': 'Large', 'for': 'Negocios con empleados o '
        'varios locales: recepcionista para clientes y asistente interno para el equipo.',
        'steps': [('Entrada', 349.00, 1500, 0, 5), ('Medio', 449.00, 3000, 0, 10), ('Tope', 599.00, 4000, 0, 15)],
        'setup': ('Instalación Negocio', 999, '1–2 semanas'),
    },
    {
        'id': 'P', 'name': 'Premium WhatsApp', 'size': 'Premium', 'for': 'Negocios donde los clientes '
        'escriben por WhatsApp (55.8% de los usuarios en PR lo usan; es la app #1 de la isla).',
        'steps': [('Entrada', 699.00, 4000, 500, 15), ('Medio', 899.00, 4000, 1000, 15),
                  ('Tope', 1199.00, 4000, 2000, 25)],
        'setup': ('Instalación WhatsApp Business', 1499, '2–4 semanas (aprobación de Meta)'),
    },
]


def cost_of(step):
    _, price, web, wa, seats = step
    ai = (web + wa) * AI_PER_CONV + seats * 150 * AI_PER_INTERNAL_MSG
    whatsapp = wa_cost(wa, reminders=int(wa * 0.3)) if wa else 0.0
    return ai, whatsapp


# ---------------------------------------------------------------- estilos
ss = getSampleStyleSheet()
NAVY = colors.HexColor('#14213D')
ACCENT = colors.HexColor('#2563EB')
LIGHT = colors.HexColor('#EEF2FF')
GREY = colors.HexColor('#6B7280')
GREEN = colors.HexColor('#DCFCE7')
AMBER = colors.HexColor('#FEF3C7')
H1 = ParagraphStyle('H1', parent=ss['Title'], fontName='Helvetica-Bold', fontSize=22, textColor=NAVY,
                    alignment=TA_LEFT, spaceAfter=4)
SUB = ParagraphStyle('SUB', parent=ss['Normal'], fontSize=10.5, textColor=GREY, spaceAfter=12)
H2 = ParagraphStyle('H2', parent=ss['Heading2'], fontName='Helvetica-Bold', fontSize=14, textColor=NAVY,
                    spaceBefore=12, spaceAfter=6)
H3 = ParagraphStyle('H3', parent=ss['Heading3'], fontName='Helvetica-Bold', fontSize=11.5, textColor=NAVY,
                    spaceBefore=8, spaceAfter=4)
BODY = ParagraphStyle('BODY', parent=ss['Normal'], fontSize=10, leading=14, spaceAfter=6)
SMALL = ParagraphStyle('SMALL', parent=BODY, fontSize=8.5, leading=11, textColor=GREY)
BUL = ParagraphStyle('BUL', parent=BODY, leftIndent=14, bulletIndent=4, spaceAfter=3)
CELL = ParagraphStyle('CELL', parent=BODY, fontSize=8.5, leading=11, spaceAfter=0)
CELLB = ParagraphStyle('CELLB', parent=CELL, fontName='Helvetica-Bold', textColor=colors.white)
BOX = ParagraphStyle('BOX', parent=BODY, fontSize=10.5, leading=15, textColor=NAVY)


def table(rows, widths, shade=None):
    data = [[Paragraph(str(c), CELLB) for c in rows[0]]] + [[Paragraph(str(c), CELL) for c in r] for r in rows[1:]]
    t = Table(data, colWidths=widths, repeatRows=1)
    st = [('BACKGROUND', (0, 0), (-1, 0), NAVY), ('VALIGN', (0, 0), (-1, -1), 'TOP'),
          ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, LIGHT]),
          ('GRID', (0, 0), (-1, -1), 0.4, colors.HexColor('#CBD5E1')),
          ('TOPPADDING', (0, 0), (-1, -1), 3), ('BOTTOMPADDING', (0, 0), (-1, -1), 3)]
    for (r, color) in (shade or []):
        st.append(('BACKGROUND', (0, r), (-1, r), color))
    t.setStyle(TableStyle(st))
    return t


def callout(text):
    t = Table([[Paragraph(text, BOX)]], colWidths=[6.9 * inch])
    t.setStyle(TableStyle([('BACKGROUND', (0, 0), (-1, -1), LIGHT), ('LINEBEFORE', (0, 0), (0, -1), 3, ACCENT),
                           ('LEFTPADDING', (0, 0), (-1, -1), 10), ('RIGHTPADDING', (0, 0), (-1, -1), 10),
                           ('TOPPADDING', (0, 0), (-1, -1), 8), ('BOTTOMPADDING', (0, 0), (-1, -1), 8)]))
    return t


def bullets(items):
    return [Paragraph(i, BUL, bulletText='•') for i in items]


def usd(x):
    return f'${x:,.2f}' if x % 1 else f'${x:,.0f}'


def footer(canvas, doc):
    canvas.saveState()
    canvas.setFont('Helvetica', 8)
    canvas.setFillColor(GREY)
    canvas.drawString(0.8 * inch, 0.5 * inch, 'DOMINIO · Propuesta de paquetes DOMINIO Chat 24/7 · Octubre 2026')
    canvas.drawRightString(7.7 * inch, 0.5 * inch, f'Página {doc.page}')
    canvas.restoreState()


S = [Paragraph('Paquetes DOMINIO Chat 24/7', H1),
     Paragraph('Propuesta de precios por rangos · 13 de octubre de 2026 · Borrador para aprobación', SUB)]

S.append(callout(
    'Cuatro paquetes, cada uno con un <b>precio de entrada</b> y un <b>tope</b>. El precio sube por escalones según '
    'las conversaciones que el negocio usa; al pasar el tope, el siguiente paquete sale mejor. Cada paquete tiene su '
    '<b>instalación</b> hecha por DOMINIO. <b>Small ($49.99–$99.99)</b> contesta y capta leads; <b>Medium '
    '($149–$299)</b> es recepcionista con citas y recados; <b>Large ($349–$599)</b> suma un asistente interno para '
    'empleados y varios locales; <b>Premium ($699–$1,199)</b> añade WhatsApp Business con número propio.'))
S.append(Spacer(1, 6))

# ---- 1. Cómo funcionan los rangos
S.append(Paragraph('1. Cómo funcionan los rangos', H2))
S += bullets([
    '<b>La unidad es la conversación</b> (un visitante que escribe, hasta que deja de escribir). Es fácil de entender '
    'para el cliente y es lo que de verdad consume capacidad. Un mensaje suelto no cuenta como conversación nueva.',
    '<b>Tres escalones por paquete</b>: Entrada, Medio y Tope. El dashboard del cliente ya muestra su uso contra lo '
    'incluido y la proyección al cierre del mes, así que ve venir el cambio de escalón.',
    '<b>Al acercarse al tope</b> se le avisa (80%) y se le ofrece subir de paquete; no se corta el servicio de golpe '
    '(margen de 20% sobre el tope; el tope diario sigue protegiendo contra abuso).',
    '<b>Anual</b>: 10 meses de precio (2 gratis), sobre el escalón contratado.',
    '<b>Precios sin IVU</b>. El IVU de servicios digitales es 11.5%; un servicio gestionado podría ir al 4% '
    '(servicio profesional designado): <b>confirmar con un CPA</b> antes de publicar.',
])

# ---- 2. Paquetes
S.append(Paragraph('2. Los paquetes', H2))
rows = [['Paquete', 'Rango mensual', 'Capacidad incluida', 'Usuarios / asientos', 'Instalación']]
for p in PACKAGES:
    lo, hi = p['steps'][0], p['steps'][-1]
    cap = f'{lo[2]:,} conv. web' if lo[2] == hi[2] else f'{lo[2]:,}–{hi[2]:,} conv. web'
    if lo[3]:
        cap += f'<br/>{lo[3]:,}–{hi[3]:,} conv. WhatsApp'
    seats = '1 usuario' if p['id'] == 'S' else ('3 usuarios' if p['id'] == 'M' else
                                                 f'{lo[4]}–{hi[4]} asientos internos')
    rows.append([f'<b>{p["size"]}</b><br/>{p["name"]}', f'{usd(lo[1])} – {usd(hi[1])}', cap, seats,
                 f'{usd(p["setup"][1])}<br/>{p["setup"][2]}'])
rows.append(['<b>Enterprise</b><br/>A la medida', 'Desde $1,500', 'Según contrato', 'Según contrato', 'Cotización'])
S.append(table(rows, [1.35 * inch, 1.2 * inch, 1.6 * inch, 1.35 * inch, 1.4 * inch]))

FEATURES = {
    'S': ['Agente de IA en la página web, español e inglés, 24/7',
          'Aprende del sitio web (escaneo automático) y de 1 documento',
          'Contesta preguntas frecuentes y capta leads (nombre + email o teléfono verificado)',
          'Aviso de cada lead por email; correos falsos o desechables se rechazan',
          'Dashboard: leads, conversaciones y uso del plan',
          'Protección anti-bots (verificación humana)'],
    'M': ['Todo lo de Small',
          'Agenda citas según tus horarios, sin doble reserva, con recordatorio por email',
          'Toma recados (para quién, urgencia y número para devolver la llamada)',
          'Sabe si estás abierto o cerrado y lo dice; fuera de horario agenda o toma recado',
          'Pasa a una persona y te avisa por SMS o WhatsApp al instante',
          'Hasta 5 documentos y versiones del conocimiento; ficha de cada lead con notas',
          'Modo clínica (no pide ni guarda datos de salud)',
          'Afinación mensual del agente por DOMINIO'],
    'L': ['Todo lo de Medium',
          'Asistente interno para empleados: contesta con tus manuales y políticas, con login, sin acceso público',
          'Hasta 3 locales, cada uno con su horario y su agenda',
          'Avisos por departamento o local (quién recibe cada recado)',
          'Reportes de temas: qué preguntan los clientes y los empleados',
          'Hasta 15 documentos; soporte prioritario'],
    'P': ['Todo lo de Large',
          'WhatsApp Business con número propio verificado y el nombre del negocio',
          'El mismo agente contesta en WhatsApp y en la web (un solo dashboard)',
          'Confirmaciones y recordatorios de cita por WhatsApp (plantillas aprobadas por Meta)',
          'Costos de Meta y del proveedor incluidos hasta la capacidad del escalón',
          'Hasta 25 asientos internos; locales adicionales a $49/mes'],
}
for p in PACKAGES:
    block = [Paragraph(f'{p["size"]} — {p["name"]} ({usd(p["steps"][0][1])} a {usd(p["steps"][-1][1])}/mes)', H3),
             Paragraph(f'<i>Para quién:</i> {p["for"]}', BODY)]
    block += bullets(FEATURES[p['id']])
    S.append(KeepTogether(block))

# ---- 3. Escalones
S.append(CondPageBreak(3 * inch))
S.append(Paragraph('3. Escalones de precio dentro de cada paquete', H2))
rows = [['Paquete', 'Escalón', 'Precio / mes', 'Anual', 'Conv. web', 'Conv. WhatsApp', 'Asientos', 'Tope diario*']]
shade = []
for p in PACKAGES:
    for name, price, web, wa, seats in p['steps']:
        daily = int((web + wa) / 30 * 3)
        rows.append([p['size'], name, usd(price), usd(price * 10), f'{web:,}', f'{wa:,}' if wa else '—',
                     str(seats) if seats else ('1' if p['id'] == 'S' else '3'), f'{daily:,} conv.'])
    shade.append((len(rows) - 1, AMBER))
S.append(table(rows, [0.8 * inch, 0.7 * inch, 0.85 * inch, 0.85 * inch, 0.75 * inch, 1.0 * inch, 0.7 * inch,
                      1.0 * inch], shade=shade))
S.append(Paragraph('* Tope diario anti-abuso = 3 veces el promedio diario del escalón. Si un día entra un bot o hay '
                   'un pico, el gasto nunca pasa de ahí. Fila resaltada = tope del paquete; pasarlo = subir de paquete.',
                   SMALL))
S += bullets([
    '<b>Extras sin cambiar de paquete:</b> +100 conversaciones web $15; asiento interno adicional $9/mes; '
    'local adicional $49/mes (Large y Premium); +500 conversaciones de WhatsApp $149 (Premium).',
])

# ---- 4. Instalación
S.append(Paragraph('4. Paquetes de instalación (pago único)', H2))
SETUP = [
    ('Small', 'Express', 199, '48 horas', 'Escaneo del sitio con IA y revisión del conocimiento; configuración del '
     'agente (tono, idioma, saludo); instalación del widget (WordPress, Wix, Shopify, Squarespace o código); 1 ronda '
     'de ajustes.'),
    ('Medium', 'Recepcionista', 499, '3–5 días', 'Todo Express + horarios y reglas de citas, contactos de '
     'escalación, hasta 5 documentos, prueba con 20 preguntas reales del negocio antes de publicar, 30 min de '
     'entrenamiento al equipo, 2 rondas de ajustes.'),
    ('Large', 'Negocio', 999, '1–2 semanas', 'Todo Recepcionista + asistente interno (carga de manuales y '
     'políticas, hasta 15 documentos), cuentas y roles del equipo, hasta 3 locales con su horario, avisos por '
     'departamento, 60 min de entrenamiento.'),
    ('Premium', 'WhatsApp Business', 1499, '2–4 semanas', 'Todo Negocio + verificación del negocio en Meta, '
     'aprobación del nombre visible, número de WhatsApp propio, 3 plantillas aprobadas (confirmación, recordatorio y '
     'seguimiento), política de consentimiento y prueba completa en WhatsApp.'),
]
rows = [['Paquete', 'Instalación', 'Precio', 'Entrega', 'Incluye']]
for r in SETUP:
    rows.append([r[0], r[1], usd(r[2]), r[3], r[4]])
S.append(table(rows, [0.75 * inch, 1.0 * inch, 0.65 * inch, 0.8 * inch, 3.7 * inch]))
S += bullets([
    '<b>Opcionales:</b> migrar contactos o conectar un CRM (desde $750, cotización); reentrenamiento completo del '
    'conocimiento ($149); instalación en sitios a la medida sin CMS ($99).',
    '<b>Promoción sugerida</b>: instalación a mitad de precio con plan anual (el anual paga la instalación).',
])

# ---- 5. Costos y márgenes
S.append(CondPageBreak(3 * inch))
S.append(Paragraph('5. Lo que cuesta dar cada escalón y el margen', H2))
S.append(Paragraph(
    'IA medida: ~$0.0015 por conversación (Haiku 5.5, effort low, con caché). WhatsApp para números de PR: Meta '
    'cobra cada respuesta de servicio a ~$0.0113 después de las primeras 1,000 gratis al mes (desde el 1 de octubre '
    'de 2026; PR se factura como "resto de Latinoamérica", no como EE. UU.), Twilio cobra $0.005 por mensaje entrante '
    'o saliente, y cada recordatorio por plantilla ~$0.0113. Se supone un recordatorio por cada 3 conversaciones.',
    BODY))
rows = [['Paquete', 'Escalón', 'Precio', 'Costo IA', 'Costo WhatsApp', 'Costo total', 'Margen']]
for p in PACKAGES:
    for step in p['steps']:
        ai, wa = cost_of(step)
        total = ai + wa
        rows.append([p['size'], step[0], usd(step[1]), f'${ai:,.2f}', f'${wa:,.2f}' if wa else '—',
                     f'${total:,.2f}', f'{(step[1] - total) / step[1] * 100:.0f}%'])
S.append(table(rows, [0.85 * inch, 0.75 * inch, 0.85 * inch, 0.9 * inch, 1.1 * inch, 1.0 * inch, 0.85 * inch]))
S.append(Paragraph('El margen no descuenta servidor (~$10–20/mes entre todos los clientes), comisión de Stripe '
                   '(2.9% + $0.30) ni el tiempo de soporte y afinación, que es el costo real del servicio. En WhatsApp '
                   'la tarifa de Meta para PR es ~3.3 veces la de EE. UU.: por eso Premium incluye un volumen definido '
                   'y vende bloques extra.', SMALL))

# ---- 6. Disponibilidad
S.append(Paragraph('6. Qué existe hoy y qué hay que construir', H2))
S.append(Paragraph(
    'Revisión del código del 13 de octubre de 2026. <b>Importante:</b> el plan Scale publicado hoy promete '
    '"WhatsApp y multicanal", "hasta 3 locales" y "CRM y calendario", y ninguno existe todavía. Hay que quitarlo o '
    'marcarlo "próximamente" ya, antes de cualquier venta.', BODY))
rows = [['Función', 'Paquete', 'Estado', 'Trabajo estimado']]
AVAIL = [
    ('Chat web, escaneo de sitio y archivos, leads, emails verificados, anti-bots, dashboards', 'S', 'Listo', '—'),
    ('Citas con horario, recordatorio por email, escalación con aviso SMS/WhatsApp', 'M', 'Listo', '—'),
    ('Versiones del conocimiento, ficha del lead con notas, modo clínica', 'M', 'Listo', '—'),
    ('Tomar recados (herramienta take_message)', 'M', 'Construir', '1–2 días'),
    ('Saber si está abierto/cerrado y comportarse fuera de horario', 'M', 'Construir', '1 día'),
    ('Citas por teléfono sin email + recordatorio por SMS', 'M', 'Construir', '1–2 días'),
    ('Capacidad mensual por paquete, aviso al 80% y escalones', 'Todos', 'Construir', '4–6 días'),
    ('Asistente interno con login, asientos y conocimiento interno separado', 'L', 'Construir', '5–8 días'),
    ('Varios locales (agenda y horario por local) y avisos por departamento', 'L', 'Construir', '6–8 días'),
    ('WhatsApp Business (Twilio): entrada, respuestas, plantillas, costos', 'P', 'Construir', '10–15 días + 1–3 '
     'semanas de Meta'),
    ('Sincronizar Google/Outlook Calendar, CRM', 'Enterprise', 'Cotizar', '1–2 semanas por integración'),
    ('Voz / llamadas telefónicas', '—', 'Fuera de alcance', 'Requiere proveedor de voz; no recomendado ahora'),
]
shade = []
for i, r in enumerate(AVAIL, start=1):
    rows.append(list(r))
    shade.append((i, GREEN if r[2] == 'Listo' else AMBER if r[2] == 'Construir' else LIGHT))
S.append(table(rows, [3.5 * inch, 0.8 * inch, 1.0 * inch, 1.6 * inch], shade=shade))
S += bullets([
    '<b>Small se puede vender hoy.</b> Medium en ~1 semana (recados, horario y citas por teléfono). Large en ~3 '
    'semanas (más asistente interno y locales). Premium en ~5–6 semanas, empezando ya la verificación de Meta del '
    'primer cliente porque es lo que más tarda.',
    '<b>WhatsApp y clínicas:</b> Meta no firma acuerdos HIPAA; para clientes en modo clínica, WhatsApp solo con '
    'aceptación escrita o no se ofrece.',
])

# ---- 7. Mercado
S.append(CondPageBreak(3 * inch))
S.append(Paragraph('7. Comparación con el mercado (precios de 2026)', H2))
rows = [['Competidor', 'Tipo', 'Precio', 'Qué incluye / nota']]
MARKET = [
    ('Tidio + Lyro', 'Chatbot web, hazlo tú', '$29 + desde $39/mes', '50 conversaciones de IA en el add-on; ~$0.50–0.70 '
     'por conversación extra'),
    ('Chatbase', 'Chatbot web, hazlo tú', '$40 / $150 / $500 al mes', 'Créditos de mensajes; quitar la marca $99/mes'),
    ('Intercom Fin', 'Chatbot de soporte', '$0.99 por resolución', 'Mínimo 50 al mes, más asientos'),
    ('Goodcall', 'Recepcionista de voz', '$79 / $129 / $249 al mes', '100–500 llamantes; solo voz'),
    ('Smith.ai (IA)', 'Recepcionista de voz', '$150 / $270 / $500 al mes', '75–300 llamadas; ~$2.17–2.50 extra'),
    ('WATI / Respond.io', 'Plataforma de WhatsApp', '$59–349 al mes', 'Bandeja de WhatsApp; la IA es aparte y Meta se '
     'paga encima'),
    ('Agencias en EE. UU.', 'Hecho para ti', '$1,500–5,000 instalación + $150–500/mes', 'Lo más parecido a DOMINIO'),
]
for r in MARKET:
    rows.append(list(r))
S.append(table(rows, [1.2 * inch, 1.35 * inch, 1.55 * inch, 2.8 * inch]))
S += bullets([
    '<b>Posición de DOMINIO:</b> servicio hecho para ti, en español de Puerto Rico, con instalación más barata que '
    'una agencia ($199–1,499 contra $1,500–5,000) y mensualidad en el rango de las herramientas que el negocio '
    'tendría que configurar solo.',
    '<b>Small ($49.99)</b> compite con Tidio/Chatbase, pero ya instalado y en español. <b>Medium</b> compite con '
    'recepcionistas de IA de voz por menos dinero, por chat. <b>Premium</b> es WhatsApp + IA + instalación en un '
    'solo precio, algo que WATI o Respond.io no dan.',
])

# ---- 8. Decisiones
S.append(Paragraph('8. Decisiones para aprobar', H2))
S += bullets([
    '<b>Precios:</b> ¿los rangos y escalones de la sección 3 tal cual, o ajustar alguno?',
    '<b>Plan actual:</b> estos paquetes reemplazan Starter ($149), Pro ($299) y Scale ($499). Recomendado: mantener '
    'los ids internos (starter/pro/scale/custom) y cambiar nombres y precios, para no migrar Stripe ni datos.',
    '<b>Lanzamiento escalonado:</b> publicar Small y Medium primero; Large y Premium como "próximamente" con lista de '
    'espera hasta que estén construidos.',
    '<b>IVU:</b> confirmar con un CPA si aplica 11.5% o 4%.',
    '<b>WhatsApp:</b> empezar con Twilio (ya está integrado para avisos) y abrir la verificación de Meta del primer '
    'cliente Premium cuanto antes.',
])
S.append(Spacer(1, 6))
S.append(Paragraph('Fuentes: tarifas de WhatsApp (developers.facebook.com, twilio.com/en-us/whatsapp/pricing y guías '
                   'de proveedores sobre el cambio del 1 de octubre de 2026), precios de competidores en sitios de '
                   'reseñas de mayo a octubre de 2026 (verificar antes de citarlos públicamente), estudio PR Digital '
                   'Trends 2026 (WhatsApp 55.8%). Costos de IA medidos en la base de DOMINIO.', SMALL))

os.makedirs(os.path.dirname(os.path.abspath(OUT)), exist_ok=True)
doc = SimpleDocTemplate(OUT, pagesize=letter, leftMargin=0.8 * inch, rightMargin=0.8 * inch,
                        topMargin=0.7 * inch, bottomMargin=0.8 * inch,
                        title='Paquetes DOMINIO Chat 24/7', author='DOMINIO')
doc.build(S, onFirstPage=footer, onLaterPages=footer)
for p in PACKAGES:
    for step in p['steps']:
        ai, wa = cost_of(step)
        print(p['size'], step[0], step[1], f'ai={ai:.2f} wa={wa:.2f} margin={(step[1] - ai - wa) / step[1] * 100:.0f}%')
