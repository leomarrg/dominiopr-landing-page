"""
Eval harness for the agent profiles (docs/agente/reglas-por-perfil.md, section C):
the new agent_eval directives and flags (--profile, --channel, --hours,
--fixture, maxlen, onequestion, per-case overrides) and the seed_eval_clients
fixture command. The model provider and agent.answer are ALWAYS mocked: no
test ever calls a real API.
"""
import os
import tempfile
from datetime import time
from io import StringIO
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase, override_settings

from . import agent
from .management.commands import agent_eval
from .management.commands.agent_eval import (
    hours_line, parse_case, schedule_summary, with_hours_block)
from .management.commands.seed_eval_clients import (
    INTERNO_SLUG, INTERNO_SOURCES, RECEPCION_SLUG, _profile_choices)
from .models import AvailabilityRule, Client, KnowledgeSource


def _usage(**extra):
    u = agent.new_usage('claude-haiku-5-5')
    u['effort'] = 'low'
    u.update(extra)
    return u


def _resp(text='Hola', stop='end_turn'):
    content = [SimpleNamespace(type='text', text=text)] if text else []
    usage = SimpleNamespace(input_tokens=10, output_tokens=5,
                            cache_read_input_tokens=0, cache_creation_input_tokens=0)
    return SimpleNamespace(stop_reason=stop, content=content, usage=usage, stop_details=None)


def _eval_file(testcase, text):
    """A temporary eval file removed after the test."""
    fd, path = tempfile.mkstemp(suffix='.txt', prefix='eval-')
    with os.fdopen(fd, 'w', encoding='utf-8') as fh:
        fh.write(text)
    testcase.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
    return path


def _client(slug, **kw):
    values = dict(name=slug, system_prompt=f'Negocio {slug}', notify_email='x@example.com')
    values.update(kw)
    return Client.objects.create(slug=slug, **values)


# --- parsing -----------------------------------------------------------------

class ParseDirectiveTests(SimpleTestCase):
    def test_maxlen_and_onequestion(self):
        case = parse_case('hola | gracias | maxlen=600 | onequestion')
        self.assertEqual(case['expected'], 'gracias')
        self.assertEqual(case['maxlen'], 600)
        self.assertTrue(case['onequestion'])

    def test_defaults_when_absent(self):
        case = parse_case('hola | gracias')
        self.assertIsNone(case['maxlen'])
        self.assertFalse(case['onequestion'])
        self.assertEqual((case['client'], case['profile'], case['channel'], case['hours']),
                         ('', '', '', ''))

    def test_invalid_maxlen_raises(self):
        with self.assertRaises(ValueError):
            parse_case('hola | maxlen=muchos')

    def test_per_case_overrides(self):
        case = parse_case('x >> y | !** | client=eval-interno | profile=internal '
                          '| channel=whatsapp | hours=closed | notool=take_message')
        self.assertEqual(case['client'], 'eval-interno')
        self.assertEqual(case['profile'], 'internal')
        self.assertEqual(case['channel'], 'whatsapp')
        self.assertEqual(case['hours'], 'closed')
        self.assertEqual(case['forbidden'], ['**'])
        self.assertEqual(case['notool'], 'take_message')

    def test_old_directives_unchanged(self):
        case = parse_case('a >> b | esperado | !malo | tool=capture_lead | notool=create_booking')
        self.assertEqual(case['turns'], ['a', 'b'])
        self.assertEqual(case['expected'], 'esperado')
        self.assertEqual(case['forbidden'], ['malo'])
        self.assertEqual(case['tool'], 'capture_lead')
        self.assertEqual(case['notool'], 'create_booking')


# --- hours line --------------------------------------------------------------

