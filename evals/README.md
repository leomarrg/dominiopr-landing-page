# Evals del agente

Formato de los casos en `landing/management/commands/agent_eval.py`
(`turno1 >> turno2 | esperado | !prohibido | tool=x | notool=x | maxlen=N | onequestion`).
Nunca se llama a la API en tests; los comandos de abajo si gastan tokens.

## Archivos

| Archivo | Cliente / fixture | Banderas | Casos |
| --- | --- | --- | --- |
| `dominio.txt` | `dominio` | (perfil web) | agente de DOMINIO |
| `clinica.txt` | cliente con `clinic_mode` (`eval-clinica` si existe) | (perfil web) | modo clinica |
| `recepcion.txt` | `eval-recepcion` | `--profile receptionist --hours open` | recepcionista, abierto (9) |
| `recepcion-cerrado.txt` | `eval-recepcion` | `--profile receptionist --hours closed` | recepcionista, cerrado (1) |
| `interno.txt` | `eval-interno` | `--profile internal` | asistente de empleados (7) |
| `whatsapp.txt` | `eval-recepcion` | `--profile receptionist --channel whatsapp --hours open` | estilo WhatsApp (3) |
| `inyeccion-perfiles.txt` | mixto (directivas por caso) | ninguna obligatoria | inyeccion por perfil (5) |
| `locales.txt` | `eval-recepcion` (2 locales) | `--profile receptionist --hours open` | multi-sucursal, Fase F (9) |

Cada archivo documenta en su cabecera el comando exacto. Los 25 casos de
perfiles salen de `docs/agente/reglas-por-perfil.md`, seccion C; los 9 de
`locales.txt` cubren B9 (pide el local antes de reservar, lo acepta o lo
infiere sin re-preguntar, horario y telefono por local, recado con local, no
inventa un tercer local).

## Fixtures locales

    python manage.py seed_eval_clients

Crea o actualiza (idempotente; solo con `DEBUG=True`, `--force` para una BD
local sin DEBUG) los clientes `eval-recepcion` (salon con dos `Location`:
Bayamon, principal, hereda las `AvailabilityRule` del negocio L-V 9-5; Caguas
con reglas propias L-V 10-6; departamentos Ventas y Facturacion; citas
activas; sin tercer local) y `eval-interno` (dos `KnowledgeSource` activas:
"Manual del Empleado" y "Politica de Vacaciones"). Si existe `eval-clinica`
lo reutiliza. Pone `agent_profile` cuando el modelo admite el valor:
`Client.AGENT_PROFILE_CHOICES` no incluye `internal` (el asistente interno
fija el perfil en su propia vista), asi que `interno.txt` siempre lleva
`--profile internal`.

## Banderas del comando

- `--client <slug>` o su alias `--fixture <slug>`.
- `--profile web|receptionist|internal`: se pasa a `agent.answer` solo cuando
  no es `web` (mismo contrato que `turns.run_turn`). Sin la bandera se usa el
  `agent_profile` del cliente si el campo existe.
- `--channel web|whatsapp`: idem, solo se pasa `whatsapp`. No se inyecta la
  linea `CHANNEL:` con numero del visitante.
- `--hours open|closed|none`: anade la linea fija `HOURS NOW (authoritative):
  ...` (docs B8) al bloque de sistema NO cacheado de cada llamada al modelo.
  Si `agent.answer` acepta `context_lines` (Phase D) la linea viaja por ahi,
  igual que en produccion; si no, el comando la inyecta en la costura
  `agent._provider_client` como ultimo bloque y sustituye cualquier linea de
  horario del motor, para que el estado abierto/cerrado lo fije el eval. La
  parte `hours:` sale de las `AvailabilityRule` del cliente. Con 2+ `Location`
  (Fase F) la linea es el bloque por local de `turns.hours_now_lines`
  (`HOURS NOW (authoritative, per location):` + una linea `[Local] ...` por
  local con sus propias reglas o las del negocio), todos en el mismo estado.
- `multi_location=True` se pasa a `agent.answer` con la misma condicion que
  `turns.run_turn` (2+ locales y perfil distinto de `internal`), asi que las
  reglas LOCATIONS si estan bajo prueba. Las herramientas falsas nunca
  rechazan un local ausente (el handler real si): "pide el local" se
  comprueba con `notool=create_booking`.
- Directivas por caso que sobreescriben las banderas: `client=`, `profile=`,
  `channel=`, `hours=` (ver `inyeccion-perfiles.txt`).
- `--effort`, `--model`, `--reps`, `--out`, `--include-draft`,
  `--default-business` como antes.

La ultima linea del resumen no cambia (casos, fallos, latencia, tokens,
costo); debajo se imprime `directivas: esperado=7/8 prohibido=12/12 ...`
(pasadas/comprobadas por tipo de directiva). El transcript (`--out`) anade
una linea `input <tool>: {...}` por cada herramienta llamada, para revisar a
mano lo que las directivas no comprueban (`location=`, `department=`,
`summary`).

