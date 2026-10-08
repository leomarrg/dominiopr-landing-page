"""Genera el PDF de gastos reales de IA durante el desarrollo (8 oct 2026).

Las cifras salen de (a) el resumen que imprime `agent_eval` en cada corrida y
(b) el libro de uso (UsageEvent) de la base local, que existe desde la fase 2.
Uso:  python -I generar_gastos_pdf.py Gastos-IA-desarrollo-2026-10-08.pdf
"""
import os
import sys

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

OUT = sys.argv[1]

# (actividad, hora PR, llamadas, entrada, salida, cache leído, cache escrito, costo, para qué, fuente)
ROWS = [
    ('Prueba corta de la API nueva (2 casos)', '10:50', 4, 1003, 712, 9264, 3088, 0.0009,
     'Confirmar que la clave nueva y Haiku 5.5 funcionan.', 'eval'),
    ('Eval completo, effort low (58 conversaciones)', '11:00', 90, 16444, 14703, 317997, 3573, 0.0126,
     'Comparar niveles de effort.', 'eval'),
    ('Eval completo, effort medium', '11:03', 90, 17078, 18696, 321570, 0, 0.0143,
     'Comparar niveles de effort.', 'eval'),
    ('Eval completo, effort high', '11:06', 90, 16710, 24935, 321570, 0, 0.0174,
     'Comparar niveles de effort.', 'eval'),
    ('Eval completo, effort auto', '11:09', 90, 17034, 16199, 321570, 0, 0.0130,
     'Comparar niveles de effort.', 'eval'),
    ('Eval de confirmación, low', '11:25', 92, 26536, 15475, 328716, 0, 0.0137,
     'Verificar el arreglo de la fecha para citas.', 'eval'),
    ('Eval de confirmación, auto', '11:28', 92, 25851, 14912, 328716, 0, 0.0133,
     'Verificar el arreglo de la fecha para citas.', 'eval'),
    ('Chat de prueba en el servidor local (2 mensajes)', '11:38', 2, 619, 719, 0, 6661, 0.0013,
     'Probar el libro de uso en vivo y el arreglo del prompt de IT.', 'ledger'),
    ('Eval de DOMINIO tras el arreglo de tools (29 casos)', '12:30', 40, 12923, 7329, 160785, 3573, 0.0070,
     'Confirmar que las tools (lead, cita, humano) se ejecutan.', 'ledger'),
    ('Eval de modo clínica (7 casos)', '12:31', 11, 4578, 2620, 39507, 3039, 0.0025,
     'Validar que no pide ni repite datos de salud.', 'ledger'),
    ('Etiquetado de temas (8 conversaciones)', '12:59', 8, 6014, 196, 0, 0, 0.0007,
     'Probar el etiquetado nocturno con la API real.', 'ledger'),
    ('Escaneo de dominiopr.com (4 páginas)', '13:40', 1, 12468, 3372, 0, 0, 0.0029,
     'Probar el escaneo del sitio con IA.', 'ledger'),
    ('Pregunta al chat de producción (1 mensaje)', '12:43', 1, 0, 0, 0, 0, 0.0006,
     'Confirmar en vivo que dominiopr.com ya dice que sí hace IT. (Estimado; queda en el '
     'libro de producción).', 'prod'),
]

TOTAL = sum(r[7] for r in ROWS)
EVALS = sum(r[7] for r in ROWS if 'Eval' in r[0] or 'Prueba corta' in r[0])

ss = getSampleStyleSheet()
NAVY = colors.HexColor('#14213D')
ACCENT = colors.HexColor('#2563EB')
LIGHT = colors.HexColor('#EEF2FF')
GREY = colors.HexColor('#6B7280')
H1 = ParagraphStyle('H1', parent=ss['Title'], fontName='Helvetica-Bold', fontSize=22, textColor=NAVY,
                    alignment=TA_LEFT, spaceAfter=4)
SUB = ParagraphStyle('SUB', parent=ss['Normal'], fontSize=10.5, textColor=GREY, spaceAfter=14)
H2 = ParagraphStyle('H2', parent=ss['Heading2'], fontName='Helvetica-Bold', fontSize=14, textColor=NAVY,
                    spaceBefore=14, spaceAfter=6)
BODY = ParagraphStyle('BODY', parent=ss['Normal'], fontSize=10, leading=14, spaceAfter=6)
SMALL = ParagraphStyle('SMALL', parent=BODY, fontSize=8.5, leading=11, textColor=GREY)
BUL = ParagraphStyle('BUL', parent=BODY, leftIndent=14, bulletIndent=4, spaceAfter=3)
CELL = ParagraphStyle('CELL', parent=BODY, fontSize=8.5, leading=11, spaceAfter=0)
CELLB = ParagraphStyle('CELLB', parent=CELL, fontName='Helvetica-Bold', textColor=colors.white)
BOX = ParagraphStyle('BOX', parent=BODY, fontSize=10.5, leading=15, textColor=NAVY)