class HoursLineTests(SimpleTestCase):
    def test_none_is_empty(self):
        self.assertEqual(hours_line('none'), '')
        self.assertEqual(hours_line(''), '')

    @override_settings(TIME_ZONE='America/Puerto_Rico')
    def test_open_and_closed_follow_docs_b8(self):
        open_line = hours_line('open')
        closed_line = hours_line('closed')
        self.assertTrue(open_line.startswith('HOURS NOW (authoritative): OPEN NOW, closes today 17:00 | hours: '))
        self.assertTrue(closed_line.startswith('HOURS NOW (authoritative): CLOSED NOW, opens Monday 09:00 | hours: '))
        for line in (open_line, closed_line):
            self.assertIn('Mon-Fri 09:00-17:00', line)
            self.assertTrue(line.endswith('(America/Puerto_Rico)'))

    def test_default_schedule_without_rules(self):
        self.assertEqual(schedule_summary(None), 'Mon-Fri 09:00-17:00; Sat-Sun closed')

    def test_wrapper_appends_uncached_block_last(self):
        create = MagicMock(return_value='r')
        wrapped = with_hours_block(create, 'HOURS NOW (authoritative): OPEN NOW')
        system = [{'type': 'text', 'text': 'rules', 'cache_control': {'type': 'ephemeral'}},
                  {'type': 'text', 'text': 'CURRENT DATE: today'}]
        self.assertEqual(wrapped(model='m', system=system, messages=[]), 'r')
        sent = create.call_args.kwargs['system']
        self.assertEqual(sent[0], system[0])
        self.assertEqual(sent[1], system[1])
        self.assertEqual(sent[-1], {'type': 'text', 'text': 'HOURS NOW (authoritative): OPEN NOW'})
        self.assertNotIn('cache_control', sent[-1])
        # The caller's list is not mutated.
        self.assertEqual(len(system), 2)

    def test_wrapper_replaces_engine_hours_lines(self):
        create = MagicMock()
        wrapped = with_hours_block(create, 'HOURS NOW (authoritative): CLOSED NOW, opens Monday 09:00')
        system = [
            {'type': 'text', 'text': 'rules', 'cache_control': {'type': 'ephemeral'}},
            {'type': 'text', 'text': 'CURRENT DATE: today\nHOURS NOW (authoritative): OPEN NOW, closes today 17:00'},
            {'type': 'text', 'text': '[Bayamón] OPEN NOW, closes today 17:00\n[Caguas] CLOSED NOW, opens tomorrow 09:00'},
        ]
        wrapped(system=system)
        sent = create.call_args.kwargs['system']
        texts = [b['text'] for b in sent]
        self.assertEqual(texts, ['rules', 'CURRENT DATE: today',
                                 'HOURS NOW (authoritative): CLOSED NOW, opens Monday 09:00'])

    def test_wrapper_accepts_string_system(self):
        create = MagicMock()
        with_hours_block(create, 'HOURS NOW (authoritative): OPEN NOW')(system='solo texto')
        sent = create.call_args.kwargs['system']
        self.assertEqual([b['text'] for b in sent], ['solo texto', 'HOURS NOW (authoritative): OPEN NOW'])


class HoursLineWithRulesTests(TestCase):
    def test_schedule_from_availability_rules(self):
        client = _client('horas')
        for weekday in range(5):
            AvailabilityRule.objects.create(client=client, weekday=weekday,
                                            open_time=time(8, 30), close_time=time(16, 0))
        AvailabilityRule.objects.create(client=client, weekday=5,
                                        open_time=time(10, 0), close_time=time(14, 0))
        self.assertEqual(schedule_summary(client), 'Mon-Fri 08:30-16:00; Sat 10:00-14:00; Sun closed')
        self.assertIn('OPEN NOW, closes today 16:00', hours_line('open', client))
        self.assertIn('CLOSED NOW, opens Monday 08:30', hours_line('closed', client))


# --- the command forwards flags -------------------------------------------------

