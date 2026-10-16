"""M-19: run a client's evaluation set against the agent BEFORE activating
knowledge changes, or to compare models / effort levels / agent profiles. Usage:

    python manage.py agent_eval --client dominio --file evals/dominio.txt
    python manage.py agent_eval --client dominio --file evals/dominio.txt \
        --effort low --reps 2 --out eval-low.txt
    python manage.py agent_eval --fixture eval-recepcion --file evals/recepcion.txt \
        --profile receptionist --hours open --effort low --reps 2

One case per line:  turno1 >> turno2 | esperado | !prohibido | tool=capture_lead

- `>>` separates visitor turns of one conversation (the agent answers each).
- `esperado`: the LAST answer must CONTAIN it (case-insensitive). Optional.
- `!prohibido`: the answers must NOT contain it (prompt-injection cases).
- `tool=name`: that tool must be called at some point in the conversation;
  `notool=name`: it must NOT be called (e.g. an invalid email or phone).
- `maxlen=N`: the LAST answer has at most N characters.
- `onequestion`: the LAST answer asks at most one question (one '?').
- Per-case overrides of the run flags, for mixed files such as
  evals/inyeccion-perfiles.txt: `client=slug`, `profile=receptionist`,
  `channel=whatsapp`, `hours=open|closed|none`.
A line with only a question checks the safe-response rule: the answer must not
be empty. Tools are fake (nothing is saved, no email is sent). Exit code 1 when
any case fails — wire it into the deploy script.

Flags for the agent profiles (docs/agente/reglas-por-perfil.md):
- `--profile web|receptionist|internal` and `--channel web|whatsapp` are passed
  to agent.answer only when not the default, exactly like turns.run_turn does.
  Without --profile the client's own `agent_profile` is used when that field
  exists; otherwise 'web'.
- `--hours open|closed|none` adds a fixed "HOURS NOW" line (docs B8) to the
  UNCACHED system block of every model call of the run. When agent.answer
  accepts `context_lines` (Phase D) the line goes through it, exactly as the
  receptionist view sends it. Otherwise (older engine) it is injected at the
  provider seam (agent._provider_client) as a last system block, replacing
  any hours line the engine built, so the eval controls the open/closed
  state. The "hours:" part comes from the client's AvailabilityRule rows
  (default Mon-Fri 09:00-17:00 when there are none). With 2+ Location rows
  (Phase F) the line becomes the per-location block turns.hours_now_lines
  builds ("HOURS NOW (authoritative, per location):" + one "[Name] ..." line
  each, from that location's rules or the business-wide ones), every
  location in the SAME fixed state.
- `multi_location=True` is passed to agent.answer exactly when turns.run_turn
  would (2+ locations, profile not internal), so the LOCATIONS rules are
  under test; the fake handlers never reject a missing location, so a
  booking without one is caught only by the case's `notool=`.
"""
import inspect
import re
import time
from collections import Counter
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import patch

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.test.utils import override_settings

from landing import agent, usage as usage_ledger
from landing.models import Client, rules_for

# take_message is offered as a fake handler too: the engine only registers the
# tool once Phase D lands; until then the extra handler key is ignored.
TOOLS = ('capture_lead', 'create_booking', 'request_human', 'take_message')
HOURS_STATES = ('open', 'closed', 'none')
# Directive names as printed in the per-directive counts (passed/checked).
DIRECTIVES = ('vacia', 'esperado', 'prohibido', 'tool', 'notool', 'maxlen', 'onequestion')

_DEFAULT_SCHEDULE = 'Mon-Fri 09:00-17:00; Sat-Sun closed'
_DAY_ABBR = ('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun')
_DAY_NAME = ('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday')
# An hours line the engine may already add (docs B8): the single authoritative
# line or one "[Location] OPEN/CLOSED NOW" line per location.
_HOURS_LINE_RE = re.compile(r'^[ \t]*(HOURS NOW\b|\[[^\]\n]+\] (OPEN|CLOSED) NOW\b).*$', re.M)


