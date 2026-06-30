# DOMINIO — Flota de subagentes de Claude Code

Estos son los **13 subagentes personalizados** ("la flota") que usamos en este
proyecto. Viven en la configuración global de Claude Code (`~/.claude/agents/`),
no dentro del repo, así que para tenerlos en otra máquina (tu desktop) hay que
copiar estos `.md` a esa carpeta.

> EN — These are the 13 custom Claude Code subagents ("the fleet"). They live in
> the global Claude Code config (`~/.claude/agents/`), not inside the repo, so to
> use them on another machine you copy these `.md` files into that folder.

---

## Qué incluye / What's included

| Agente | Para qué sirve |
|---|---|
| `arquitecto-sistemas` | Diseño de arquitectura antes de features/refactors grandes (read-only). |
| `auditor-seguridad` | Auditoría de seguridad (auth, multi-tenancy, pagos, infra) antes de prod. |
| `cientifico-datos` | Auditar datasets, modelos, métricas, leakage/overfitting. |
| `completeness-auditor` | Auditoría final: comparar lo pedido vs lo entregado, gaps y bordes. |
| `disenador-ui` | Diseñar/revisar UI, responsive, estados, accesibilidad (puede editar UI). |
| `especialista-3d` | Experiencias 3D web (Three.js / R3F), budgets y fallback. |
| `especialista-animacion` | Motion 2D, microinteracciones, scroll-driven, reduced-motion. |
| `estratega-opciones` | Auditar lógica financiera de scanners de opciones (read-only). |
| `estratega-producto` | Validar usuario/problema/MVP/métricas antes de construir (read-only). |
| `guardian-performance` | Core Web Vitals, bundle, imágenes, hydration (read-only). |
| `ingeniero-backend` | Modelos, APIs, jobs, queries, migraciones (puede editar). |
| `qa-revisor` | QA funcional después de cada cambio, reproduce fallos, corre tests. |
| `redactor-copy` | Copy de landings, emails, CTAs, estados (puede editar texto). |

Cada archivo es markdown autocontenido (frontmatter `name/description/tools/model`
+ system prompt). No contienen rutas específicas de ninguna máquina, así que son
100% portables.

---

## Instalación en tu desktop

### Requisito previo
Tener **Claude Code** instalado y haber iniciado sesión al menos una vez (para que
exista la carpeta `~/.claude/`). Si nunca lo has abierto, ejecútalo una vez:

```bash
claude
```

### Windows (PowerShell)

Desde la raíz del repo ya clonado en tu desktop:

```powershell
# Crea la carpeta de agentes si no existe
New-Item -ItemType Directory -Force "$env:USERPROFILE\.claude\agents" | Out-Null

# Copia los 13 agentes
Copy-Item "setup\claude-agents\agents\*.md" "$env:USERPROFILE\.claude\agents\" -Force

# Verifica
Get-ChildItem "$env:USERPROFILE\.claude\agents\*.md" | Select-Object Name
```

### macOS / Linux (bash/zsh)

```bash
mkdir -p ~/.claude/agents
cp setup/claude-agents/agents/*.md ~/.claude/agents/
ls -1 ~/.claude/agents/*.md
```

### Verificar dentro de Claude Code

1. Abre Claude Code en el proyecto: `claude`
2. Corre `/agents` — deberías ver los 13 agentes en la lista.
3. O simplemente pídele algo a un agente: *"usa el agente disenador-ui para revisar el header"*.

> Si ya tienes agentes con el mismo nombre en tu desktop, `-Force` / `cp` los
> **sobrescribe** con esta versión. Si quieres conservarlos, haz un backup primero:
> `Copy-Item "$env:USERPROFILE\.claude\agents" "$env:USERPROFILE\.claude\agents.backup" -Recurse`

---

## Notas

- **Ámbito (scope):** copiados a `~/.claude/agents/` quedan **globales** (disponibles
  en todos tus proyectos). Si los prefieres solo para este proyecto, cópialos a
  `.claude/agents/` dentro del repo en su lugar.
- **Modelo:** varios agentes piden `model: opus` y `effort: high`. Funcionan igual
  en tu cuenta; el modelo se resuelve con tu plan de Claude Code.
- **No incluye:** skills (`.claude/skills/`, que sí viajan con el repo) ni plugins
  (`~/.claude/plugins/`). Estos agentes son solo la flota de subagentes.
- **Mantener sincronizado:** si editas un agente en una máquina, vuelve a copiar el
  `.md` actualizado aquí y haz commit para que la otra máquina lo reciba con `git pull`.