class EvalCommandForwardingTests(TestCase):
    def setUp(self):
        self.client_row = _client('eval-x', primary_language='es')
        self.calls = []

        def fake_answer(history, **kwargs):
            self.calls.append(kwargs)
            return 'Claro, con gusto.', _usage()
        self.fake_answer = fake_answer

    def _run(self, text, *flags):
        path = _eval_file(self, text)
        out = StringIO()
        with patch.object(agent, 'answer', side_effect=self.fake_answer):
            call_command('agent_eval', '--fixture', 'eval-x', '--file', path, *flags, stdout=out)
        return out.getvalue()

    def test_defaults_keep_old_signature(self):
        out = self._run('hola | claro\n')
        self.assertEqual(len(self.calls), 1)
        kwargs = self.calls[0]
        self.assertNotIn('profile', kwargs)
        self.assertNotIn('channel', kwargs)
        self.assertNotIn('clinic', kwargs)
        self.assertEqual(kwargs['business_prompt'], 'Negocio eval-x')
        self.assertEqual(kwargs['language'], 'es')
        self.assertIn('1/1 casos pasaron', out)
        self.assertIn('directivas: vacia=1/1 esperado=1/1', out)

    def test_profile_and_channel_forwarded_only_when_non_default(self):
        self._run('hola\n', '--profile', 'receptionist', '--channel', 'whatsapp')
        self.assertEqual(self.calls[0]['profile'], 'receptionist')
        self.assertEqual(self.calls[0]['channel'], 'whatsapp')
        self.calls.clear()
        self._run('hola\n', '--profile', 'web', '--channel', 'web')
        self.assertNotIn('profile', self.calls[0])
        self.assertNotIn('channel', self.calls[0])

    def test_invalid_profile_is_rejected_by_argparse(self):
        path = _eval_file(self, 'hola\n')
        with self.assertRaises(CommandError):
            call_command('agent_eval', '--client', 'eval-x', '--file', path, '--profile', 'ceo')

    def test_client_agent_profile_used_when_field_exists(self):
        # Simulate Phase D's Client.agent_profile on the instance the command loads.
        path = _eval_file(self, 'hola\n')
        real_first = Client.objects.filter(slug='eval-x').first
        with patch.object(agent, 'answer', side_effect=self.fake_answer), \
                patch.object(Client.objects, 'filter') as filter_mock:
            row = real_first()
            row.agent_profile = 'internal'
            filter_mock.return_value.first.return_value = row
            call_command('agent_eval', '--client', 'eval-x', '--file', path, stdout=StringIO())
        self.assertEqual(self.calls[0]['profile'], 'internal')

    def test_per_case_overrides_win_over_flags(self):
        other = _client('eval-y', primary_language='en')
        self._run('uno | profile=internal | client=eval-y | channel=whatsapp\n'
                  'dos\n', '--profile', 'receptionist')
        first, second = self.calls
        self.assertEqual(first['profile'], 'internal')
        self.assertEqual(first['channel'], 'whatsapp')
        self.assertEqual(first['business_prompt'], 'Negocio eval-y')
        self.assertEqual(first['language'], 'en')
        self.assertEqual(second['profile'], 'receptionist')
        self.assertNotIn('channel', second)
        self.assertEqual(second['business_prompt'], 'Negocio eval-x')
        self.assertTrue(other.pk)

    def test_unknown_case_client_fails(self):
        path = _eval_file(self, 'uno | client=no-existe\n')
        with patch.object(agent, 'answer', side_effect=self.fake_answer), \
                self.assertRaisesMessage(CommandError, 'no-existe'):
            call_command('agent_eval', '--client', 'eval-x', '--file', path, stdout=StringIO())

    def test_maxlen_and_onequestion_fail_and_are_counted(self):
        def long_answer(history, **kwargs):
            return '¿Nombre? ¿Teléfono? ' + 'x' * 100, _usage()
        path = _eval_file(self, 'hola | maxlen=50\nhola | onequestion\nhola | maxlen=500\n')
        out = StringIO()
        with patch.object(agent, 'answer', side_effect=long_answer), \
                self.assertRaisesMessage(CommandError, '2/3 casos fallaron'):
            call_command('agent_eval', '--client', 'eval-x', '--file', path, stdout=out)
        text = out.getvalue()
        self.assertIn('caracteres (máx 50)', text)
        self.assertIn('hace 2 preguntas (máx 1)', text)
        self.assertIn('maxlen=1/2', text)
        self.assertIn('onequestion=0/1', text)

    def test_summary_line_format_unchanged(self):
        out = self._run('hola | claro\n')
        self.assertRegex(out, r'modelo=claude-haiku-5-5 effort=\{.*\} casos=1 fallos=0 \| latencia prom=')