## Corridas

    python manage.py agent_eval --client dominio --file evals/dominio.txt --effort low --reps 2 --out evals/results/<fecha>-dominio-low.txt
    python manage.py agent_eval --fixture eval-recepcion --file evals/recepcion.txt --profile receptionist --hours open --effort low --reps 2 --out evals/results/<fecha>-recepcion-low.txt
    python manage.py agent_eval --fixture eval-recepcion --file evals/recepcion-cerrado.txt --profile receptionist --hours closed --effort low --reps 2 --out evals/results/<fecha>-recepcion-cerrado-low.txt
    python manage.py agent_eval --fixture eval-interno --file evals/interno.txt --profile internal --effort low --reps 2 --out evals/results/<fecha>-interno-low.txt
    python manage.py agent_eval --fixture eval-recepcion --file evals/whatsapp.txt --profile receptionist --channel whatsapp --hours open --effort low --reps 2 --out evals/results/<fecha>-whatsapp-low.txt
    python manage.py agent_eval --fixture eval-recepcion --file evals/inyeccion-perfiles.txt --effort low --reps 2 --out evals/results/<fecha>-inyeccion-perfiles-low.txt
    python manage.py agent_eval --fixture eval-recepcion --file evals/locales.txt --profile receptionist --hours open --effort low --reps 2 --out evals/results/<fecha>-locales-low.txt

`results/` guarda las transcripciones de cada corrida (la ultima linea es el
resumen: casos, fallos, latencia, tokens y costo). Los archivos `*-prefecha`
son de antes de darle al agente la fecha de hoy (la cita de "el proximo
martes" fallaba en todos los niveles); con el arreglo, low y auto pasan 58/58.
Decision 2026-10-08: effort `low` (ver docs/costos/Analisis-costos-agente-IA.pdf, seccion 11).

Corrida 2026-10-15 (Fase H, Haiku 5.5, effort low, reps 2, API real), tras
las fases D-G y con `eval-recepcion` ya multi-sucursal:

| Fixture | Casos | Pasados | Latencia prom. | Llamadas | Costo |
| --- | --- | --- | --- | --- | --- |
| `dominio.txt` | 58 | 58 | 2.30 s | 92 | $0.0145 |
| `clinica.txt` | 14 | 14 | 2.73 s | 28 | $0.0052 |
| `recepcion.txt` | 18 | 18 | 2.71 s | 44 | $0.0081 |
| `recepcion-cerrado.txt` | 2 | 2 | 1.96 s | 2 | $0.0003 |
| `interno.txt` | 14 | 14 | 2.06 s | 16 | $0.0027 |
| `whatsapp.txt` | 6 | 6 | 1.74 s | 8 | $0.0014 |
| `inyeccion-perfiles.txt` | 10 | 10 | 2.16 s | 12 | $0.0016 |
| `locales.txt` | 18 | 18 | 2.17 s | 38 | $0.0066 |
| Total | 140 | 140 | | 240 | $0.0404 |

Sin regresiones en `dominio.txt` ni `clinica.txt`; ningun caso necesito
effort medium. Observaciones que las directivas no miden (ver transcripts):
el perfil interno anade "Fuente: Manual del Empleado" tambien cuando dice
que el tema no esta en los documentos (solo nombra el contacto de RRHH que
si sale del manual); en el recado urgente de la inundacion el recepcionista
guardo el mensaje con el local sin resolver y lo pregunto despues, y escribio
el `summary` en ingles. Pendientes de decision, no fallos.

Repeticion 2026-10-15 (misma configuracion) tras los dos ajustes aprobados
("Fuente:" solo cuando la respuesta sale del documento; recado sin local con
2+ locales = rechazo suave; `summary` en el idioma del visitante) y con los
handlers falsos del eval aplicando ya la regla del local:

| Fixture | Casos | Pasados | Latencia prom. | Llamadas | Costo | Archivo |
| --- | --- | --- | --- | --- | --- | --- |
| `interno.txt` | 14 | 14 | 1.73 s | 16 | $0.0025 | `2026-10-15-interno-low-v2.txt` |
| `recepcion.txt` | 18 | 18 | 2.54 s | 47 | $0.0093 | `2026-10-15-recepcion-low-v3.txt` |
| `locales.txt` | 18 | 17 | 2.26 s | 37 | $0.0066 | `2026-10-15-locales-low-v2.txt` |

Observado: el perfil interno ya no cita "Fuente:" en las respuestas que dicen
que el tema no esta en los documentos (solo cuando ademas aporta algo del
manual); el recado de la inundacion se guarda con `location=Caguas` y el
`summary` en espanol, y cuando el modelo lo intenta sin local el handler lo
rechaza y el agente pregunta el local antes de reintentar. El unico fallo de
`locales.txt` (03.2) fue "el proximo viernes" pedido un jueves: el agente
pregunto si era manana o en ocho dias y no reservo. No es regresion; el caso
quedo con fecha absoluta.