def parse_case(line):
    """-> dict(turns, expected, forbidden, tool, notool, maxlen, onequestion,
    client, profile, channel, hours)."""
    parts = [p.strip() for p in line.split('|')]
    turns = [t.strip() for t in parts[0].split('>>') if t.strip()]
    case = {'turns': turns, 'expected': '', 'forbidden': [], 'tool': '', 'notool': '',
            'maxlen': None, 'onequestion': False,
            'client': '', 'profile': '', 'channel': '', 'hours': ''}
    for part in parts[1:]:
        if not part:
            continue
        if part.startswith('notool='):
            case['notool'] = part[7:].strip()
        elif part.startswith('tool='):
            case['tool'] = part[5:].strip()
        elif part.startswith('maxlen='):
            try:
                case['maxlen'] = int(part[7:].strip())
            except ValueError:
                raise ValueError(f'maxlen inválido en la línea: {line[:60]}')
        elif part == 'onequestion':
            case['onequestion'] = True
        elif part.startswith(('client=', 'profile=', 'channel=', 'hours=')):
            key, _, value = part.partition('=')
            case[key] = value.strip()
        elif part.startswith('!'):
            case['forbidden'].append(part[1:].strip())
        else:
            case['expected'] = part
    return case


def fake_handlers(called, inputs=None, client=None):
    """Recording stand-ins for the real tool handlers: no DB writes, no email.
    `inputs` (optional list) receives (name, tool_input) for the transcript.
    `client` (optional) lets the stand-ins mirror the one rule the real
    handlers answer with text instead of saving: with 2+ locations a booking or
    a message without a recognisable location is a soft rejection, so the eval
    sees the same second turn the visitor would."""
    locations = list(client.locations.all()) if client is not None else []

    def make(name):
        def handler(tool_input):
            called.append(name)
            if inputs is not None:
                inputs.append((name, dict(tool_input or {})))
            if name in ('create_booking', 'take_message') and len(locations) > 1:
                from landing.models import match_location
                if match_location(client, (tool_input or {}).get('location')) is None:
                    names = ', '.join(l.name for l in locations)
                    if name == 'create_booking':
                        return ('No se reservó: falta el local. Pregunta en cuál local quiere '
                                f'la cita: {names}.')
                    return ('No se guardó el recado: falta el local. Pregunta en cuál local '
                            f'ocurre: {names}.')
            return 'Listo: guardado correctamente.'
        return handler
    return {name: make(name) for name in TOOLS}


def _rules_for(client):
    """The business-wide rules (Phase F: per-location windows are not the
    fixture's schedule)."""
    manager = getattr(client, 'availability_rules', None)
    if manager is None:
        return []
    return list(manager.filter(location__isnull=True).order_by('weekday'))


def schedule_summary(client=None):
    """'Mon-Fri 09:00-17:00; Sat-Sun closed' from the client's AvailabilityRule
    rows (consecutive days with the same window are grouped)."""
    return _schedule_of(_rules_for(client))


def _locations(client):
    manager = getattr(client, 'locations', None)
    return list(manager.all()) if manager is not None else []


def _hours_state(state, rules):
    """'OPEN NOW, closes today 17:00' / 'CLOSED NOW, opens Monday 09:00' from
    the first rule (Monday 09:00-17:00 when there are none)."""
    first = rules[0] if rules else None
    opens = f'{first.open_time:%H:%M}' if first else '09:00'
    closes = f'{first.close_time:%H:%M}' if first else '17:00'
    opens_day = _DAY_NAME[first.weekday] if first else 'Monday'
    if state == 'open':
        return f'OPEN NOW, closes today {closes}'
    return f'CLOSED NOW, opens {opens_day} {opens}'


def _schedule_of(rules):
    saved = {r.weekday: f'{r.open_time:%H:%M}-{r.close_time:%H:%M}' for r in rules}
    if not saved:
        return _DEFAULT_SCHEDULE
    parts, start = [], 0
    for i in range(1, 8):
        if i == 7 or saved.get(i) != saved.get(start):
            span = _DAY_ABBR[start] if i - 1 == start else f'{_DAY_ABBR[start]}-{_DAY_ABBR[i - 1]}'
            parts.append(f'{span} {saved.get(start, "closed")}')
            start = i
    return '; '.join(parts)