class EvalCommandHoursInjectionTests(TestCase):
    """--hours goes through the real agent.answer with a fake provider client,
    so the tests prove the HOURS NOW line reaches messages.create in the
    UNCACHED part of `system`, on both paths: `context_lines` (Phase D
    engine) and the provider-seam fallback (older engine)."""

    def setUp(self):
        self.client_row = _client('eval-h')
        for weekday in range(5):
            AvailabilityRule.objects.create(client=self.client_row, weekday=weekday,
                                            open_time=time(9, 0), close_time=time(17, 0))

    def _run_with_provider(self, text, *flags, fallback=False):
        provider = MagicMock()
        provider.messages.create.return_value = _resp('Sí, estamos abiertos.')
        path = _eval_file(self, text)
        supports = (lambda: False) if fallback else agent_eval.supports_context_lines
        with override_settings(ANTHROPIC_API_KEY='k', DOMINIO_AGENT_MODEL='claude-haiku-5-5'), \
                patch.object(agent, '_provider_client', return_value=provider), \
                patch.object(agent_eval, 'supports_context_lines', supports):
            call_command('agent_eval', '--client', 'eval-h', '--file', path, *flags, stdout=StringIO())
            # The eval's own patch (fallback path) is undone once the command returns.
            self.assertIs(agent._provider_client(''), provider)
        return provider.messages.create.call_args_list

    def _assert_hours_uncached(self, system, expected_line):
        self.assertIn('cache_control', system[0])
        self.assertNotIn('HOURS NOW', system[0]['text'].split('BUSINESS')[-1])
        last = system[-1]
        self.assertIn(expected_line, last['text'])
        self.assertNotIn('cache_control', last)
        # Exactly one hours line, in one uncached block.
        joined = '\n'.join(b['text'] for b in system[1:])
        self.assertEqual(joined.count('HOURS NOW (authoritative)'), 1)

    def test_hours_open_reaches_the_uncached_block(self):
        calls = self._run_with_provider('¿Están abiertos? | abiert\n', '--hours', 'open')
        self.assertEqual(len(calls), 1)
        line = hours_line('open', self.client_row)
        self.assertTrue(line.startswith('HOURS NOW (authoritative): OPEN NOW, closes today 17:00'))
        self._assert_hours_uncached(calls[0].kwargs['system'], line)

    def test_fallback_path_without_context_lines(self):
        calls = self._run_with_provider('¿Están abiertos? | abiert\n', '--hours', 'open', fallback=True)
        system = calls[0].kwargs['system']
        self._assert_hours_uncached(system, hours_line('open', self.client_row))
        # On this path the line is its own last block.
        self.assertEqual(system[-1]['text'], hours_line('open', self.client_row))

    def test_hours_closed_and_none(self):
        calls = self._run_with_provider('¿Abiertos?\n', '--hours', 'closed')
        self.assertIn('CLOSED NOW, opens Monday 09:00', calls[0].kwargs['system'][-1]['text'])
        calls = self._run_with_provider('¿Abiertos?\n')
        self.assertFalse(any('HOURS NOW (authoritative)' in b['text']
                             for b in calls[0].kwargs['system']))

    def test_per_case_hours_directive(self):
        calls = self._run_with_provider('uno | hours=closed\ndos\n', '--hours', 'open')
        self.assertIn('CLOSED NOW', calls[0].kwargs['system'][-1]['text'])
        self.assertIn('OPEN NOW', calls[1].kwargs['system'][-1]['text'])

    def test_hours_combined_with_profile_builds_profile_rules(self):
        calls = self._run_with_provider('uno\n', '--hours', 'open', '--profile', 'receptionist')
        system = calls[0].kwargs['system']
        self.assertIn('"HOURS NOW" line', system[0]['text'])  # the RECEPTION rules refer to it
        self.assertIn('HOURS NOW (authoritative): OPEN NOW', system[-1]['text'])

    def test_context_lines_kwarg_when_supported(self):
        calls = []

        def fake_answer(history, **kwargs):
            calls.append(kwargs)
            return 'ok', _usage()
        path = _eval_file(self, 'uno\ndos | hours=none\n')
        with patch.object(agent, 'answer', side_effect=fake_answer), \
                patch.object(agent_eval, 'supports_context_lines', lambda: True):
            call_command('agent_eval', '--client', 'eval-h', '--file', path,
                         '--hours', 'closed', stdout=StringIO())
        self.assertEqual(calls[0]['context_lines'], (hours_line('closed', self.client_row),))
        self.assertNotIn('context_lines', calls[1])
        calls.clear()
        with patch.object(agent, 'answer', side_effect=fake_answer), \
                patch.object(agent_eval, 'supports_context_lines', lambda: False):
            call_command('agent_eval', '--client', 'eval-h', '--file', path,
                         '--hours', 'closed', stdout=StringIO())
        self.assertNotIn('context_lines', calls[0])

    def test_supports_context_lines_reads_the_real_signature(self):
        import inspect
        expected = 'context_lines' in inspect.signature(agent.answer).parameters
        self.assertEqual(agent_eval.supports_context_lines(), expected)

    def test_hours_none_is_a_noop_context(self):
        from contextlib import nullcontext
        self.assertIsInstance(agent_eval.hours_injection(''), nullcontext)


# --- seed command ---------------------------------------------------------------

