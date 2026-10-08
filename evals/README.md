# Evals del agente

Casos en `dominio.txt` y `clinica.txt`; formato en
`landing/management/commands/agent_eval.py`. Correr:

    python manage.py agent_eval --client dominio --file evals/dominio.txt --effort low --reps 2 --out evals/results/<fecha>-dominio-low.txt

`results/` guarda las transcripciones de cada corrida (la ultima linea es el
resumen: casos, fallos, latencia, tokens y costo). Los archivos `*-prefecha`
son de antes de darle al agente la fecha de hoy (la cita de "el proximo
martes" fallaba en todos los niveles); con el arreglo, low y auto pasan 58/58.
Decision 2026-10-08: effort `low` (ver docs/costos/Analisis-costos-agente-IA.pdf, seccion 11).