def hours_line(state, client=None):
    """The fixed HOURS NOW line (docs B8) for `state`; '' for 'none'. The
    'closes today' / 'opens <day>' values come from the client's first
    AvailabilityRule (Monday 09:00-17:00 when there are none): the eval fixes
    the state, it never computes it from the clock. With 2+ locations
    (Phase F) the per-location block of turns.hours_now_lines, each location
    in the same `state` with its own rules (rules_for)."""
    if state not in ('open', 'closed'):
        return ''
    locations = _locations(client)
    if len(locations) < 2:
        rules = _rules_for(client)
        return (f'HOURS NOW (authoritative): {_hours_state(state, rules)} | '
                f'hours: {schedule_summary(client)} ({settings.TIME_ZONE})')
    lines = ['HOURS NOW (authoritative, per location):']
    for loc in locations:
        rules = sorted(rules_for(client, loc), key=lambda r: r.weekday)
        lines.append(f'[{loc.name}] {_hours_state(state, rules)} | '
                     f'hours: {_schedule_of(rules)} ({settings.TIME_ZONE})')
    return '\n'.join(lines)


def with_hours_block(create, line):
    """Wrap a provider `messages.create` so every call gets `line` as the LAST
    (uncached) system block. Hours lines the engine already put in `system`
    are removed first; a block left empty by that is dropped."""
    def wrapped(*args, **kwargs):
        system = kwargs.get('system') or []
        if isinstance(system, str):
            system = [{'type': 'text', 'text': system}]
        cleaned = []
        for block in system:
            text = block.get('text', '') if isinstance(block, dict) else ''
            if text and _HOURS_LINE_RE.search(text):
                text = _HOURS_LINE_RE.sub('', text).strip()
                if not text:
                    continue
                block = dict(block, text=text)
            cleaned.append(block)
        cleaned.append({'type': 'text', 'text': line})
        kwargs['system'] = cleaned
        return create(*args, **kwargs)
    return wrapped


def supports_context_lines():
    """True when agent.answer takes `context_lines` (Phase D): the HOURS NOW
    line is then passed like production does instead of patched in."""
    try:
        return 'context_lines' in inspect.signature(agent.answer).parameters
    except (TypeError, ValueError):
        return False


def hours_injection(line):
    """Fallback for an engine without `context_lines`. Context manager: while
    active, every provider client that agent._provider_client hands out
    appends `line` to the system blocks of each call. No-op for an empty
    line or when agent.answer takes the line itself. Lives here (not in
    agent.py) so the eval never changes the production prompt path."""
    if not line or supports_context_lines():
        return nullcontext()
    original = agent._provider_client

    def provider(api_key):
        return _ProviderProxy(original(api_key), line)
    return patch.object(agent, '_provider_client', provider)


class _ProviderProxy:
    """The provider client with `messages.create` wrapped by with_hours_block.
    A proxy, not an attribute swap, so the underlying client is never mutated."""

    def __init__(self, inner, line):
        self._inner = inner
        self.messages = SimpleNamespace(create=with_hours_block(inner.messages.create, line))

    def __getattr__(self, name):
        return getattr(self._inner, name)


