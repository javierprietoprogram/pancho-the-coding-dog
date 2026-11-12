"""Hook Claude de Pancho como proceso real: stdin (evento) → stdout (protocolo).

La app se simula escribiendo sus archivos del contrato (heartbeat, response, release)
en un almacén temporal. Ninguna prueba toca el HOME ni la configuración reales.
"""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shlex
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / 'scripts' / 'pancho_hook.py'
EXACT = '  Dos  espacios,\nsalto de línea, tildes áéíóú ñ, emoji 🙂 y espacio final  '
ORPHAN = 'Claude dejó de esperar; no se entregó'
sys.path.insert(0, str(ROOT / 'src'))
from pancho_bridge import claude as claude_adapter  # noqa: E402


def key_for(session, call):
    return hashlib.sha256('|'.join(['claude', 'claude-code', session, call]).encode('utf-8')).hexdigest()


def create_once(path, value):
    tmp = path.with_name('.tmp-' + os.urandom(4).hex())
    tmp.write_text(json.dumps(value, ensure_ascii=False))
    try:
        os.link(tmp, path)
    finally:
        tmp.unlink()


class PanchoHookTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.home = self.tmp / 'Pancho'
        self.env = dict(os.environ, PANCHO_HOME=str(self.home), HOME=str(self.tmp / 'home'))
        self.env.pop('PANCHO_CLAUDE_WAIT_SECONDS', None)

    # --- utilidades ---------------------------------------------------------------
    def command(self, owner='pancho-v1'):
        return [sys.executable, '-I', str(HOOK), 'hook', '--provider', 'claude', '--owner', owner]

    def run_hook(self, payload, raw=None, timeout=15, **env):
        data = raw if raw is not None else json.dumps(payload, ensure_ascii=False).encode('utf-8')
        return subprocess.run(self.command(), input=data, capture_output=True, timeout=timeout,
                              env=dict(self.env, **env))

    def start_hook(self, payload, stdout=subprocess.PIPE, **env):
        process = subprocess.Popen(self.command(), stdin=subprocess.PIPE, stdout=stdout,
                                   stderr=subprocess.PIPE, env=dict(self.env, **env))
        process.stdin.write(json.dumps(payload, ensure_ascii=False).encode('utf-8'))
        process.stdin.close()
        def cleanup():
            if process.poll() is None:
                process.kill()
                process.wait()
            for stream in (process.stdout, process.stderr):
                if stream is not None and not stream.closed:
                    stream.close()
        self.addCleanup(cleanup)
        return process

    def finish(self, process, timeout=15):
        stdout = process.stdout.read()
        process.wait(timeout=timeout)
        process.stdout.close()
        process.stderr.close()
        return process.returncode, stdout

    def heartbeat(self, pid=None, age=0.0):
        directory = self.home / 'v1' / 'app'
        directory.mkdir(parents=True, exist_ok=True)
        (directory / 'heartbeat.json').write_text(json.dumps(
            dict(pid=pid or os.getpid(), ts=time.time() - age, version='1')))

    def event(self, name, session='sesion-A', call='toolu_1', cwd='/Users/x/proyecto-uno', **extra):
        payload = dict(hook_event_name=name, session_id=session, cwd=cwd,
                       transcript_path='/Users/x/.claude/projects/SECRETO-TRANSCRIPT.jsonl')
        if name in ('PreToolUse', 'PostToolUse', 'PostToolUseFailure'):
            payload.update(tool_name='AskUserQuestion', tool_use_id=call)
        payload.update(extra)
        return payload

    def questions(self):
        return [
            dict(question='¿Qué frase exacta?' + EXACT, header='Frase', multiSelect=False,
                 options=[dict(label='A  doble', description='Descripción íntegra ' + 'larga ' * 300 + 'FIN'),
                          dict(label='B', description='Otra 🙂\nlínea')]),
            dict(question='¿Cuáles?', header='Varias', multiSelect=True,
                 options=[dict(label='Uno', description='1'), dict(label='Dos', description='2')]),
        ]

    def ask(self, session='sesion-A', call='toolu_1', cwd='/Users/x/proyecto-uno', questions=None):
        return self.event('PreToolUse', session, call, cwd, tool_input=dict(questions=questions or self.questions()))

    def req(self, session='sesion-A', call='toolu_1'):
        return self.home / 'v1' / 'requests' / key_for(session, call)

    def state(self, session='sesion-A', call='toolu_1'):
        return json.loads((self.req(session, call) / 'state.json').read_text())

    def wait_state(self, session='sesion-A', call='toolu_1', status=None, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                current = self.state(session, call)
                if status is None or current['status'] == status:
                    return current
            except (OSError, ValueError):
                pass
            time.sleep(0.03)
        self.fail('Sin estado %s para %s/%s' % (status, session, call))

    def respond(self, state, answers, **override):
        value = dict(key=state['key'], provider='claude', session_id=state['session_id'],
                     call_id=state['call_id'], generation=state['generation'], answers=answers,
                     created_at=time.time())
        value.update(override)
        create_once(self.req(state['session_id'], state['call_id']) / 'response.json', value)

    def answers(self, first=EXACT, second='Uno, Dos'):
        q = self.questions()
        return {q[0]['question']: first, q[1]['question']: second}

    # --- pruebas ----------------------------------------------------------------------
    # --- recuadro «siguiente paso» --------------------------------------------------------
    def wrap_up(self, session='sesion-A', call='toolu_w', **tool_input):
        value = dict(recap='SECRETO-RECAP', ask='¿Instalo ya?',
                     actions=[dict(label='Sí, instala'), dict(label='Ahora no'), dict(action='commit', label='x')])
        value.update(tool_input)
        return self.event('PreToolUse', session, call, tool_name='mcp__ccd_turn__wrap_up', tool_input=value)

    def note(self, session='sesion-A'):
        return self.home / 'v1' / 'next-step' / (hashlib.sha256(session.encode()).hexdigest()[:32] + '.json')

    def next_state(self, status=None, call='next-toolu_w'):
        return self.wait_state(call=call, status=status)

    def test_next_step_is_noted_without_output_and_answered_at_stop_blocks(self):
        self.heartbeat()
        run = self.run_hook(self.wrap_up())
        self.assertEqual((run.returncode, run.stdout), (0, b''))
        note = json.loads(self.note().read_text())
        self.assertEqual((note['ask'], note['labels']), ('¿Instalo ya?', ['Sí, instala', 'Ahora no']))
        self.assertNotIn('SECRETO-RECAP', self.note().read_text())
        self.assertFalse((self.home / 'v1' / 'requests').exists())
        process = self.start_hook(self.event('Stop'))
        state = self.next_state('pending')
        self.assertFalse(self.note().exists())
        self.assertEqual(state['kind'], 'next_step')
        self.assertEqual(state['questions'], [dict(
            id='next_step', header='Siguiente paso', question='¿Instalo ya?', multiSelect=False,
            options=[dict(label='Sí, instala', description=''), dict(label='Ahora no', description='')])])
        self.respond(state, {'next_step': 'Sí, instala'})
        code, stdout = self.finish(process)
        output = json.loads(stdout.decode('utf-8'))
        self.assertEqual((code, output['decision']), (0, 'block'))
        self.assertIn('«¿Instalo ya?»', output['reason'])
        self.assertIn('«Sí, instala»', output['reason'])
        final = self.next_state()
        self.assertEqual((final['status'], final['sent'], final['accepted']), ('accepted', True, True))
        session = json.loads(next((self.home / 'v1' / 'sessions').glob('*.json')).read_text())
        self.assertEqual(session['activity'], 'working')
        # El siguiente final de turno, sin recuadro nuevo, no espera ni bloquea.
        self.assertEqual(self.run_hook(self.event('Stop')).stdout, b'')
        self.assertEqual(self.next_state()['status'], 'continued')

    def test_next_step_free_text_answer_is_sent_verbatim(self):
        self.heartbeat()
        self.run_hook(self.wrap_up())
        process = self.start_hook(self.event('Stop'))
        self.respond(self.next_state('pending'), {'next_step': EXACT})
        self.assertIn('«' + EXACT + '»', json.loads(self.finish(process)[1].decode('utf-8'))['reason'])

    def test_next_step_release_lets_the_turn_end_without_output(self):
        self.heartbeat()
        self.run_hook(self.wrap_up())
        process = self.start_hook(self.event('Stop'))
        state = self.next_state('pending')
        create_once(self.req(call='next-toolu_w') / 'release.json', dict(key=state['key'], created_at=time.time()))
        self.assertEqual(self.finish(process), (0, b''))
        self.assertEqual(self.next_state()['status'], 'released')
        session = json.loads(next((self.home / 'v1' / 'sessions').glob('*.json')).read_text())
        self.assertNotEqual(session['activity'], 'working')

    def test_next_step_is_ignored_without_question_buttons_or_app(self):
        for tool_input in (dict(ask=None), dict(actions=[]), dict(actions=[dict(action='commit')]), dict(ask='  ')):
            self.heartbeat()
            self.run_hook(self.wrap_up())           # Deja nota…
            self.run_hook(self.wrap_up(**tool_input))  # …y un recuadro sin pregunta la borra.
            self.assertFalse(self.note().exists(), tool_input)
            self.assertEqual(self.run_hook(self.event('Stop')).stdout, b'')
        (self.home / 'v1' / 'app' / 'heartbeat.json').unlink()
        self.run_hook(self.wrap_up())
        self.assertFalse(self.note().exists())
        self.assertFalse((self.home / 'v1' / 'requests').exists())

    def test_next_step_note_dies_with_a_new_prompt_a_stale_age_or_a_closed_app(self):
        self.heartbeat()
        self.run_hook(self.wrap_up())
        self.run_hook(self.event('UserPromptSubmit', prompt='SECRETO'))
        self.assertFalse(self.note().exists())
        self.run_hook(self.wrap_up())
        note = json.loads(self.note().read_text())
        note['at'] -= 3600
        self.note().write_text(json.dumps(note))
        self.assertEqual(self.run_hook(self.event('Stop')).stdout, b'')
        self.assertFalse(self.note().exists())
        self.run_hook(self.wrap_up())
        (self.home / 'v1' / 'app' / 'heartbeat.json').unlink()
        self.assertEqual(self.run_hook(self.event('Stop')).stdout, b'')
        self.assertFalse((self.home / 'v1' / 'requests').exists())

    def test_next_step_other_session_stop_does_not_take_the_note(self):
        self.heartbeat()
        self.run_hook(self.wrap_up())
        self.assertEqual(self.run_hook(self.event('Stop', session='otra')).stdout, b'')
        self.assertTrue(self.note().exists())

    def test_next_step_pending_when_user_writes_in_claude_is_resolved_elsewhere(self):
        self.heartbeat()
        self.run_hook(self.wrap_up())
        process = self.start_hook(self.event('Stop'))
        self.next_state('pending')
        process.kill()
        process.wait()
        self.run_hook(self.event('UserPromptSubmit', prompt='x'))
        self.assertEqual(self.next_state()['status'], 'resolved_elsewhere')

    def test_app_closed_returns_control_immediately_without_output(self):
        started = time.monotonic()
        result = self.run_hook(self.ask())
        self.assertEqual((result.returncode, result.stdout), (0, b''))
        self.assertLess(time.monotonic() - started, 3)
        state = self.state()
        self.assertEqual(state['status'], 'released')
        self.assertEqual(state['message'], 'Pancho estaba cerrado; contesta en Claude')
        self.assertIsNone(state['expires_at'])
        self.assertFalse(state['sent'])

    def test_stale_heartbeat_or_dead_pid_counts_as_closed(self):
        self.heartbeat(age=7)
        self.assertEqual(self.run_hook(self.ask(call='c1')).stdout, b'')
        self.assertEqual(self.state(call='c1')['status'], 'released')
        dead = subprocess.Popen([sys.executable, '-c', 'pass'])
        dead.wait()
        self.heartbeat(pid=dead.pid)
        self.assertEqual(self.run_hook(self.ask(call='c2')).stdout, b'')
        self.assertEqual(self.state(call='c2')['status'], 'released')

    def test_exact_answer_is_returned_then_accepted_then_continued(self):
        self.heartbeat()
        payload = self.ask()
        process = self.start_hook(payload)
        state = self.wait_state(status='pending')
        self.assertEqual(state['questions'], [
            dict(id=q['question'], header=q['header'], question=q['question'], multiSelect=q['multiSelect'],
                 options=q['options']) for q in self.questions()])
        self.assertEqual((state['provider'], state['host'], state['cwd'], state['project']),
                         ('claude', 'claude-code', '/Users/x/proyecto-uno', 'proyecto-uno'))
        self.assertEqual(state['key'], key_for('sesion-A', 'toolu_1'))
        self.assertGreater(state['expires_at'], time.time() + 1700)
        draft = self.req() / 'draft.json'
        draft.write_text(json.dumps(dict(selections={}, other={}, updated_at=1)))
        self.respond(state, self.answers())
        code, stdout = self.finish(process)
        self.assertEqual(code, 0)
        output = json.loads(stdout.decode('utf-8'))
        self.assertEqual(output, dict(hookSpecificOutput=dict(
            hookEventName='PreToolUse', permissionDecision='allow',
            updatedInput=dict(payload['tool_input'], answers=self.answers()))))
        sent = self.state()
        self.assertEqual((sent['status'], sent['sent'], sent['accepted']), ('sent', True, False))
        self.assertEqual(sent['answers'], self.answers())
        self.assertTrue(draft.exists())
        self.assertEqual(self.run_hook(self.event('PostToolUse', tool_input=output['hookSpecificOutput']['updatedInput'],
                                                  tool_response=dict(answers=self.answers()))).stdout, b'')
        self.assertEqual((self.state()['status'], self.state()['accepted']), ('accepted', True))
        self.run_hook(self.event('Stop', session='otra-sesion'))
        self.assertEqual(self.state()['status'], 'accepted')
        self.assertEqual(self.run_hook(self.event('Stop', last_assistant_message='SECRETO-FINAL')).stdout, b'')
        final = self.state()
        self.assertEqual((final['status'], final['continued']), ('continued', True))

    def test_sessions_and_projects_with_same_call_id_stay_separate(self):
        self.heartbeat()
        cases = [('sesion-A', '/Users/x/proyecto-uno'), ('sesion-B', '/Users/x/proyecto-uno'),
                 ('sesion-C', '/Users/x/otro proyecto nuevo')]
        processes = {s: self.start_hook(self.ask(session=s, call='toolu_igual', cwd=cwd)) for s, cwd in cases}
        states = {s: self.wait_state(session=s, call='toolu_igual', status='pending') for s, _ in cases}
        self.assertEqual(len({st['key'] for st in states.values()}), 3)
        self.assertEqual(states['sesion-C']['project'], 'otro proyecto nuevo')
        expected = {'sesion-B': 'respuesta B  ', 'sesion-A': 'respuesta A', 'sesion-C': EXACT}
        for session in ['sesion-B', 'sesion-A', 'sesion-C']:  # B → A → C
            self.respond(states[session], self.answers(first=expected[session]))
        for session, _ in cases:
            code, stdout = self.finish(processes[session])
            answers = json.loads(stdout.decode('utf-8'))['hookSpecificOutput']['updatedInput']['answers']
            self.assertEqual(answers, self.answers(first=expected[session]))
        # Aceptación de B no toca A.
        self.run_hook(self.event('PostToolUse', session='sesion-B', call='toolu_igual',
                                 tool_response=dict(answers=self.answers(first=expected['sesion-B']))))
        self.assertEqual(self.state('sesion-B', 'toolu_igual')['status'], 'accepted')
        self.assertEqual(self.state('sesion-A', 'toolu_igual')['status'], 'sent')
        sessions = [json.loads(p.read_text()) for p in (self.home / 'v1' / 'sessions').glob('*.json')]
        self.assertEqual(sorted(s['session_id'] for s in sessions), ['sesion-A', 'sesion-B', 'sesion-C'])

    def test_release_returns_control_and_native_answer_is_resolved_elsewhere(self):
        self.heartbeat()
        process = self.start_hook(self.ask())
        state = self.wait_state(status='pending')
        create_once(self.req() / 'release.json', dict(key=state['key'], at=time.time()))
        self.assertEqual(self.finish(process), (0, b''))
        self.assertEqual(self.state()['status'], 'released')
        self.run_hook(self.event('PostToolUse', tool_response=dict(answers={'x': 'nativa'})))
        final = self.state()
        self.assertEqual(final['status'], 'resolved_elsewhere')
        self.assertIsNone(final['answers'])

    def test_timeout_expires_without_output_and_keeps_draft(self):
        self.heartbeat()
        process = self.start_hook(self.ask(), PANCHO_CLAUDE_WAIT_SECONDS='0.8')
        state = self.wait_state(status='pending')
        self.assertLess(state['expires_at'], time.time() + 5)
        (self.req() / 'draft.json').write_text('{"other":{"q":"no perder ñ🙂"}}')
        self.assertEqual(self.finish(process), (0, b''))
        self.assertEqual(self.state()['status'], 'expired')
        self.assertEqual(json.loads((self.req() / 'draft.json').read_text())['other']['q'], 'no perder ñ🙂')
        self.run_hook(self.event('PostToolUse', tool_response=dict(answers={'q': 'contestada en Claude'})))
        self.assertEqual(self.state()['status'], 'resolved_elsewhere')

    def test_app_prefs_can_shorten_the_wait_but_never_lengthen_it(self):
        for value, expected in ((300, 300), (20, 1800), (99999, 1800), ('5', 1800), (True, 1800)):
            with self.subTest(value=value):
                self.heartbeat()
                (self.home / 'v1' / 'app' / 'prefs.json').write_text(json.dumps(dict(wait_seconds=value)))
                session = 'prefs-%s' % value
                process = self.start_hook(self.ask(session=session))
                deadline = time.time() + 10
                found = None
                while time.time() < deadline and found is None:
                    for path in (self.home / 'v1' / 'requests').glob('*/state.json'):
                        try:
                            state = json.loads(path.read_text())
                        except ValueError:
                            continue
                        if state.get('session_id') == session and state.get('status') == 'pending':
                            found = state
                    time.sleep(0.05)
                self.assertIsNotNone(found)
                self.assertAlmostEqual(found['expires_at'] - found['created_at'], expected, delta=1)
                process.kill()
                process.wait()

    def test_app_exit_during_wait_releases(self):
        app = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
        self.addCleanup(lambda: app.poll() is None and app.kill())
        self.heartbeat(pid=app.pid)
        process = self.start_hook(self.ask())
        self.wait_state(status='pending')
        app.kill()
        app.wait()
        self.assertEqual(self.finish(process), (0, b''))
        self.assertEqual(self.state()['status'], 'released')
        self.assertIn('Pancho se cerró', self.state()['message'])

    def test_live_app_with_late_heartbeat_is_not_released_after_ten_seconds(self):
        self.heartbeat(age=5.5)  # Viva al empezar (≤ 6 s, con margen para el arranque del intérprete); el latido supera 10 s sin renovarse.
        process = self.start_hook(self.ask())
        state = self.wait_state(status='pending')
        time.sleep(5.5)
        self.assertEqual(self.state()['status'], 'pending')
        self.assertIsNone(process.poll())
        self.respond(state, self.answers())
        code, stdout = self.finish(process)
        self.assertEqual(code, 0)
        self.assertTrue(stdout)
        self.assertEqual(self.state()['status'], 'sent')

    def test_heartbeat_stalled_with_live_pid_releases_after_stall_limit(self):
        self.heartbeat()  # Pid vivo (este proceso) y latido que deja de renovarse.
        started = time.monotonic()
        process = self.start_hook(self.ask(), PANCHO_HEARTBEAT_STALL_SECONDS='1.5')
        self.wait_state(status='pending')
        process.wait(timeout=8)
        self.assertLess(time.monotonic() - started, 6)
        self.assertEqual(self.finish(process), (0, b''))
        self.assertEqual(self.state()['status'], 'released')
        self.assertIn('Pancho se cerró', self.state()['message'])

    def test_heartbeat_stall_count_restarts_after_clock_jump(self):
        # Unidad del vigilante de latido con relojes falsos (no hay forma segura de saltar
        # el reloj del sistema desde un test): reposo de 120 s visto por el reloj de pared.
        clock = dict(wall=1000.0, mono=50.0)
        beat = dict(pid=os.getpid(), ts=1000.0)
        watch = claude_adapter.HeartbeatWatch(lambda: dict(beat), stall=60.0,
                                              clock=lambda: clock['wall'], mono=lambda: clock['mono'],
                                              alive=lambda pid: True)
        clock['wall'] += 40; clock['mono'] += 40
        self.assertFalse(watch.lost())
        clock['wall'] += 120; clock['mono'] += 1   # Despertar: el monótono apenas avanzó.
        self.assertFalse(watch.lost())
        clock['wall'] += 30; clock['mono'] += 30   # 30 s desde el salto: aún no.
        self.assertFalse(watch.lost())
        clock['wall'] += 31; clock['mono'] += 31   # 61 s parado desde el salto.
        self.assertTrue(watch.lost())
        beat['ts'] = clock['wall']                 # Un latido nuevo reinicia la cuenta.
        self.assertFalse(watch.lost())
        watch.alive = lambda pid: False            # Pid muerto: perdida al instante.
        self.assertTrue(watch.lost())

    def test_invalid_responses_are_rejected_until_a_valid_one_is_sent(self):
        self.heartbeat()
        process = self.start_hook(self.ask())
        state = self.wait_state(status='pending')
        invalid = [dict(generation='otra'), dict(call_id='toolu_otra'),
                   dict(answers={self.questions()[0]['question']: 'solo una'}),
                   dict(answers=self.answers(second='   '))]
        for number, override in enumerate(invalid, 1):
            self.respond(state, override.pop('answers', self.answers()), **override)
            current = self.wait_state(status='error')
            self.assertTrue(current['can_retry'])
            self.assertFalse(current['sent'])
            deadline = time.monotonic() + 5
            while (self.req() / 'response.json').exists() and time.monotonic() < deadline:
                time.sleep(0.03)
            self.assertEqual(len(list(self.req().glob('response.rejected-*.json'))), number)
            self.assertIsNone(process.poll())
        self.respond(state, self.answers())
        code, stdout = self.finish(process)
        self.assertEqual(json.loads(stdout.decode('utf-8'))['hookSpecificOutput']['updatedInput']['answers'],
                         self.answers())
        self.assertEqual((self.state()['status'], self.state()['can_retry']), ('sent', False))

    def _sent(self, call):
        self.heartbeat()
        process = self.start_hook(self.ask(call=call))
        self.respond(self.wait_state(call=call, status='pending'), self.answers())
        self.assertEqual(self.finish(process)[0], 0)
        self.assertEqual(self.state(call=call)['status'], 'sent')

    def test_mismatch_failure_or_missing_confirmation_is_unconfirmed(self):
        self._sent('c1')
        self.run_hook(self.event('PostToolUse', call='c1', tool_response=dict(answers=self.answers(first='otra'))))
        self._sent('c2')
        self.run_hook(self.event('PostToolUseFailure', call='c2', error='fallo'))
        self._sent('c3')
        self.run_hook(self.event('Stop'))
        for call in ['c1', 'c2', 'c3']:
            state = self.state(call=call)
            self.assertEqual(state['status'], 'unconfirmed')
            self.assertEqual(state['message'], 'Sin confirmar; respuesta conservada')
            self.assertEqual(state['answers'], self.answers())
            self.assertFalse(state['can_retry'])

    def test_killed_waiter_is_expired_at_stop_not_left_pending(self):
        self.heartbeat()
        process = self.start_hook(self.ask())
        self.wait_state(status='pending')
        process.kill()
        self.finish(process)
        self.assertEqual(self.state()['status'], 'pending')
        self.run_hook(self.event('Stop'))
        self.assertEqual(self.state()['status'], 'expired')

    # --- Revisión 1: espera huérfana, waiter, privacidad, bloqueo, StopFailure ------------

    def test_emit_failure_expires_without_retry(self):
        self.heartbeat()
        readonly = self.tmp / 'stdout-solo-lectura'
        readonly.write_bytes(b'')
        with open(str(readonly), 'rb') as stdout:
            process = self.start_hook(self.ask(), stdout=stdout)
        state = self.wait_state(status='pending')
        self.respond(state, self.answers())
        self.assertEqual(process.wait(timeout=10), 0)
        final = self.state()
        self.assertEqual((final['status'], final['message']), ('expired', ORPHAN))
        self.assertEqual((final['sent'], final['can_retry'], final['waiter_pid']), (False, False, None))
        events = [json.loads(l)['event'] for l in (self.req() / 'events.jsonl').read_text().splitlines()]
        self.assertEqual(events[-2:], ['sending', 'emit_failed'])

    def test_closed_stdout_reader_expires_while_waiting(self):
        self.heartbeat()
        process = self.start_hook(self.ask())
        self.wait_state(status='pending')
        process.stdout.close()  # Claude deja de leer la salida del hook.
        self.assertEqual(process.wait(timeout=5), 0)
        final = self.state()
        self.assertEqual((final['status'], final['message'], final['can_retry']), ('expired', ORPHAN, False))
        self.assertIsNone(final['waiter_pid'])

    def test_dead_host_behind_sh_or_true_expires(self):
        # Como la instalación real: host → sh -c '<hook> || true' → hook. Muere el host; sh sigue.
        self.heartbeat()
        payload = self.tmp / 'evento.json'
        payload.write_text(json.dumps(self.ask(), ensure_ascii=False))
        out = self.tmp / 'stdout-hook'
        command = ' '.join(shlex.quote(p) for p in self.command()) + ' || true'
        middle = ('import subprocess,sys,time\n'
                  'with open(sys.argv[2],"rb") as i, open(sys.argv[3],"wb") as o:\n'
                  '    subprocess.Popen(["/bin/sh","-c",sys.argv[1]],stdin=i,stdout=o)\n'
                  'time.sleep(60)\n')
        host = subprocess.Popen([sys.executable, '-c', middle, command, str(payload), str(out)], env=self.env)
        self.addCleanup(lambda: host.poll() is None and host.kill())
        state = self.wait_state(status='pending')
        self.addCleanup(lambda: os.kill(state['waiter_pid'], signal.SIGKILL)
                        if self._alive(state['waiter_pid']) else None)
        host.kill()
        host.wait()
        final = self.wait_state(status='expired', timeout=6)
        self.assertEqual((final['message'], final['can_retry'], final['waiter_pid']), (ORPHAN, False, None))
        deadline = time.monotonic() + 5
        while self._alive(state['waiter_pid']) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(self._alive(state['waiter_pid']))  # El hook terminó por sí mismo.

    @staticmethod
    def _alive(pid):
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    def test_sigterm_from_claude_expires_immediately(self):
        self.heartbeat()
        process = self.start_hook(self.ask())
        self.wait_state(status='pending')
        process.terminate()
        process.wait(timeout=5)
        final = self.state()
        self.assertEqual((final['status'], final['message']), ('expired', ORPHAN))

    def test_waiter_pid_is_published_while_waiting_and_cleared_after(self):
        self.heartbeat()
        process = self.start_hook(self.ask())
        state = self.wait_state(status='pending')
        self.assertEqual(state['waiter_pid'], process.pid)
        self.assertTrue(state['waiter_start'])
        self.respond(state, self.answers())
        self.finish(process)
        final = self.state()
        self.assertEqual(final['status'], 'sent')
        self.assertIsNone(final['waiter_pid'])
        self.assertIsNone(final['waiter_start'])

    def test_orphan_sweep_runs_on_every_session_event(self):
        for number, name in enumerate(['SessionStart', 'UserPromptSubmit', 'SessionEnd'], 1):
            call = 'huerfana-%d' % number
            self.heartbeat()
            process = self.start_hook(self.ask(call=call))
            self.wait_state(call=call, status='pending')
            process.kill()
            self.finish(process)
            self.assertEqual(self.state(call=call)['status'], 'pending')
            self.run_hook(self.event(name))
            final = self.state(call=call)
            self.assertEqual((final['status'], final['message'], final['can_retry']), ('expired', ORPHAN, False), name)

    def test_session_end_expires_sending_whose_waiter_died(self):
        self.heartbeat()
        process = self.start_hook(self.ask())
        self.wait_state(status='pending')
        process.kill()
        self.finish(process)
        path = self.req() / 'state.json'
        state = json.loads(path.read_text())
        state.update(status='sending', answers=self.answers())  # Hook muerto en pleno envío.
        path.write_text(json.dumps(state, ensure_ascii=False))
        self.run_hook(self.event('SessionEnd'))
        final = self.state()
        self.assertEqual((final['status'], final['message'], final['can_retry']), ('expired', ORPHAN, False))

    def test_reused_waiter_pid_is_not_mistaken_for_live_hook(self):
        self.heartbeat()
        process = self.start_hook(self.ask())
        self.wait_state(status='pending')
        process.kill()
        self.finish(process)
        path = self.req() / 'state.json'
        state = json.loads(path.read_text())
        state['waiter_pid'] = os.getpid()  # Pid vivo de otro proceso; hora de inicio del hook muerto.
        path.write_text(json.dumps(state, ensure_ascii=False))
        self.run_hook(self.event('UserPromptSubmit'))
        self.assertEqual(self.state()['status'], 'expired')

    def test_terminal_states_never_offer_retry(self):
        self.heartbeat()
        process = self.start_hook(self.ask())
        state = self.wait_state(status='pending')
        self.respond(state, self.answers(), generation='otra')
        self.assertTrue(self.wait_state(status='error')['can_retry'])
        deadline = time.monotonic() + 5
        while (self.req() / 'response.json').exists() and time.monotonic() < deadline:
            time.sleep(0.03)
        create_once(self.req() / 'release.json', dict(key=state['key'], at=time.time()))
        self.assertEqual(self.finish(process), (0, b''))
        final = self.state()
        self.assertEqual((final['status'], final['can_retry']), ('released', False))

    def test_app_closed_keeps_only_metadata(self):
        result = self.run_hook(self.ask())
        self.assertEqual((result.returncode, result.stdout), (0, b''))
        state = self.state()
        self.assertEqual((state['status'], state['questions'], state['question_count']), ('released', [], 2))
        self.assertEqual((state['session_id'], state['call_id']), ('sesion-A', 'toolu_1'))
        self.assertFalse((self.req() / 'input.json').exists())
        for file in self.home.rglob('*'):
            if file.is_file():
                content = file.read_bytes()
                for text in ('¿Qué frase exacta?', '¿Cuáles?', 'Descripción íntegra', 'A  doble'):
                    self.assertNotIn(text.encode('utf-8'), content, str(file))

    def _hold_lock(self):
        (self.home / 'v1').mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.home / 'v1' / '.lock'), os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        return fd

    def test_busy_lock_exits_silently_within_two_seconds(self):
        fd = self._hold_lock()
        try:
            started = time.monotonic()
            result = self.run_hook(self.event('UserPromptSubmit'), timeout=10)
            self.assertEqual((result.returncode, result.stdout), (0, b''))
            self.assertLess(time.monotonic() - started, 4)
            self.assertFalse((self.home / 'v1' / 'sessions').exists()
                             and list((self.home / 'v1' / 'sessions').glob('*.json')))
        finally:
            os.close(fd)
        log = (self.home / 'v1' / 'logs' / 'hook-errors.log').read_text()
        self.assertIn('LockBusy', log)

    def test_busy_lock_during_question_wait_is_retried_within_deadline(self):
        self.heartbeat()
        process = self.start_hook(self.ask())
        state = self.wait_state(status='pending')
        fd = self._hold_lock()
        try:
            self.respond(state, self.answers())
            time.sleep(3)  # Más que el reintento de 2 s de los demás eventos.
            self.assertIsNone(process.poll())
        finally:
            os.close(fd)
        code, stdout = self.finish(process)
        self.assertEqual(json.loads(stdout.decode('utf-8'))['hookSpecificOutput']['updatedInput']['answers'],
                         self.answers())

    def test_stop_failure_keeps_accepted_and_says_turn_failed(self):
        self._sent('c1')
        self.run_hook(self.event('PostToolUse', call='c1', tool_response=dict(answers=self.answers())))
        self.assertEqual(self.state(call='c1')['status'], 'accepted')
        result = self.run_hook(self.event('StopFailure', error='rate_limit'))
        self.assertEqual((result.returncode, result.stdout), (0, b''))
        final = self.state(call='c1')
        self.assertEqual((final['status'], final['continued']), ('accepted', False))
        self.assertIn('el turno falló', final['message'])
        record = json.loads(next((self.home / 'v1' / 'sessions').glob('*.json')).read_text())
        self.assertEqual((record['last_event'], record['activity']), ('Stop', 'idle'))

    def test_duplicate_question_texts_or_replayed_call_are_not_intercepted(self):
        self.heartbeat()
        duplicated = [dict(question='Igual', options=[]), dict(question='Igual', options=[])]
        result = self.run_hook(self.ask(questions=duplicated))
        self.assertEqual((result.returncode, result.stdout), (0, b''))
        self.assertFalse(self.req().exists())
        self.heartbeat(age=60)
        self.run_hook(self.ask(call='rep'))
        before = (self.req(call='rep') / 'state.json').read_bytes()
        self.heartbeat()
        started = time.monotonic()
        self.assertEqual(self.run_hook(self.ask(call='rep')).stdout, b'')
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual((self.req(call='rep') / 'state.json').read_bytes(), before)

    def test_session_activity_without_storing_prompt_or_messages(self):
        secrets = ['SECRETO-PROMPT-ñ🙂', 'SECRETO-FINAL', 'SECRETO-TRANSCRIPT']
        path = None
        expected = [('SessionStart', 'observed'), ('UserPromptSubmit', 'working'),
                    ('PreToolUse', 'working'), ('Stop', 'idle'), ('SessionEnd', 'ended'),
                    ('SessionStart', 'observed')]
        for name, activity in expected:
            extra = dict(prompt=secrets[0]) if name == 'UserPromptSubmit' else {}
            if name == 'Stop':
                extra = dict(last_assistant_message=secrets[1])
            if name == 'PreToolUse':
                extra = dict(tool_name='Bash', tool_use_id='b1', tool_input=dict(command=secrets[0]))
            result = self.run_hook(self.event(name, cwd='/Users/x/mi proyecto', **extra))
            self.assertEqual((result.returncode, result.stdout), (0, b''))
            path = next((self.home / 'v1' / 'sessions').glob('*.json'))
            record = json.loads(path.read_text())
            self.assertEqual(record['activity'], activity, name)
        # Claude añade solo de dónde viene la conversación (lista blanca; ver test_pancho_session_origin).
        self.assertEqual(set(record), {'provider', 'session_id', 'cwd', 'project', 'first_seen',
                                       'last_event', 'last_event_at', 'activity',
                                       'host_session', 'entrypoint', 'term_program'})
        self.assertEqual((record['project'], record['provider']), ('mi proyecto', 'claude'))
        self.run_hook(self.ask())  # App cerrada: solicitud gestionada liberada.
        for file in self.home.rglob('*'):
            if file.is_file():
                content = file.read_bytes()
                for secret in secrets:
                    self.assertNotIn(secret.encode('utf-8'), content, str(file))

    def test_store_permissions_are_private(self):
        self.run_hook(self.ask())
        for path in [self.home] + list(self.home.rglob('*')):
            mode = path.stat().st_mode & 0o777
            self.assertEqual(mode, 0o700 if path.is_dir() else 0o600, str(path))

    def test_internal_errors_exit_zero_without_stdout_or_payload_in_log(self):
        cases = [b'{no es json SECRETO-PAYLOAD', b'["lista"]', json.dumps(self.ask()).encode()[:40]]
        for raw in cases:
            result = self.run_hook(None, raw=raw)
            self.assertEqual((result.returncode, result.stdout), (0, b''))
        log = (self.home / 'v1' / 'logs' / 'hook-errors.log').read_text()
        self.assertEqual(len(log.splitlines()), 3)
        self.assertNotIn('SECRETO', log)
        blocked = self.tmp / 'archivo'
        blocked.write_text('no es directorio')
        result = self.run_hook(self.ask(), PANCHO_HOME=str(blocked / 'Pancho'))
        self.assertEqual((result.returncode, result.stdout), (0, b''))
        for argv in (['--owner', 'otro'], ['--provider', 'codex', '--owner', 'pancho-v1'], ['--rara']):
            result = subprocess.run([sys.executable, '-I', str(HOOK), 'hook'] + argv, input=b'{}',
                                    capture_output=True, env=self.env, timeout=10)
            self.assertEqual((result.returncode, result.stdout), (0, b''))

    def test_non_question_events_are_fast(self):
        timings = []
        for i in range(5):
            started = time.monotonic()
            result = self.run_hook(self.event('UserPromptSubmit', session='rapida', prompt='hola'))
            timings.append(time.monotonic() - started)
            self.assertEqual(result.returncode, 0)
        # Incluye el arranque del intérprete; el umbral es holgado para no ser frágil.
        self.assertLess(sorted(timings)[2], 0.5, timings)

    # --- avisos de permiso -------------------------------------------------------------
    SUGGESTIONS = [dict(type='addRules', rules=[dict(toolName='Bash', ruleContent='npm run lint:*')],
                        behavior='allow', destination='localSettings')]

    def permission(self, tool='Bash', tool_input=None, suggestions=SUGGESTIONS, session='sesion-P'):
        payload = dict(hook_event_name='PermissionRequest', session_id=session, cwd='/Users/x/proyecto-uno',
                       permission_mode='default', tool_name=tool,
                       tool_input=tool_input or dict(command='npm run lint  --fix', description='Lint'),
                       transcript_path='/Users/x/.claude/projects/SECRETO-TRANSCRIPT.jsonl')
        if suggestions is not None:
            payload['permission_suggestions'] = suggestions
        return payload

    def permission_state(self, status=None, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for path in (self.home / 'v1' / 'requests').glob('*/state.json'):
                try:
                    current = json.loads(path.read_text())
                except (OSError, ValueError):
                    continue
                if current.get('kind') == 'permission' and (status is None or current['status'] == status):
                    return current
            time.sleep(0.03)
        self.fail('Sin aviso de permiso en estado %s' % status)

    def decide(self, answer, **kwargs):
        self.heartbeat()
        process = self.start_hook(self.permission(**kwargs))
        state = self.permission_state('pending')
        self.respond(state, {'permission': answer})
        code, stdout = self.finish(process)
        self.assertEqual(code, 0)
        return state, (json.loads(stdout.decode('utf-8')) if stdout else None)

    def test_permission_card_shows_the_exact_command_and_three_choices(self):
        state, output = self.decide('Permitir una vez')
        (question,) = state['questions']
        self.assertEqual(question['id'], 'permission')
        self.assertIn('Bash', question['question'])
        self.assertIn('npm run lint  --fix', question['question'])
        self.assertEqual([o['label'] for o in question['options']],
                         ['Permitir una vez', 'Permitir siempre', 'Rechazar'])
        self.assertIn('Bash(npm run lint:*)', question['options'][1]['description'])
        self.assertIn('este proyecto', question['options'][1]['description'])
        self.assertEqual(output, dict(hookSpecificOutput=dict(
            hookEventName='PermissionRequest', decision=dict(behavior='allow'))))
        final = self.permission_state('accepted')
        self.assertEqual(final['message'], 'Decisión enviada a Claude')
        self.assertTrue(final['sent'])
        self.assertNotIn('SECRETO', json.dumps(final))

    def test_permission_always_returns_exactly_the_suggestions_received(self):
        _, output = self.decide('Permitir siempre')
        self.assertEqual(output['hookSpecificOutput']['decision'],
                         dict(behavior='allow', updatedPermissions=self.SUGGESTIONS))

    def test_permission_deny_and_free_text_both_deny(self):
        _, output = self.decide('Rechazar')
        self.assertEqual(output['hookSpecificOutput']['decision']['behavior'], 'deny')
        _, output = self.decide('  mejor usa pnpm  ', session='sesion-Q')
        self.assertEqual(output['hookSpecificOutput']['decision'],
                         dict(behavior='deny', message='  mejor usa pnpm  '))

    def test_permission_without_suggestions_offers_a_visible_rule_of_its_own(self):
        cases = [
            ('Bash', dict(command='git status --short'), 'Bash(git status:*)',
             dict(type='addRules', rules=[dict(toolName='Bash', ruleContent='git status:*')],
                  behavior='allow', destination='localSettings')),
            ('Bash', dict(command='ls -la /tmp'), 'Bash(ls:*)', None),
            ('Bash', dict(command='cat a.txt | sh'), 'Bash(cat a.txt | sh)', None),
            ('Bash', dict(command='cat ../../secreto'), 'Bash(cat ../../secreto)', None),
            ('WebFetch', dict(url='https://e.test:8443/x?y=1'), 'WebFetch(domain:e.test)', None),
            ('Edit', dict(file_path='/tmp/a.txt', old_string='a', new_string='b'), 'acceptEdits',
             dict(type='setMode', mode='acceptEdits', destination='session')),
            ('mcp__notas__crear', dict(titulo='x'), 'mcp__notas__crear', None),
        ]
        for index, (tool, tool_input, shown, expected) in enumerate(cases):
            for empty in ([], None):
                state, output = self.decide('Permitir siempre', tool=tool, tool_input=tool_input,
                                            suggestions=empty, session='sesion-%d-%s' % (index, empty is None))
                options = state['questions'][0]['options']
                self.assertEqual([o['label'] for o in options], ['Permitir una vez', 'Permitir siempre', 'Rechazar'])
                self.assertIn(shown, options[1]['description'])
                self.assertEqual(state['always_rule'], 'pancho')
                decision = output['hookSpecificOutput']['decision']
                self.assertEqual(decision['behavior'], 'allow')
                if expected is not None:
                    self.assertEqual(decision['updatedPermissions'], [expected])

    def test_unreadable_suggestions_never_offer_or_accept_always(self):
        self.heartbeat()
        process = self.start_hook(self.permission(suggestions=['raro']), PANCHO_CLAUDE_WAIT_SECONDS='4')
        state = self.permission_state('pending')
        self.assertEqual([o['label'] for o in state['questions'][0]['options']], ['Permitir una vez', 'Rechazar'])
        self.respond(state, {'permission': 'Permitir siempre'})
        self.assertTrue(self.permission_state('error')['can_retry'])
        self.respond(state, {'permission': 'Permitir una vez'})
        code, stdout = self.finish(process)
        self.assertEqual(json.loads(stdout)['hookSpecificOutput']['decision'], dict(behavior='allow'))

    def test_permission_with_pancho_closed_leaves_the_native_prompt_alone(self):
        result = self.run_hook(self.permission())
        self.assertEqual((result.returncode, result.stdout), (0, b''))
        state = self.permission_state('released')
        self.assertEqual(state['questions'], [])
        self.assertFalse((self.home / 'v1' / 'requests' / state['key'] / 'input.json').exists())
        self.assertNotIn('npm run lint', json.dumps(state))

    def test_permission_for_questions_and_plans_is_not_intercepted(self):
        self.heartbeat()
        for tool in ('AskUserQuestion', 'ExitPlanMode'):
            result = self.run_hook(self.permission(tool=tool, tool_input=dict(plan='x')))
            self.assertEqual((result.returncode, result.stdout), (0, b''))
        self.assertEqual(list((self.home / 'v1' / 'requests').glob('*/state.json')), [])

    def test_permission_still_pending_at_end_of_turn_was_resolved_in_claude(self):
        self.heartbeat()
        process = self.start_hook(self.permission())
        self.permission_state('pending')
        self.run_hook(self.event('Stop', session='sesion-P'))
        final = self.permission_state('resolved_elsewhere')
        self.assertEqual(final['message'], 'Resuelto en Claude')
        self.assertEqual(self.finish(process), (0, b''))

    def ran(self, name='PostToolUse', tool='Bash', tool_input=None, session='sesion-P'):
        return self.event(name, session=session, tool_name=tool, tool_use_id='toolu_perm',
                          tool_input=tool_input or dict(command='npm run lint  --fix', description='Lint'))

    def test_permission_granted_in_claude_is_noticed_when_the_tool_runs(self):
        for name in ('PostToolUse', 'PostToolUseFailure'):
            with self.subTest(event=name):
                self.heartbeat()
                process = self.start_hook(self.permission())
                key = self.permission_state('pending')['key']
                self.run_hook(self.ran(name))
                final = self.permission_state('resolved_elsewhere')
                self.assertEqual(final['message'], 'Resuelto en Claude')
                self.assertEqual(self.finish(process), (0, b''))
                events = (self.home / 'v1' / 'requests' / key / 'events.jsonl').read_text()
                self.assertNotIn('npm run lint', events.split('resolved_elsewhere')[-1])
                shutil.rmtree(str(self.home / 'v1' / 'requests' / key))

    def test_other_tools_running_do_not_resolve_a_pending_permission(self):
        self.heartbeat()
        process = self.start_hook(self.permission())
        self.permission_state('pending')
        self.run_hook(self.ran(tool='Read', tool_input=dict(file_path='/tmp/a')))
        self.run_hook(self.ran(tool_input=dict(command='ls')))
        self.run_hook(self.ran(session='otra-sesion'))
        self.assertEqual(self.permission_state()['status'], 'pending')
        self.run_hook(self.event('Stop', session='sesion-P'))
        self.permission_state('resolved_elsewhere')
        self.assertEqual(self.finish(process), (0, b''))

    def test_long_permission_detail_is_cut_with_a_visible_warning(self):
        state, _ = self.decide('Rechazar', tool_input=dict(command='echo ' + 'a' * 9000))
        text = state['questions'][0]['question']
        self.assertLess(len(text), 4300)
        self.assertIn('recortado', text)

    def test_simulation_uses_separate_marked_root(self):
        sim = self.tmp / 'simulation'
        env = dict(self.env, PANCHO_HOME=str(sim))
        run = subprocess.run([sys.executable, '-I', str(HOOK), 'simulate', 'ask', '--home', str(sim)],
                             capture_output=True, env=env, timeout=15)
        self.assertEqual(run.returncode, 0, run.stderr)
        info = json.loads(run.stdout.decode('utf-8'))
        self.assertEqual(json.loads((sim / 'v1' / 'app' / 'mode.json').read_text()), dict(simulation=True))
        state = json.loads((Path(info['directory']) / 'state.json').read_text())
        self.assertEqual((state['status'], state['simulation']), ('pending', True))
        self.assertFalse(self.home.exists())
        self.home.joinpath('v1').mkdir(parents=True)
        refused = subprocess.run([sys.executable, '-I', str(HOOK), 'simulate', 'ask', '--home', str(self.home)],
                                 capture_output=True, env=self.env, timeout=15)
        self.assertNotEqual(refused.returncode, 0)
        self.assertFalse((self.home / 'v1' / 'requests').exists())

    def test_simulated_full_flow_with_auto_accept(self):
        sim = self.tmp / 'simulation'
        process = subprocess.Popen([sys.executable, '-I', str(HOOK), 'simulate', 'ask', '--home', str(sim),
                                    '--session', 'SIM', '--wait', '10', '--auto-accept'],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env)
        self.addCleanup(lambda: process.poll() is None and process.kill())
        deadline = time.monotonic() + 8
        states = []
        while not states and time.monotonic() < deadline:
            states = list((sim / 'v1' / 'requests').glob('*/state.json')) if sim.exists() else []
            time.sleep(0.05)
        self.assertTrue(states)
        state = json.loads(states[0].read_text())
        answers = {q['id']: 'simulada' for q in state['questions']}
        create_once(states[0].parent / 'response.json', dict(
            key=state['key'], provider='claude', session_id='SIM', call_id=state['call_id'],
            generation=state['generation'], answers=answers, created_at=time.time()))
        stdout, _ = process.communicate(timeout=15)
        self.assertEqual(json.loads(stdout.decode('utf-8'))['status'], 'continued')


if __name__ == '__main__':
    unittest.main()
