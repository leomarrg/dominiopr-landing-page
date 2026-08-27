"""M-19: run a client's evaluation set against the agent BEFORE activating
knowledge changes. Usage:

    python manage.py agent_eval --client dominio --file evals/dominio.txt

The eval file has one case per line:  pregunta | fragmento_esperado
(the answer must CONTAIN the expected fragment, case-insensitive). A line with
only a question checks the safe-response rule instead: the answer must not be
empty. Exit code 1 when any case fails — wire it into the deploy script.
"""
from django.core.management.base import BaseCommand, CommandError

from landing import agent
from landing.models import Client


class Command(BaseCommand):
    help = 'Run an evaluation question set against a client agent (M-19).'

    def add_arguments(self, parser):
        parser.add_argument('--client', required=True, help='Client slug.')
        parser.add_argument('--file', required=True, help='Eval file: pregunta | esperado')
        parser.add_argument('--include-draft', type=int, default=None,
                            help='Include one draft KnowledgeSource pk in the compile.')

    def handle(self, *args, **options):
        client = Client.objects.filter(slug=options['client']).first()
        if client is None:
            raise CommandError(f'No existe el cliente "{options["client"]}".')
        try:
            with open(options['file'], encoding='utf-8') as fh:
                lines = [l.strip() for l in fh if l.strip() and not l.startswith('#')]
        except OSError as e:
            raise CommandError(f'No se pudo leer el archivo: {e}')

        prompt = client.compiled_prompt(include_draft_pk=options['include_draft'])
        failures = 0
        for i, line in enumerate(lines, 1):
            question, _, expected = (p.strip() for p in line.partition('|'))
            try:
                reply, _usage = agent.answer(
                    [{'role': 'user', 'content': question}],
                    business_prompt=prompt, handlers={},
                    language=client.primary_language)
            except agent.AgentNotConfigured:
                raise CommandError('ANTHROPIC_API_KEY no está configurada.')
            ok = bool(reply) and (not expected or expected.lower() in reply.lower())
            mark = self.style.SUCCESS('PASS') if ok else self.style.ERROR('FAIL')
            self.stdout.write(f'[{i:02d}] {mark} {question[:60]}')
            if not ok:
                failures += 1
                self.stdout.write(f'     esperado: {expected[:80]}')
                self.stdout.write(f'     respuesta: {reply[:160]}')
        total = len(lines)
        if failures:
            raise CommandError(f'{failures}/{total} casos fallaron — NO publiques este cambio.')
        self.stdout.write(self.style.SUCCESS(f'{total}/{total} casos pasaron.'))