class Command(BaseCommand):
    help = 'Run an evaluation question set against a client agent (M-19).'

    def add_arguments(self, parser):
        parser.add_argument('--client', '--fixture', dest='client', required=True,
                            help='Client slug (--fixture is an alias).')
        parser.add_argument('--file', required=True, help='Eval file (see module docstring).')
        parser.add_argument('--include-draft', type=int, default=None,
                            help='Include one draft KnowledgeSource pk in the compile.')
        parser.add_argument('--effort', default=None,
                            help='Override DOMINIO_AGENT_EFFORT: low | medium | high | auto.')
        parser.add_argument('--model', default=None, help='Override DOMINIO_AGENT_MODEL.')
        parser.add_argument('--reps', type=int, default=1, help='Run each case N times.')
        parser.add_argument('--out', default=None, help='Write full transcripts here.')
        parser.add_argument('--default-business', action='store_true',
                            help="Use the code's DOMINIO_BUSINESS instead of the DB prompt.")
        parser.add_argument('--profile', choices=agent.PROFILES, default=None,
                            help="Agent profile. Default: the client's agent_profile, else web.")
        parser.add_argument('--channel', choices=agent.CHANNELS, default='web',
                            help='Delivery channel passed to the agent when not web.')
        parser.add_argument('--hours', choices=HOURS_STATES, default='none',
                            help='Inject a fixed HOURS NOW line: open | closed | none.')

    def handle(self, *args, **options):
        client = Client.objects.filter(slug=options['client']).first()
        if client is None:
            raise CommandError(f'No existe el cliente "{options["client"]}".')
        try:
            with open(options['file'], encoding='utf-8') as fh:
                lines = [l.strip() for l in fh if l.strip() and not l.startswith('#')]
        except OSError as e:
            raise CommandError(f'No se pudo leer el archivo: {e}')

        overrides = {}
        if options['effort']:
            overrides['DOMINIO_AGENT_EFFORT'] = options['effort']
        if options['model']:
            overrides['DOMINIO_AGENT_MODEL'] = options['model']
        with override_settings(**overrides):
            self._run(client, lines, options)

    # -- per-case configuration -------------------------------------------

    def _client_for(self, slug, cache):
        if slug not in cache:
            found = Client.objects.filter(slug=slug).first()
            if found is None:
                raise CommandError(f'No existe el cliente "{slug}" (directiva client=).')
            cache[slug] = found
        return cache[slug]

    def _prompt_for(self, client, options, cache):
        if options['default_business']:
            return agent.DOMINIO_BUSINESS
        if client.slug not in cache:
            cache[client.slug] = client.compiled_prompt(include_draft_pk=options['include_draft'])
        return cache[client.slug]

    @staticmethod
    def _config_for(case, client, options):
        """(profile, channel, hours) for one case: directive > flag > client."""
        profile = case['profile'] or options['profile'] or getattr(client, 'agent_profile', '') or 'web'
        channel = case['channel'] or options['channel'] or 'web'
        hours = case['hours'] or options['hours'] or 'none'
        if profile not in agent.PROFILES:
            raise CommandError(f'profile inválido "{profile}" (válidos: {", ".join(agent.PROFILES)}).')
        if channel not in agent.CHANNELS:
            raise CommandError(f'channel inválido "{channel}" (válidos: {", ".join(agent.CHANNELS)}).')
        if hours not in HOURS_STATES:
            raise CommandError(f'hours inválido "{hours}" (válidos: {", ".join(HOURS_STATES)}).')
        return profile, channel, hours

    # -- run -----------------------------------------------------------------

    def _run(self, client, lines, options):
        clients, prompts = {client.slug: client}, {}
        reps = max(1, options['reps'])
        total = agent.new_usage()
        failures = runs = 0
        latencies, transcript = [], []
        efforts = {}
        checks, failed = Counter(), Counter()

        default_profile = options['profile'] or getattr(client, 'agent_profile', '') or 'web'
        header = (f'cliente={client.slug} perfil={default_profile} canal={options["channel"]} '
                  f'horario={options["hours"]}')
        self.stdout.write(header)
        transcript.append(f'# {header}')

        for i, line in enumerate(lines, 1):
            try:
                case = parse_case(line)
            except ValueError as e:
                raise CommandError(str(e))
            if not case['turns']:
                self.stdout.write(self.style.WARNING(f'[{i:02d}] línea sin pregunta, se salta: {line[:60]}'))
                continue
            case_client = self._client_for(case['client'], clients) if case['client'] else client
            prompt = self._prompt_for(case_client, options, prompts)
            profile, channel, hours = self._config_for(case, case_client, options)
            # Only non-default kwargs are passed (same contract as turns.run_turn).
            extra = dict(agent.clinic_kwargs(case_client))
            if profile != 'web':
                extra['profile'] = profile
            if channel != 'web':
                extra['channel'] = channel
            if profile != 'internal' and len(_locations(case_client)) > 1:
                # Phase F, same condition as turns.run_turn.
                extra['multi_location'] = True
            line_hours = hours_line(hours, case_client)
            if line_hours and supports_context_lines():
                extra['context_lines'] = (line_hours,)

            for rep in range(reps):
                runs += 1
                called, inputs, history, replies = [], [], [], []
                handlers = fake_handlers(called, inputs, client=case_client)
                with hours_injection(line_hours):
                    for turn in case['turns']:
                        history.append({'role': 'user', 'content': turn})
                        started = time.monotonic()
                        try:
                            reply, u = agent.answer(history, business_prompt=prompt,
                                                    handlers=handlers,
                                                    language=case_client.primary_language,
                                                    **extra)
                        except agent.AgentNotConfigured:
                            raise CommandError('ANTHROPIC_API_KEY no está configurada.')
                        latencies.append(time.monotonic() - started)
                        usage_ledger.record('eval', u, client=case_client)
                        for key in agent.USAGE_TOKEN_KEYS + ('api_calls',):
                            total[key] += u.get(key, 0) or 0
                        total['model'] = u.get('model') or total['model']
                        eff = u.get('effort') or '-'
                        efforts[eff] = efforts.get(eff, 0) + 1
                        history.append({'role': 'assistant', 'content': reply})
                        replies.append(reply)

                last = replies[-1] if replies else ''
                problems = []

                def check(name, ok, problem):
                    checks[name] += 1
                    if not ok:
                        failed[name] += 1
                        problems.append(problem)

                check('vacia', bool(last), 'respuesta vacía')
                if case['expected']:
                    check('esperado', case['expected'].lower() in last.lower(),
                          f'falta "{case["expected"]}"')
                joined = ' '.join(replies).lower()
                for bad in case['forbidden']:
                    check('prohibido', bad.lower() not in joined, f'contiene prohibido "{bad}"')
                if case['tool']:
                    check('tool', case['tool'] in called,
                          f'no llamó {case["tool"]} (llamó: {called or "ninguna"})')
                if case['notool']:
                    check('notool', case['notool'] not in called,
                          f'llamó {case["notool"]} y no debía')
                if case['maxlen'] is not None:
                    check('maxlen', len(last) <= case['maxlen'],
                          f'respuesta de {len(last)} caracteres (máx {case["maxlen"]})')
                if case['onequestion']:
                    check('onequestion', last.count('?') <= 1,
                          f'hace {last.count("?")} preguntas (máx 1)')

                ok = not problems
                failures += not ok
                mark = self.style.SUCCESS('PASS') if ok else self.style.ERROR('FAIL')
                tag = f'[{i:02d}{"." + str(rep + 1) if reps > 1 else ""}]'
                self.stdout.write(f'{tag} {mark} {case["turns"][-1][:60]}')
                if not ok:
                    self.stdout.write(f'     {"; ".join(problems)}')
                    self.stdout.write(f'     respuesta: {last[:160]}')
                transcript.append(f'{tag} {"PASS" if ok else "FAIL: " + "; ".join(problems)}')
                for turn, reply in zip(case['turns'], replies):
                    transcript.append(f'  > {turn}\n  < {reply}')
                transcript.append(f'  tools: {called} | client={case_client.slug} '
                                  f'profile={profile} channel={channel} hours={hours}')
                for name, tool_input in inputs:
                    transcript.append(f'  input {name}: {tool_input}')
                transcript.append('')

        cost = usage_ledger.cost_for(total.get('model', ''), total)
        avg = sum(latencies) / len(latencies) if latencies else 0
        p95 = sorted(latencies)[int(len(latencies) * 0.95) - 1] if latencies else 0
        summary = (
            f'modelo={total["model"]} effort={dict(sorted(efforts.items()))} '
            f'casos={runs} fallos={failures} | latencia prom={avg:.2f}s p95={p95:.2f}s | '
            f'tokens in={total["input_tokens"]} out={total["output_tokens"]} '
            f'cache_r={total["cache_read_input_tokens"]} cache_w={total["cache_creation_input_tokens"]} '
            f'llamadas={total["api_calls"]} | costo=${cost:.4f}')
        directives = 'directivas: ' + ' '.join(
            f'{name}={checks[name] - failed[name]}/{checks[name]}'
            for name in DIRECTIVES if checks[name])
        self.stdout.write(summary)
        self.stdout.write(directives)
        if options['out']:
            with open(options['out'], 'w', encoding='utf-8') as fh:
                fh.write('\n'.join(transcript) + '\n' + summary + '\n' + directives + '\n')
        if failures:
            raise CommandError(f'{failures}/{runs} casos fallaron — NO publiques este cambio.')
        self.stdout.write(self.style.SUCCESS(f'{runs}/{runs} casos pasaron.'))