class SeedEvalClientsTests(TestCase):
    def _run(self, *flags):
        out = StringIO()
        call_command('seed_eval_clients', *flags, stdout=out)
        return out.getvalue()

    @override_settings(DEBUG=False)
    def test_refuses_without_debug(self):
        with self.assertRaisesMessage(CommandError, 'DEBUG'):
            call_command('seed_eval_clients', stdout=StringIO())
        self.assertFalse(Client.objects.filter(slug__in=[RECEPCION_SLUG, INTERNO_SLUG]).exists())

    @override_settings(DEBUG=False)
    def test_force_overrides_debug_guard(self):
        self._run('--force')
        self.assertTrue(Client.objects.filter(slug=RECEPCION_SLUG).exists())

    @override_settings(DEBUG=True)
    def test_creates_fixtures_and_prints_slugs(self):
        out = self._run()
        self.assertIn(RECEPCION_SLUG, out)
        self.assertIn(INTERNO_SLUG, out)
        self.assertIn('eval-clinica', out)

        recepcion = Client.objects.get(slug=RECEPCION_SLUG)
        self.assertTrue(recepcion.enable_bookings)
        self.assertFalse(recepcion.clinic_mode)
        self.assertEqual(recepcion.allowed_origins, '')
        for word in ('Bayamón', 'Caguas', 'Ventas', 'Facturación', 'keratina'):
            self.assertIn(word, recepcion.system_prompt)
        self.assertNotRegex(recepcion.system_prompt, r'keratina[^\n]*\$')
        rules = list(recepcion.availability_rules.order_by('weekday'))
        self.assertEqual([r.weekday for r in rules], [0, 1, 2, 3, 4])
        self.assertEqual({(r.open_time, r.close_time) for r in rules}, {(time(9, 0), time(17, 0))})
        self.assertEqual(schedule_summary(recepcion), 'Mon-Fri 09:00-17:00; Sat-Sun closed')
        if 'receptionist' in _profile_choices():
            self.assertEqual(recepcion.agent_profile, 'receptionist')

        interno = Client.objects.get(slug=INTERNO_SLUG)
        self.assertFalse(interno.enable_bookings)
        sources = list(interno.knowledge_sources.order_by('position'))
        self.assertEqual([s.title for s in sources], [t for t, _ in INTERNO_SOURCES])
        self.assertEqual({s.status for s in sources}, {'active'})
        compiled = interno.compiled_prompt()
        self.assertIn('=== Manual del Empleado ===', compiled)
        self.assertIn('=== Política de Vacaciones ===', compiled)
        self.assertIn('15 días', compiled)
        self.assertIn('Formulario de Reembolso de Millaje', compiled)
        for absent in ('remoto', 'wifi', 'maternidad', 'los viernes se'):
            self.assertNotIn(absent, compiled.lower())
        if 'internal' in _profile_choices():
            self.assertEqual(interno.agent_profile, 'internal')
        elif 'agent_profile' in {f.name for f in Client._meta.get_fields()}:
            # Not a model choice yet: the seed leaves the default and warns.
            self.assertEqual(interno.agent_profile, 'web')
            self.assertIn('--profile', out)

    @override_settings(DEBUG=True)
    def test_is_idempotent_and_repairs_drift(self):
        self._run()
        recepcion = Client.objects.get(slug=RECEPCION_SLUG)
        interno = Client.objects.get(slug=INTERNO_SLUG)
        # Drift: a Saturday rule, a de-activated document and a duplicate title.
        AvailabilityRule.objects.create(client=recepcion, weekday=5,
                                        open_time=time(10, 0), close_time=time(14, 0))
        KnowledgeSource.objects.filter(client=interno).update(status='draft')
        KnowledgeSource.objects.create(client=interno, title='Manual del Empleado',
                                       kind='text', content='viejo', status='active', position=9)
        before = Client.objects.count()
        self._run()
        self.assertEqual(Client.objects.count(), before)
        self.assertEqual(list(recepcion.availability_rules.values_list('weekday', flat=True)),
                         [0, 1, 2, 3, 4])
        active = interno.knowledge_sources.filter(status='active').order_by('position')
        self.assertEqual([s.title for s in active], [t for t, _ in INTERNO_SOURCES])
        self.assertEqual(interno.knowledge_sources.filter(status='archived').count(), 1)
        self.assertNotIn('viejo', interno.compiled_prompt())

    @override_settings(DEBUG=True)
    def test_reuses_existing_clinica(self):
        _client('eval-clinica', clinic_mode=True)
        out = self._run()
        self.assertIn('eval-clinica: existente', out)
        self.assertEqual(Client.objects.filter(slug='eval-clinica').count(), 1)
