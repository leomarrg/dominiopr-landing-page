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

Cada archivo documenta en su cabecera el comando exacto. Los 25 casos nuevos
salen de `docs/agente/reglas-por-perfil.md`, seccion C.

## Fixtures locales

    python manage.py seed_eval_clients

Crea o actualiza (idempotente; solo con `DEBUG=True`, `--force` para una BD
local sin DEBUG) los clientes `eval-recepcion` (salon, locales Bayamon y
Caguas, departamentos Ventas y Facturacion, L-V 9-5 como `AvailabilityRule`,
citas activas) y `eval-interno` (dos `KnowledgeSource` activas: "Manual del
Empleado" y "Politica de Vacaciones"). Si existe `eval-clinica` lo reutiliza.
Pone `agent_profile` cuando ese campo del modelo existe; antes de eso hay que
pasar `--profile` al correr.

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
  parte `hours:` sale de las `AvailabilityRule` del cliente.
- Directivas por caso que sobreescriben las banderas: `client=`, `profile=`,
  `channel=`, `hours=` (ver `inyeccion-perfiles.txt`).
- `--effort`, `--model`, `--reps`, `--out`, `--include-draft`,
  `--default-business` como antes.

La ultima linea del resumen no cambia (casos, fallos, latencia, tokens,
costo); debajo se imprime `directivas: esperado=7/8 prohibido=12/12 ...`
(pasadas/comprobadas por tipo de directiva).

## Corridas

    python manage.py agent_eval --client dominio --file evals/dominio.txt --effort low --reps 2 --out evals/results/<fecha>-dominio-low.txt
    python manage.py agent_eval --fixture eval-recepcion --file evals/recepcion.txt --profile receptionist --hours open --effort low --reps 2 --out evals/results/<fecha>-recepcion-low.txt
    python manage.py agent_eval --fixture eval-recepcion --file evals/recepcion-cerrado.txt --profile receptionist --hours closed --effort low --reps 2 --out evals/results/<fecha>-recepcion-cerrado-low.txt
    python manage.py agent_eval --fixture eval-interno --file evals/interno.txt --profile internal --effort low --reps 2 --out evals/results/<fecha>-interno-low.txt
    python manage.py agent_eval --fixture eval-recepcion --file evals/whatsapp.txt --profile receptionist --channel whatsapp --hours open --effort low --reps 2 --out evals/results/<fecha>-whatsapp-low.txt
    python manage.py agent_eval --fixture eval-recepcion --file evals/inyeccion-perfiles.txt --effort low --reps 2 --out evals/results/<fecha>-inyeccion-perfiles-low.txt

`results/` guarda las transcripciones de cada corrida (la ultima linea es el
resumen: casos, fallos, latencia, tokens y costo). Los archivos `*-prefecha`
son de antes de darle al agente la fecha de hoy (la cita de "el proximo
martes" fallaba en todos los niveles); con el arreglo, low y auto pasan 58/58.
Decision 2026-10-08: effort `low` (ver docs/costos/Analisis-costos-agente-IA.pdf, seccion 11).
Tras refactorizar `build_rules`, re-correr `dominio.txt` y `clinica.txt` para
confirmar 58/58 antes de publicar.
