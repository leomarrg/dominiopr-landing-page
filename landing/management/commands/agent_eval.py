"""M-19: run a client's evaluation set against the agent BEFORE activating
knowledge changes, or to compare models / effort levels. Usage:

    python manage.py agent_eval --client dominio --file evals/dominio.txt
    python manage.py agent_eval --client dominio --file evals/dominio.txt \
        --effort low --reps 2 --out eval-low.txt

One case per line:  turno1 >> turno2 | esperado | !prohibido | tool=capture_lead

- `>>` separates visitor turns of one conversation (the agent answers each).
- `esperado`: the LAST answer must CONTAIN it (case-insensitive). Optional.
- `!prohibido`: the answers must NOT contain it (prompt-injection cases).
- `tool=name`: that tool must be called at some point in the conversation;
  `notool=name`: it must NOT be called (e.g. an invalid email or phone).
A line with only a question checks the safe-response rule: the answer must not
be empty. Tools are fake (nothing is saved, no email is sent). Exit code 1 when
any case fails — wire it into the deploy script.
"""
import time

from django.core.management.base import BaseCommand, CommandError
from django.test.utils import override_settings

from landing import agent, usage as usage_ledger
from landing.models import Client

TOOLS = ('capture_lead', 'create_booking', 'request_human')


def parse_case(line):
    """-> dict(turns, expected, forbidden, tool)."""
    parts = [p.strip() for p in line.split('|')]
    turns = [t.strip() for t in parts[0].split('>>') if t.strip()]
    case = {'turns': turns, 'expected': '', 'forbidden': [], 'tool': '', 'notool': ''}
    for part in parts[1:]:
        if not part:
            continue
        if part.startswith('notool='):
            case['notool'] = part[7:].strip()
        elif part.startswith('tool='):
            case['tool'] = part[5:].strip()
        elif part.startswith('!'):
            case['forbidden'].append(part[1:].strip())
        else:
            case['expected'] = part
    return case


def fake_handlers(called):
    """Recording stand-ins for the real tool handlers: no DB writes, no email."""
    def make(name):
        def handler(tool_input):
            called.append(name)
            return 'Listo: guardado correctamente.'
        return handler
    return {name: make(name) for name in TOOLS}


class Command(BaseCommand):
    help = 'Run an evaluation question set against a client agent (M-19).'

    def add_arguments(self, parser):
        parser.add_argument('--client', required=True, help='Client slug.')
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

    def _run(self, client, lines, options):
        if options['default_business']:
            prompt = agent.DOMINIO_BUSINESS
        else:
            prompt = client.compiled_prompt(include_draft_pk=options['include_draft'])
        reps = max(1, options['reps'])
        total = agent.new_usage()
        failures = runs = 0
        latencies, transcript = [], []
        efforts = {}

        for i, line in enumerate(lines, 1):
            case = parse_case(line)
            if not case['turns']:
                self.stdout.write(self.style.WARNING(f'[{i:02d}] línea sin pregunta, se salta: {line[:60]}'))
                continue
            for rep in range(reps):
                runs += 1
                called, history, replies = [], [], []
                handlers = fake_handlers(called)
                for turn in case['turns']:
                    history.append({'role': 'user', 'content': turn})
                    started = time.monotonic()
                    try:
                        reply, u = agent.answer(history, business_prompt=prompt,
                                                handlers=handlers,
                                                language=client.primary_language,
                                                **agent.clinic_kwargs(client))
                    except agent.AgentNotConfigured:
                        raise CommandError('ANTHROPIC_API_KEY no está configurada.')
                    latencies.append(time.monotonic() - started)
                    usage_ledger.record('eval', u, client=client)
                    for key in agent.USAGE_TOKEN_KEYS + ('api_calls',):
                        total[key] += u.get(key, 0) or 0
                    total['model'] = u.get('model') or total['model']
                    eff = u.get('effort') or '-'
                    efforts[eff] = efforts.get(eff, 0) + 1
                    history.append({'role': 'assistant', 'content': reply})
                    replies.append(reply)

                last = replies[-1] if replies else ''
                problems = []
                if not last:
                    problems.append('respuesta vacía')
                if case['expected'] and case['expected'].lower() not in last.lower():
                    problems.append(f'falta "{case["expected"]}"')
                joined = ' '.join(replies).lower()
                for bad in case['forbidden']:
                    if bad.lower() in joined:
                        problems.append(f'contiene prohibido "{bad}"')
                if case['tool'] and case['tool'] not in called:
                    problems.append(f'no llamó {case["tool"]} (llamó: {called or "ninguna"})')
                if case['notool'] and case['notool'] in called:
                    problems.append(f'llamó {case["notool"]} y no debía')

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
                transcript.append(f'  tools: {called}\n')

        cost = usage_ledger.cost_for(total.get('model', ''), total)
        avg = sum(latencies) / len(latencies) if latencies else 0
        p95 = sorted(latencies)[int(len(latencies) * 0.95) - 1] if latencies else 0
        summary = (
            f'modelo={total["model"]} effort={dict(sorted(efforts.items()))} '
            f'casos={runs} fallos={failures} | latencia prom={avg:.2f}s p95={p95:.2f}s | '
            f'tokens in={total["input_tokens"]} out={total["output_tokens"]} '
            f'cache_r={total["cache_read_input_tokens"]} cache_w={total["cache_creation_input_tokens"]} '
            f'llamadas={total["api_calls"]} | costo=${cost:.4f}')
        self.stdout.write(summary)
        if options['out']:
            with open(options['out'], 'w', encoding='utf-8') as fh:
                fh.write('\n'.join(transcript) + '\n' + summary + '\n')
        if failures:
            raise CommandError(f'{failures}/{runs} casos fallaron — NO publiques este cambio.')
        self.stdout.write(self.style.SUCCESS(f'{runs}/{runs} casos pasaron.'))