def table(rows, widths, bold_last=False):
    data = [[Paragraph(str(c), CELLB) for c in rows[0]]] + [[Paragraph(str(c), CELL) for c in r] for r in rows[1:]]
    t = Table(data, colWidths=widths, repeatRows=1)
    st = [('BACKGROUND', (0, 0), (-1, 0), NAVY), ('VALIGN', (0, 0), (-1, -1), 'TOP'),
          ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, LIGHT]),
          ('GRID', (0, 0), (-1, -1), 0.4, colors.HexColor('#CBD5E1')),
          ('TOPPADDING', (0, 0), (-1, -1), 3), ('BOTTOMPADDING', (0, 0), (-1, -1), 3)]
    if bold_last:
        st.append(('BACKGROUND', (0, -1), (-1, -1), colors.HexColor('#DBEAFE')))
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


def footer(canvas, doc):
    canvas.saveState()
    canvas.setFont('Helvetica', 8)
    canvas.setFillColor(GREY)
    canvas.drawString(0.8 * inch, 0.5 * inch, 'DOMINIO · Gastos de IA durante el desarrollo · 8 de octubre de 2026')
    canvas.drawRightString(7.7 * inch, 0.5 * inch, f'Página {doc.page}')
    canvas.restoreState()


S = [Paragraph('Gastos de IA durante el desarrollo', H1),
     Paragraph('DOMINIO · 8 de octubre de 2026 · Clave nueva de Anthropic, modelo Claude Haiku 5.5', SUB)]

S.append(Paragraph('Resumen', H2))
S.append(callout(
    f'Hoy se gastaron <b>~${TOTAL:.2f}</b> en la API de Anthropic, y <b>todo fue en pruebas de desarrollo</b>: '
    f'ningún cliente generó gasto. El <b>{EVALS / TOTAL * 100:.0f}%</b> (${EVALS:.3f}) fueron las evaluaciones '
    'que compararon niveles de effort y verificaron arreglos. El resto: un chat de prueba, el etiquetado de '
    'temas, el escaneo de dominiopr.com y una pregunta al chat de producción.'))
S.append(Spacer(1, 8))
S += bullets([
    '<b>Cuadra con lo que ves en la consola de Anthropic</b> (~$0.10). Pequeñas diferencias de centavos '
    'son por redondeo y por la hora de corte del panel.',
    '<b>Cada gasto dejó algo concreto</b> (sección 3): se eligió el effort, se encontraron y confirmaron '
    'tres bugs (fecha para citas, tools que no se ejecutaban, prompt que decía "no hacemos IT") y se '
    'validaron el modo clínica, el etiquetado y el escaneo del sitio.',
    '<b>Desde la fase 2 cada llamada queda registrada</b> en el libro de uso, así que estos gastos se ven '
    'por cliente y por tipo en el centro de control (/dashboard/control/).',
])

S.append(Paragraph('1. Desglose por actividad', H2))
rows = [['Actividad', 'Hora PR (aprox.)', 'Llamadas', 'Tokens entrada / salida', 'Costo', 'Para qué']]
for r in ROWS:
    tokens = '—' if r[9] == 'prod' else f'{r[3] + r[5] + r[6]:,} / {r[4]:,}'
    rows.append([r[0], r[1], r[2], tokens, f'${r[7]:.4f}', r[8]])
rows.append(['<b>Total</b>', '', f'<b>{sum(r[2] for r in ROWS)}</b>', '', f'<b>${TOTAL:.4f}</b>', ''])
S.append(table(rows, [1.9 * inch, 0.55 * inch, 0.55 * inch, 1.15 * inch, 0.6 * inch, 2.15 * inch], bold_last=True))
S.append(Paragraph('"Tokens de entrada" incluye los leídos del caché (cobrados al 10%). Fuente: resumen de cada '
                   'corrida de agent_eval y el libro de uso (UsageEvent) de la base local.', SMALL))

S.append(Paragraph('2. Por categoría', H2))
cats = {}
for r in ROWS:
    key = ('Evaluaciones (elegir effort y verificar arreglos)' if ('Eval' in r[0] or 'Prueba corta' in r[0])
           else r[0])
    cats[key] = cats.get(key, 0) + r[7]
crow = [['Categoría', 'Costo', '% del total']]
for k, v in sorted(cats.items(), key=lambda kv: -kv[1]):
    crow.append([k, f'${v:.4f}', f'{v / TOTAL * 100:.0f}%'])
S.append(table(crow, [4.6 * inch, 1.1 * inch, 1.2 * inch]))

tin = sum(r[3] for r in ROWS); tout = sum(r[4] for r in ROWS)
tcr = sum(r[5] for r in ROWS); tcw = sum(r[6] for r in ROWS)
S.append(Spacer(1, 8))
S.append(table([
    ['Tipo de token', 'Tokens', 'Precio Haiku 5.5 / millón', 'Costo aprox.'],
    ['Entrada sin caché', f'{tin:,}', '$0.10', f'${tin * 0.10 / 1e6:.4f}'],
    ['Salida (respuesta + razonamiento)', f'{tout:,}', '$0.50', f'${tout * 0.50 / 1e6:.4f}'],
    ['Leídos del caché', f'{tcr:,}', '$0.01', f'${tcr * 0.01 / 1e6:.4f}'],
    ['Escritos al caché', f'{tcw:,}', '$0.125', f'${tcw * 0.125 / 1e6:.4f}'],
], [2.6 * inch, 1.3 * inch, 1.6 * inch, 1.4 * inch]))
S.append(Paragraph(f'El caché sirvió el {tcr / (tin + tcr) * 100:.0f}% de la entrada a una décima parte del precio '
                   f'(sin caché, esa entrada habría costado ${(tin + tcr) * 0.10 / 1e6:.3f} en vez de '
                   f'${tin * 0.10 / 1e6 + tcr * 0.01 / 1e6:.3f}). La mayor parte del gasto es la salida: las '
                   'respuestas y el razonamiento del modelo.', SMALL))

S.append(KeepTogether([Paragraph('3. Lo que dejó cada gasto', H2)] + bullets([
    '<b>Effort elegido con datos ($0.057):</b> low, medium, high y auto dieron el mismo resultado; low es el más '
    'rápido y barato. Decisión: low por defecto.',
    '<b>Bug de citas encontrado ($0.027 en la verificación):</b> el agente no sabía la fecha de hoy y no podía '
    'agendar "el próximo martes". Arreglado y verificado (58/58).',
    '<b>Bug de las tools confirmado ($0.007):</b> tras un cambio interno, las tools dejaron de ejecutarse; el eval '
    'real confirmó el arreglo antes de subir a producción (29/29).',
    '<b>Prompt de IT ($0.0019):</b> el chat local mostró que el agente decía "no hacemos IT" (su copia del '
    'conocimiento estaba vieja); tras el deploy, la pregunta a producción confirmó que ahora dice que sí.',
    '<b>Modo clínica validado ($0.0025):</b> no da consejos médicos, no repite datos de salud y manda emergencias '
    'al 911 (7/7).',
    '<b>Etiquetado y escaneo ($0.0036):</b> etiquetar 8 conversaciones costó $0.0007; escanear tu sitio, $0.003.',
])))

S.append(KeepTogether([Paragraph('4. Lo que costará en producción', H2)] + bullets([
    'Un mensaje del chat cuesta ~$0.00017 con Haiku 5.5 (medido). Un cliente típico (300 conversaciones al mes) '
    'gasta ~$1 al mes en IA.',
    'Etiquetar los temas cuesta ~$0.0001 por conversación; escanear un sitio, ~$0.003 por escaneo.',
    'Los topes diarios por plan y el tope global siguen limitando el gasto máximo.',
    'El análisis completo de costos y proyecciones está en Analisis-costos-agente-IA.pdf.',
])))

S.append(Spacer(1, 8))
S.append(Paragraph('Notas: las horas son de Puerto Rico. Las corridas de evaluación anteriores a la fase 2 no '
                   'están en el libro de uso (no existía); sus cifras salen del resumen que imprimió cada corrida. '
                   'La pregunta al chat de producción es un estimado: su costo exacto quedó en el libro de uso '
                   'del servidor.', SMALL))

os.makedirs(os.path.dirname(os.path.abspath(OUT)), exist_ok=True)
doc = SimpleDocTemplate(OUT, pagesize=letter, leftMargin=0.8 * inch, rightMargin=0.8 * inch,
                        topMargin=0.7 * inch, bottomMargin=0.8 * inch,
                        title='Gastos de IA durante el desarrollo', author='DOMINIO')
doc.build(S, onFirstPage=footer, onLaterPages=footer)
print(f'total=${TOTAL:.4f} evals=${EVALS:.4f}')
