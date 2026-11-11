"""Adaptador Codex de Pancho (T2) por sus seams públicos: el hook como proceso real
(stdin → sin stdout) y `codex-send` como proceso real contra un servidor IPC Unix FALSO.

Nunca se usa el HOME, la configuración ni el socket reales de Codex: HOME y la raíz de
Pancho son temporales, la app Codex es un Info.plist + ASAR falsos y el «Codex» que
contesta es un hilo de este test (patrón de tests/test_codex_ipc.py).
"""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / 'scripts' / 'pancho_hook.py'
EXACT = '  Dos  espacios,\nsalto de línea, tildes áéíóú ñ, emoji 🙂 y espacio final  '
SECRET_PROMPT = 'PROMPT-AJENO-SECRETO no debe guardarse'
OWNER_ID = '85648110-c5e4-441f-baf7-17608982cc89'
OPEN, CLOSE = '<send_user_message_question_reply>', '</send_user_message_question_reply>'


def key_for(session, call):
    return hashlib.sha256('|'.join(['codex', 'codex-desktop', session, call]).encode('utf-8')).hexdigest()


def qid(call, index):
    return json.dumps(['request_user_input_async', call, index], separators=(',', ':'))


def create_once(path, value):
    tmp = path.with_name('.tmp-' + os.urandom(4).hex())
    tmp.write_text(json.dumps(value, ensure_ascii=False))
    try:
        os.link(str(tmp), str(path))
    finally:
        tmp.unlink()


def read_exact(peer, size):
    data = b''
    while len(data) < size:
        chunk = peer.recv(size - len(data))
        if not chunk:
            raise EOFError
        data += chunk
    return data



# Piezas de Codex Desktop que usa Pancho (decisión 2026-11-11: pin por interfaz, no por versión).
INTERFACE = (b'thread-owner-discovery', b'thread-follower-steer-turn', b'thread-follower-start-turn',
             b'send_user_message_question_reply', b'request_user_input_async')


def asar_bytes(files):
    """ASAR con el formato real: [4][S][S-4][L] + JSON de cabecera; los datos empiezan en 8 + S."""
    tree, blob = {}, b''
    for path, data in files.items():
        node = tree
        parts = path.split('/')
        for part in parts[:-1]:
            node = node.setdefault(part, {'files': {}})['files']
        node[parts[-1]] = {'size': len(data), 'offset': str(len(blob))}
        blob += data
    header = json.dumps({'files': tree}).encode()
    pad = (4 - len(header) % 4) % 4
    pickle = struct.pack('<I', len(header)) + header + b'\0' * pad
    return struct.pack('<III', 4, len(pickle) + 4, len(pickle)) + pickle + blob


class FakeCodex:
    """Servidor Unix que imita el router IPC de la app: discovery, start-turn v2 y steer-turn v1.

    `steer_mode`: 'ok' ({turnId}), 'error' (resultType error del owner), 'drop' (cierra sin
    responder) o 'no_turn' (éxito sin turnId: ambiguo).
    """

    def __init__(self, directory, drop_start=False):
        self.path = Path(directory) / 'ipc.sock'
        self.requests = []
        self.drop_start = drop_start
        self.steer_mode = 'ok'
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(self.path))
        os.chmod(str(self.path), 0o600)
        self.listener.listen(8)
        self.listener.settimeout(0.2)
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while not self.stopped.is_set():
            try:
                peer, _ = self.listener.accept()
            except (socket.timeout, OSError):
                continue
            with peer:
                try:
                    peer.settimeout(3)
                    size = struct.unpack('<I', read_exact(peer, 4))[0]
                    request = json.loads(read_exact(peer, size).decode('utf-8'))
                    self.requests.append(request)
                    method = request['method']
                    if method == 'thread-follower-start-turn' and self.drop_start:
                        continue  # Cierra sin responder: resultado incierto.
                    if method == 'thread-follower-steer-turn' and self.steer_mode == 'drop':
                        continue
                    result = {}
                    if method == 'thread-owner-discovery':
                        result = {'supportsUntrustedAppInput': True}
                    elif method == 'thread-follower-steer-turn' and self.steer_mode == 'ok':
                        result = {'turnId': 'turn-steer-ack'}
                    reply = dict(type='response', requestId=request['requestId'], resultType='success',
                                 method=method, handledByClientId=OWNER_ID, result=result)
                    if method == 'thread-follower-steer-turn' and self.steer_mode == 'error':
                        reply = dict(type='response', requestId=request['requestId'], resultType='error',
                                     error='turn-not-in-progress')
                    data = json.dumps(reply).encode('utf-8')
                    peer.sendall(struct.pack('<I', len(data)) + data)
                except (EOFError, OSError, ValueError):
                    pass

    def starts(self):
        return [r for r in self.requests if r['method'] == 'thread-follower-start-turn']

    def steers(self):
        return [r for r in self.requests if r['method'] == 'thread-follower-steer-turn']

    def close(self):
        self.stopped.set()
        self.thread.join(3)
        self.listener.close()


class PanchoCodexTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='pc-', dir='/tmp')  # Ruta corta para AF_UNIX.
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.home = self.tmp / 'Pancho'
        self.env = dict(os.environ, PANCHO_HOME=str(self.home), HOME=str(self.tmp / 'home'))
        for name in ('PANCHO_CODEX_UPS_SECONDS', 'PANCHO_CLAUDE_WAIT_SECONDS'):
            self.env.pop(name, None)
        self.codex = FakeCodex(self.tmp)
        self.addCleanup(self.codex.close)
        self.make_app()

    # --- app Codex falsa y pin ------------------------------------------------------
    def make_app(self, version='26.1103.41825'):
        app = self.tmp / 'ChatGPT.app'
        (app / 'Contents' / 'Resources').mkdir(parents=True, exist_ok=True)
        (app / 'Contents' / 'Info.plist').write_bytes(plistlib.dumps(
            {'CFBundleIdentifier': 'com.openai.codex', 'CFBundleShortVersionString': version}))
        header, one, two = b'H' * 64, b'modulo uno ' * 50, b'modulo dos ' * 70
        self.asar = app / 'Contents' / 'Resources' / 'app.asar'
        self.asar.write_bytes(header + one + two)
        pin = dict(app_path=str(app), bundle_id='com.openai.codex', expected_app_version='26.1103.41825',
                   archive_path=str(self.asar), data_offset=len(header), files=[
                       dict(path='a.js', size=len(one), offset='0', sha256=hashlib.sha256(one).hexdigest()),
                       dict(path='b.js', size=len(two), offset=str(len(one)),
                            sha256=hashlib.sha256(two).hexdigest())])
        (self.home / 'install').mkdir(parents=True, exist_ok=True)
        (self.home / 'install' / 'codex-app-pin.json').write_text(json.dumps(pin))

    # --- utilidades ---------------------------------------------------------------
    def heartbeat(self):
        directory = self.home / 'v1' / 'app'
        directory.mkdir(parents=True, exist_ok=True)
        (directory / 'heartbeat.json').write_text(json.dumps(dict(pid=os.getpid(), ts=time.time(), version='1')))

    def hook(self, payload, socket_arg=True):
        command = [sys.executable, '-I', str(HOOK), 'hook', '--provider', 'codex', '--owner', 'pancho-v1']
        if socket_arg:
            command += ['--codex-ipc-socket', str(self.codex.path)]
        run = subprocess.run(command, input=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
                             capture_output=True, env=self.env, timeout=20)
        self.assertEqual((run.returncode, run.stdout), (0, b''), run.stderr)
        return run

    def send(self, session, call='call_1', extra=()):
        run = subprocess.run([sys.executable, '-I', str(HOOK), 'codex-send', '--key', key_for(session, call),
                              '--ipc-socket', str(self.codex.path)] + list(extra),
                             capture_output=True, env=self.env, timeout=60)
        return run.returncode, json.loads(run.stdout.decode('utf-8'))

    def event(self, name, session, turn='turn-1', cwd='/Users/x/proyecto-uno', call='call_1', **extra):
        payload = dict(hook_event_name=name, session_id=session, cwd=cwd, turn_id=turn, model='gpt',
                       transcript_path='/Users/x/.codex/sessions/SECRETO-TRANSCRIPT.jsonl')
        if name in ('PreToolUse', 'PostToolUse'):
            payload.update(tool_name='request_user_input_async', tool_use_id=call)
        payload.update(extra)
        return payload

    def ask(self, session, call='call_1', turn='turn-1', cwd='/Users/x/proyecto-uno'):
        questions = [dict(title='¿Qué frase exacta?' + EXACT, options=['A  doble — descripción íntegra FIN', 'B 🙂']),
                     dict(title='¿Qué frase exacta?' + EXACT, options=['Repetido'])]
        self.hook(self.event('PreToolUse', session, turn, cwd, call, tool_input=dict(questions=questions)))
        self.hook(self.event('PostToolUse', session, turn, cwd, call, tool_input=dict(questions=questions),
                             tool_response='{"accepted":true}'))
        return questions

    def answer(self, session, call='call_1', answers=None):
        state = self.state(session, call)
        answers = answers or {qid(call, 0): EXACT, qid(call, 1): 'Repetido'}
        create_once(self.req(session, call) / 'response.json', dict(
            key=state['key'], provider='codex', session_id=session, call_id=call,
            generation=state['generation'], answers=answers, created_at=time.time()))
        return answers

    def envelope(self, call, answers, questions):
        items = [dict(questionItemId=qid(call, i), question=q['title'], answer=answers[qid(call, i)])
                 for i, q in enumerate(questions)]
        return OPEN + '\n' + json.dumps(items, ensure_ascii=False, separators=(',', ':')) + '\n' + CLOSE

    def req(self, session, call='call_1'):
        return self.home / 'v1' / 'requests' / key_for(session, call)

    def state(self, session, call='call_1'):
        return json.loads((self.req(session, call) / 'state.json').read_text())

    def wait_status(self, session, status, call='call_1', timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            current = self.state(session, call)
            if current['status'] == status:
                return current
            time.sleep(0.05)
        self.fail('Estado %s no alcanzado; actual %s' % (status, self.state(session, call)['status']))

    def all_bytes(self):
        return b''.join(p.read_bytes() for p in self.home.rglob('*') if p.is_file())

    # --- registro de la pregunta -----------------------------------------------------
    def test_question_registers_without_waiting_in_any_cwd_with_native_ids(self):
        self.heartbeat()
        session = str(uuid.uuid4())
        started = time.monotonic()
        questions = self.ask(session, cwd='/Users/x/proyecto-nuevo-sin-marcador')
        self.assertLess(time.monotonic() - started, 8)
        state = self.state(session)
        self.assertEqual((state['provider'], state['host'], state['status']), ('codex', 'codex-desktop', 'pending'))
        self.assertEqual(state['project'], 'proyecto-nuevo-sin-marcador')
        self.assertIsNone(state['expires_at'])
        self.assertEqual([q['id'] for q in state['questions']], [qid('call_1', 0), qid('call_1', 1)])
        self.assertEqual(state['questions'][0]['question'], questions[0]['title'])
        self.assertEqual([o['label'] for o in state['questions'][0]['options']], questions[0]['options'])
        self.assertEqual({q['multiSelect'] for q in state['questions']}, {False})
        self.assertTrue(state['question_published'])
        self.assertFalse((self.req(session) / 'input.json').exists())
        self.assertNotIn(b'SECRETO-TRANSCRIPT', self.all_bytes())
        sessions = [json.loads(p.read_text()) for p in (self.home / 'v1' / 'sessions').glob('*.json')]
        self.assertEqual([(s['provider'], s['session_id']) for s in sessions], [('codex', session)])

    def test_app_closed_keeps_only_metadata_and_native_answer_resolves_in_codex(self):
        session = str(uuid.uuid4())
        self.ask(session)
        state = self.state(session)
        self.assertEqual((state['status'], state['questions'], state['question_count']), ('released', [], 2))
        self.assertIn('Codex', state['message'])
        self.assertNotIn('frase exacta'.encode('utf-8'), self.all_bytes())
        native = OPEN + '\n' + json.dumps([dict(questionItemId=qid('call_1', 0), question='x', answer='nativa')],
                                          separators=(',', ':')) + '\n' + CLOSE
        self.hook(self.event('UserPromptSubmit', session, 'turn-2', prompt=native))
        state = self.state(session)
        self.assertEqual((state['status'], state['message']), ('resolved_elsewhere', 'Resuelta en Codex'))
        self.assertNotIn(b'nativa', self.all_bytes())

    # --- envío, aceptación y continuación --------------------------------------------
    def test_send_after_origin_stop_exact_envelope_then_ups_and_stop_accept(self):
        self.heartbeat()
        session = str(uuid.uuid4())
        questions = self.ask(session)
        self.hook(self.event('Stop', session, 'turn-1', last_assistant_message='ajeno'))
        answers = self.answer(session)
        code, out = self.send(session)
        self.assertEqual((code, out['result'], out['status']), (0, 'sent', 'sent'), out)
        methods = [(r['method'], r['version']) for r in self.codex.requests]
        self.assertEqual(methods, [('thread-owner-discovery', 1), ('thread-follower-start-turn', 2)])
        start = self.codex.starts()[0]
        envelope = self.envelope('call_1', answers, questions)
        self.assertEqual(start['params']['conversationId'], session)
        self.assertEqual(start['params']['turnStart']['request']['input'][0]['text'], envelope + '\n')
        self.assertEqual(start['targetClientId'], OWNER_ID)
        state = self.state(session)
        self.assertEqual((state['sent'], state['accepted'], state['answers']), (True, False, answers))
        self.assertEqual((state['transport_mode'], state['message']), ('start', 'Enviada; esperando confirmación de Codex'))
        self.assertEqual(self.codex.steers(), [])
        # UPS con el envelope exacto: Codex lo recibió → aceptada ya, sin esperar al fin del turno (r3).
        self.hook(self.event('UserPromptSubmit', session, 'turn-reply', prompt=envelope + '\n'))
        state = self.state(session)
        self.assertEqual((state['status'], state['accepted'], state['continued'], state['message']),
                         ('accepted', True, False, 'Codex recibió la respuesta y sigue trabajando'))
        # Stop de otro turno no acepta; Stop del turno con el envelope sí.
        self.hook(self.event('Stop', session, 'turn-reply', last_assistant_message='ok'))
        state = self.state(session)
        self.assertEqual((state['status'], state['accepted'], state['continued']), ('continued', True, True))
        events = [json.loads(l)['event'] for l in (self.req(session) / 'events.jsonl').read_text().splitlines()]
        self.assertIn('accepted', events)
        self.assertNotIn(SECRET_PROMPT.encode(), self.all_bytes())
        # Una segunda invocación no reenvía.
        code, out = self.send(session)
        self.assertEqual((code, out['result']), (11, 'already'))
        self.assertEqual(len(self.codex.starts()), 1)

    def test_r2_active_turn_is_answered_by_steer_and_accepted_by_ups_and_stop_of_that_turn(self):
        self.heartbeat()
        session = str(uuid.uuid4())
        self.hook(self.event('UserPromptSubmit', session, 'turn-1', prompt=SECRET_PROMPT))
        questions = self.ask(session)  # Codex sigue trabajando en turn-1: sin Stop.
        answers = self.answer(session)
        code, out = self.send(session)
        self.assertEqual((code, out['result'], out['status']), (0, 'sent', 'sent'), out)
        self.assertEqual(self.codex.starts(), [])
        self.assertEqual([(r['method'], r['version']) for r in self.codex.requests],
                         [('thread-owner-discovery', 1), ('thread-follower-steer-turn', 1)])
        steer = self.codex.steers()[0]
        envelope = self.envelope('call_1', answers, questions)
        self.assertEqual(steer['targetClientId'], OWNER_ID)
        params = steer['params']
        self.assertEqual(params['conversationId'], session)
        self.assertEqual(params['input'], [dict(type='text', text=envelope + '\n', text_elements=[])])
        self.assertEqual(params['restoreMessage']['context']['turnTrigger'], 'send_user_message_async_question')
        self.assertEqual(params['restoreMessage']['text'], envelope)
        self.assertNotIn('toolOutput', params)
        state = self.state(session)
        self.assertEqual((state['transport_mode'], state['steer_turn_id']), ('steer', 'turn-steer-ack'))
        self.assertEqual(state['message'], 'Enviada a Codex mientras trabaja; esperando confirmación')
        # Nuestro UPS llega con el turno ACTIVO (el de origen): es nuestra respuesta, no «resuelta en Codex».
        self.hook(self.event('UserPromptSubmit', session, 'turn-1', prompt=envelope + '\n'))
        state = self.state(session)
        self.assertEqual((state['status'], state['reply_turn_id'], state['accepted'], state['message']),
                         ('accepted', 'turn-1', True, 'Codex recibió la respuesta y sigue trabajando'))
        self.hook(self.event('Stop', session, 'turn-1'))
        state = self.state(session)
        self.assertEqual((state['status'], state['accepted'], state['continued']), ('continued', True, True))
        self.assertNotIn(SECRET_PROMPT.encode(), self.all_bytes())

    def test_r2_steer_rejection_or_ambiguity_is_uncertain_and_never_falls_back_to_start(self):
        self.heartbeat()
        for mode in ('error', 'drop'):
            with self.subTest(mode=mode):
                self.codex.steer_mode = mode
                session = str(uuid.uuid4())
                self.ask(session)
                answers = self.answer(session)
                code, out = self.send(session)
                self.assertEqual((code, out['result']), (22, 'uncertain'), out)
                state = self.state(session)
                self.assertEqual((state['status'], state['can_retry'], state['answers']), ('error', False, answers))
                self.assertEqual(state['transport_mode'], 'steer')
                self.assertEqual(self.send(session)[0], 11)
                self.assertEqual(self.codex.starts(), [])

    def test_r3_steer_ack_without_turn_id_shows_sent_not_error_and_ups_accepts(self):
        # Visto en real (2026-11-11): el ack del steer llegó sin turnId y el mensaje sí se entregó.
        self.heartbeat()
        self.codex.steer_mode = 'no_turn'
        session = str(uuid.uuid4())
        self.hook(self.event('UserPromptSubmit', session, 'turn-1', prompt=SECRET_PROMPT))
        questions = self.ask(session)
        answers = self.answer(session)
        code, out = self.send(session)
        self.assertEqual((code, out['status']), (0, 'sent'), out)
        state = self.state(session)
        self.assertEqual((state['status'], state['can_retry'], state['accepted'], state['steer_turn_id'], state['message']),
                         ('sent', False, False, None, 'Enviada a Codex mientras trabaja; esperando confirmación'))
        events = [json.loads(l) for l in (self.req(session) / 'events.jsonl').read_text().splitlines()]
        ambiguous = [e for e in events if e['event'] == 'transport_ambiguous']
        self.assertEqual(len(ambiguous), 1)
        self.assertIsInstance(ambiguous[0]['result_keys'], list)
        self.assertEqual(self.send(session)[0], 11)  # Nunca se reenvía ni se cae a start.
        self.assertEqual((len(self.codex.steers()), self.codex.starts()), (1, []))
        self.hook(self.event('UserPromptSubmit', session, 'turn-1',
                             prompt=self.envelope('call_1', answers, questions) + '\n'))
        self.assertEqual(self.state(session)['status'], 'accepted')
        self.hook(self.event('Stop', session, 'turn-1'))
        self.assertEqual(self.state(session)['status'], 'continued')

    def test_r3_steer_ack_without_turn_id_and_no_ups_ends_unconfirmed_at_stop(self):
        self.heartbeat()
        self.codex.steer_mode = 'no_turn'
        session = str(uuid.uuid4())
        self.hook(self.event('UserPromptSubmit', session, 'turn-1', prompt=SECRET_PROMPT))
        self.ask(session)
        answers = self.answer(session)
        self.assertEqual(self.send(session)[0], 0)
        self.hook(self.event('Stop', session, 'turn-1'))
        state = self.state(session)
        self.assertEqual((state['status'], state['accepted'], state['answers']), ('unconfirmed', False, answers))

    def test_r3_accepted_reply_whose_stop_was_lost_continues_at_the_next_turn(self):
        self.heartbeat()
        session = str(uuid.uuid4())
        self.hook(self.event('UserPromptSubmit', session, 'turn-1', prompt=SECRET_PROMPT))
        questions = self.ask(session)
        answers = self.answer(session)
        self.assertEqual(self.send(session)[0], 0)
        self.hook(self.event('UserPromptSubmit', session, 'turn-1',
                             prompt=self.envelope('call_1', answers, questions) + '\n'))
        self.hook(self.event('UserPromptSubmit', session, 'turn-2', prompt=SECRET_PROMPT))
        state = self.state(session)
        self.assertEqual((state['status'], state['continued']), ('continued', True))

    def test_r2_two_answers_in_one_active_chat_b_then_a_use_the_conversation_lock(self):
        self.heartbeat()
        session = str(uuid.uuid4())
        questions = self.ask(session, call='call_A')
        self.ask(session, call='call_B')
        b = self.answer(session, 'call_B', {qid('call_B', 0): 'B exacta ', qid('call_B', 1): 'Repetido'})
        self.assertEqual(self.send(session, 'call_B')[0], 0)
        a = self.answer(session, 'call_A', {qid('call_A', 0): 'A exacta\n', qid('call_A', 1): 'Repetido'})
        code, out = self.send(session, 'call_A')
        self.assertEqual((code, out['status']), (10, 'queued'))
        self.assertEqual(self.state(session, 'call_A')['message'],
                         'En cola: espera a que Codex confirme la respuesta anterior de este chat')
        # r4: en cuanto Codex entrega B a su modelo (UPS exacto), A sale por steer sin esperar al Stop.
        self.hook(self.event('UserPromptSubmit', session, 'turn-1', prompt=self.envelope('call_B', b, questions)))
        self.assertEqual(self.state(session, 'call_B')['status'], 'accepted')
        self.wait_status(session, 'sent', call='call_A')
        self.assertEqual([(r['method']) for r in self.codex.requests if r['method'] != 'thread-owner-discovery'],
                         ['thread-follower-steer-turn', 'thread-follower-steer-turn'])
        self.assertEqual([s['params']['input'][0]['text'] for s in self.codex.steers()],
                         [self.envelope('call_B', b, questions) + '\n', self.envelope('call_A', a, questions) + '\n'])
        self.assertEqual(self.state(session, 'call_A')['transport_mode'], 'steer')
        self.hook(self.event('UserPromptSubmit', session, 'turn-1', prompt=self.envelope('call_A', a, questions)))
        self.hook(self.event('Stop', session, 'turn-1'))
        self.assertEqual([self.state(session, c)['status'] for c in ('call_A', 'call_B')], ['continued', 'continued'])

    def test_two_sessions_answered_b_then_a_never_cross(self):
        self.heartbeat()
        a, b = str(uuid.uuid4()), str(uuid.uuid4())
        questions = self.ask(a, cwd='/Users/x/mismo')
        self.ask(b, cwd='/Users/x/mismo')
        self.assertNotEqual(key_for(a, 'call_1'), key_for(b, 'call_1'))
        for s in (a, b):
            self.hook(self.event('Stop', s, 'turn-1'))
        answers_b = self.answer(b, answers={qid('call_1', 0): 'respuesta b pinguino ', qid('call_1', 1): 'Repetido'})
        self.assertEqual(self.send(b)[0], 0)
        answers_a = self.answer(a, answers={qid('call_1', 0): 'piñata\n🙂', qid('call_1', 1): 'Repetido'})
        self.assertEqual(self.send(a)[0], 0)
        starts = self.codex.starts()
        self.assertEqual([s['params']['conversationId'] for s in starts], [b, a])
        env_a, env_b = self.envelope('call_1', answers_a, questions), self.envelope('call_1', answers_b, questions)
        self.assertEqual([s['params']['turnStart']['request']['input'][0]['text'] for s in starts],
                         [env_b + '\n', env_a + '\n'])
        # El envelope de A en la sesión B no acepta nada.
        self.hook(self.event('UserPromptSubmit', b, 'rb', prompt=env_a))
        self.hook(self.event('Stop', b, 'rb'))
        self.assertNotEqual(self.state(b)['status'], 'continued')
        self.hook(self.event('UserPromptSubmit', a, 'ra', prompt=env_a))
        self.hook(self.event('Stop', a, 'ra'))
        self.assertEqual(self.state(a)['status'], 'continued')
        self.assertEqual(self.state(b)['answers'], answers_b)

    def test_same_conversation_second_answer_waits_only_until_first_is_accepted(self):
        self.heartbeat()
        session = str(uuid.uuid4())
        questions = self.ask(session, call='call_1')
        self.ask(session, call='call_2')
        self.hook(self.event('Stop', session, 'turn-1'))
        first = self.answer(session, 'call_1')
        self.assertEqual(self.send(session, 'call_1')[0], 0)
        self.answer(session, 'call_2', {qid('call_2', 0): 'segunda', qid('call_2', 1): 'Repetido'})
        code, out = self.send(session, 'call_2')
        self.assertEqual((code, out['status']), (10, 'queued'))
        self.assertEqual(len(self.codex.starts()), 1)
        # r4: aceptada la primera (UPS exacto), la segunda sale por steer al turno de la respuesta.
        self.hook(self.event('UserPromptSubmit', session, 'r1', prompt=self.envelope('call_1', first, questions)))
        self.assertEqual(self.state(session, 'call_1')['status'], 'accepted')
        self.wait_status(session, 'sent', call='call_2')
        self.assertEqual((len(self.codex.starts()), len(self.codex.steers())), (1, 1))

    # --- sin confirmación ----------------------------------------------------------
    def test_without_matching_ups_or_stop_it_becomes_unconfirmed_keeping_answers(self):
        self.heartbeat()
        s1, s2, s3, s4 = (str(uuid.uuid4()) for _ in range(4))
        for s in (s1, s2, s3, s4):
            questions = self.ask(s)
            self.hook(self.event('Stop', s, 'turn-1'))
            self.answer(s)
            self.assertEqual(self.send(s)[0], 0)
        # 1) Otro prompt en vez del envelope.
        self.hook(self.event('UserPromptSubmit', s1, 'otro', prompt=SECRET_PROMPT))
        # 2) UPS exacto y el Stop llega de otro turno (r3: aceptada desde el UPS).
        answers = self.state(s2)['answers']
        self.hook(self.event('UserPromptSubmit', s2, 'r2', prompt=self.envelope('call_1', answers, questions)))
        self.hook(self.event('Stop', s2, 'turno-distinto'))
        # 3) Sin UPS: plazo de 120 s (acortado solo en el test) evaluado en el siguiente evento.
        self.env['PANCHO_CODEX_UPS_SECONDS'] = '0.2'
        time.sleep(0.3)
        self.hook(self.event('SessionStart', s3, source='resume'))
        # 4) La conversación termina (SessionEnd) sin UPS.
        self.hook(self.event('SessionEnd', s4, reason='other'))
        for s in (s1, s3, s4):
            state = self.state(s)
            self.assertEqual((state['status'], state['message']), ('unconfirmed', 'Sin confirmar; respuesta conservada'))
            self.assertEqual(state['answers'][qid('call_1', 0)], EXACT)
            self.assertFalse(state['accepted'])
        # r3: el UPS exacto ya es la aceptación; el Stop de otro turno solo cierra el turno.
        state = self.state(s2)
        self.assertEqual((state['status'], state['accepted'], state['continued']), ('continued', True, True))
        self.assertNotIn(SECRET_PROMPT.encode(), self.all_bytes())

    def test_native_answer_cancels_queued_reply_and_resolves_in_codex(self):
        self.heartbeat()
        session = str(uuid.uuid4())
        questions = [dict(title='¿Qué frase exacta?' + EXACT, options=['A']),
                     dict(title='¿Qué frase exacta?' + EXACT, options=['Repetido'])]
        self.hook(self.event('PreToolUse', session, tool_input=dict(questions=questions)))
        self.answer(session)
        self.assertEqual(self.send(session)[0], 10)  # En cola: aún no se vio la publicación.
        native = OPEN + '\n' + json.dumps([dict(questionItemId=qid('call_1', 0), question='q', answer='Otra')],
                                          ensure_ascii=False, separators=(',', ':')) + '\n' + CLOSE
        # Respuesta nativa por steer: mismo turn_id que el origen, sin Stop previo.
        self.hook(self.event('UserPromptSubmit', session, 'turn-1', prompt=native))
        self.assertEqual(self.state(session)['status'], 'resolved_elsewhere')
        self.hook(self.event('PostToolUse', session, tool_input=dict(questions=questions),
                             tool_response='{"accepted":true}'))
        self.hook(self.event('Stop', session, 'turn-1'))
        time.sleep(2.0)  # Margen para un despachador que no debe existir.
        state = self.state(session)
        self.assertEqual(state['status'], 'resolved_elsewhere')
        self.assertEqual(state['answers'][qid('call_1', 0)], EXACT)  # Lo de Pancho se conserva.
        self.assertEqual(self.send(session)[0], 23)
        self.assertEqual(self.codex.starts() + self.codex.steers(), [])

    # --- pin por interfaz (2026-11-11) ---------------------------------------------------
    def make_interface_app(self, version='26.1103.60933', bundle='com.openai.codex', drop=None):
        app = self.tmp / 'ChatGPT.app'
        (app / 'Contents' / 'Resources').mkdir(parents=True, exist_ok=True)
        (app / 'Contents' / 'Info.plist').write_bytes(plistlib.dumps(
            {'CFBundleIdentifier': bundle, 'CFBundleShortVersionString': version}))
        pieces = [n for n in INTERFACE if n != drop]
        files = {'.vite/build/early-bootstrap-339-%s.js' % version.replace('.', ''): b'x=1;' + b';'.join(b'"%s"' % n for n in pieces[:3]),
                 '.vite/build/early-bootstrap-815-abc.js': b'y=2;' + b';'.join(b'"%s"' % n for n in pieces[3:]),
                 'node_modules/otra/index.js': b'"%s"' % (drop or b'nada')}
        self.asar = app / 'Contents' / 'Resources' / 'app.asar'
        self.asar.write_bytes(asar_bytes(files))
        pin = dict(schema=2, mode='interface', app_path=str(app), bundle_id='com.openai.codex', archive_path=str(self.asar))
        (self.home / 'install').mkdir(parents=True, exist_ok=True)
        (self.home / 'install' / 'codex-app-pin.json').write_text(json.dumps(pin))

    def ready_to_send(self):
        self.heartbeat()
        session = str(uuid.uuid4())
        self.ask(session)
        self.hook(self.event('Stop', session, 'turn-1'))
        self.answer(session)
        return session

    def test_interface_pin_sends_after_a_codex_update_with_the_same_interface(self):
        for version in ('26.1103.60933', '27.0.0'):
            self.make_interface_app(version=version)
            session = self.ready_to_send()
            code, out = self.send(session)
            self.assertEqual((code, out['status']), (0, 'sent'), version)

    def test_interface_pin_blocks_when_a_piece_is_missing_or_only_outside_the_build(self):
        for piece in INTERFACE:
            self.make_interface_app(drop=piece)  # La pieza solo queda fuera de .vite/build: no cuenta.
            session = self.ready_to_send()
            code, out = self.send(session)
            self.assertEqual((code, out['result']), (21, 'blocked'), piece)
            state = self.state(session)
            self.assertEqual((state['status'], state['can_retry'], state['sent']), ('error', True, False))
            self.assertIn('interfaz', state['message'])
        self.assertEqual(self.codex.requests, [])

    def test_interface_pin_blocks_another_app_or_an_unreadable_archive(self):
        self.make_interface_app(bundle='com.example.otra')
        session = self.ready_to_send()
        self.assertEqual(self.send(session)[0], 21)
        self.make_interface_app()
        self.asar.write_bytes(b'basura')
        self.assertEqual(self.send(session)[0], 21)
        self.assertEqual(self.codex.requests, [])
        self.make_interface_app()
        self.assertEqual(self.send(session)[0], 0)  # «Reintentar» con la app correcta.

    # --- bloqueos antes del envío ----------------------------------------------------
    def test_wrong_pin_blocks_sending_before_any_connection(self):
        self.heartbeat()
        session = str(uuid.uuid4())
        self.ask(session)
        self.hook(self.event('Stop', session, 'turn-1'))
        answers = self.answer(session)
        data = bytearray(self.asar.read_bytes())
        data[100] ^= 1
        self.asar.write_bytes(bytes(data))
        code, out = self.send(session)
        self.assertEqual((code, out['result']), (21, 'blocked'))
        state = self.state(session)
        self.assertEqual((state['status'], state['can_retry'], state['sent']), ('error', True, False))
        self.assertIn('Codex Desktop cambió', state['message'])
        self.assertEqual(state['answers'], answers)
        self.assertEqual(self.codex.requests, [])
        self.make_app(version='27.0.0')
        self.assertEqual(self.send(session)[0], 21)
        self.make_app()
        code, out = self.send(session)  # «Reintentar» con el pin correcto.
        self.assertEqual((code, out['status']), (0, 'sent'))

    def test_invalid_response_is_rejected_and_kept_aside(self):
        self.heartbeat()
        session = str(uuid.uuid4())
        self.ask(session)
        self.answer(session, answers={qid('call_1', 0): 'solo una'})
        code, out = self.send(session)
        self.assertEqual((code, out['result']), (20, 'rejected'))
        state = self.state(session)
        self.assertEqual((state['status'], state['can_retry']), ('error', True))
        self.assertFalse((self.req(session) / 'response.json').exists())
        self.assertEqual(len(list(self.req(session).glob('response.rejected-*.json'))), 1)

    def test_uncertain_transport_is_never_retried(self):
        self.heartbeat()
        self.codex.drop_start = True
        session = str(uuid.uuid4())
        self.ask(session)
        self.hook(self.event('Stop', session, 'turn-1'))
        answers = self.answer(session)
        code, out = self.send(session)
        self.assertEqual((code, out['result']), (22, 'uncertain'))
        state = self.state(session)
        self.assertEqual((state['status'], state['can_retry'], state['answers']), ('error', False, answers))
        self.assertEqual(self.send(session)[0], 11)
        self.assertEqual(len(self.codex.starts()), 1)

    def test_unpublished_question_is_not_sent(self):
        self.heartbeat()
        session = str(uuid.uuid4())
        questions = [dict(title='¿Sí?', options=['Sí'])]
        self.hook(self.event('PreToolUse', session, tool_input=dict(questions=questions)))
        self.hook(self.event('PostToolUse', session, tool_input=dict(questions=questions),
                             tool_response='{"accepted":false}'))
        state = self.state(session)
        self.assertEqual((state['status'], state['can_retry']), ('error', False))
        self.answer(session, answers={qid('call_1', 0): 'Sí'})
        self.assertEqual(self.send(session)[0], 23)
        self.assertEqual(self.codex.requests, [])

    # --- revisión r1 ---------------------------------------------------------------------
    def hold_lock(self, seconds):
        """Ocupa v1/.lock desde otro hilo durante `seconds` (como otro proceso lento)."""
        (self.home / 'v1').mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.home / 'v1' / '.lock'), os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)

        def release():
            time.sleep(seconds)
            os.close(fd)
        thread = threading.Thread(target=release, daemon=True)
        thread.start()
        self.addCleanup(thread.join, seconds + 2)
        return thread

    def test_i1_busy_session_record_does_not_drop_origin_stop(self):
        self.heartbeat()
        session = str(uuid.uuid4())
        self.ask(session)
        self.hold_lock(2.6)  # El registro de sesión agota sus 2 s; el adaptador consigue el bloqueo después.
        self.hook(self.event('Stop', session, 'turn-1'))
        self.assertTrue(self.state(session)['origin_stopped'])

    def test_i1_lost_origin_stop_is_inferred_from_a_later_turn_but_not_from_subagent_stop(self):
        self.heartbeat()
        session = str(uuid.uuid4())
        self.hook(self.event('UserPromptSubmit', session, 'turn-1', prompt=SECRET_PROMPT))
        self.ask(session)
        # Un SubagentStop con el session_id del padre y el mismo turno no es el Stop de origen.
        self.hook(self.event('SubagentStop', session, 'turn-1', agent_id='a1', agent_type='worker',
                             stop_hook_active=False, last_assistant_message='hijo'))
        self.assertFalse(self.state(session)['origin_stopped'])
        self.hold_lock(4.6)  # Stop de origen perdido: registro y adaptador sin bloqueo.
        self.hook(self.event('Stop', session, 'turn-1'))
        self.assertFalse(self.state(session)['origin_stopped'])
        self.hook(self.event('UserPromptSubmit', session, 'turn-2', prompt=SECRET_PROMPT))
        self.assertTrue(self.state(session)['origin_stopped'])
        self.hook(self.event('Stop', session, 'turn-2'))
        self.answer(session)
        self.assertEqual(self.send(session)[0], 0)
        # Chat inactivo según lo inferido: start v2, no steer a un turno ya terminado.
        self.assertEqual((len(self.codex.starts()), self.codex.steers()), (1, []))
        self.assertEqual(self.state(session)['transport_mode'], 'start')

    def test_i1_stop_of_a_later_turn_also_closes_the_origin(self):
        self.heartbeat()
        session = str(uuid.uuid4())
        self.ask(session, turn='turn-sub')  # p. ej. turno de un subagente que nunca emite Stop.
        self.hook(self.event('Stop', session, 'turn-padre'))
        self.answer(session)
        self.assertEqual(self.send(session)[0], 0)
        self.assertEqual((len(self.codex.starts()), self.codex.steers()), (1, []))

    def test_m1_publication_failure_while_queued_becomes_error(self):
        self.heartbeat()
        session = str(uuid.uuid4())
        questions = [dict(title='¿Sí?', options=['Sí'])]
        self.hook(self.event('PreToolUse', session, tool_input=dict(questions=questions)))
        self.answer(session, answers={qid('call_1', 0): 'Sí'})
        self.assertEqual(self.send(session)[0], 10)
        self.hook(self.event('PostToolUse', session, tool_input=dict(questions=questions),
                             tool_response='{"accepted":false}'))
        state = self.state(session)
        self.assertEqual((state['status'], state['can_retry']), ('error', False))
        self.hook(self.event('Stop', session, 'turn-1'))
        time.sleep(1.5)
        self.assertEqual(self.codex.requests, [])

    def test_m2_invalid_cwd_is_not_started_and_can_be_retried(self):
        self.heartbeat()
        session = str(uuid.uuid4())
        self.ask(session, cwd='relativo/sin-barra')
        self.hook(self.event('Stop', session, 'turn-1', cwd='relativo/sin-barra'))
        self.answer(session)
        code, out = self.send(session)
        self.assertEqual((code, out['result']), (21, 'blocked'))
        state = self.state(session)
        self.assertEqual((state['status'], state['can_retry'], state['transport_started']), ('error', True, False))
        self.assertEqual(self.codex.starts(), [])

    def test_m6_equivalent_item_list_with_other_json_spacing_is_accepted(self):
        self.heartbeat()
        session = str(uuid.uuid4())
        questions = self.ask(session)
        self.hook(self.event('Stop', session, 'turn-1'))
        answers = self.answer(session)
        self.assertEqual(self.send(session)[0], 0)
        items = [dict(questionItemId=qid('call_1', i), question=q['title'], answer=answers[qid('call_1', i)])
                 for i, q in enumerate(questions)]
        other = OPEN + '\n' + json.dumps(items, ensure_ascii=True, indent=1) + '\n' + CLOSE  # Mismos ítems.
        self.assertNotEqual(other, self.envelope('call_1', answers, questions))
        self.hook(self.event('UserPromptSubmit', session, 'r', prompt=other))
        self.hook(self.event('Stop', session, 'r'))
        self.assertEqual(self.state(session)['status'], 'continued')

    def test_m8_uncertain_send_locks_the_conversation_until_it_is_unconfirmed(self):
        self.heartbeat()
        self.codex.drop_start = True
        session = str(uuid.uuid4())
        self.ask(session, call='call_1')
        self.ask(session, call='call_2')
        self.hook(self.event('Stop', session, 'turn-1'))
        self.answer(session, 'call_1')
        self.assertEqual(self.send(session, 'call_1')[0], 22)
        self.codex.drop_start = False
        self.answer(session, 'call_2', {qid('call_2', 0): 'dos', qid('call_2', 1): 'Repetido'})
        code, out = self.send(session, 'call_2')
        self.assertEqual((code, out['status']), (10, 'queued'))
        self.assertEqual(len(self.codex.starts()), 1)
        # Javier escribe otra cosa en el chat: la incierta pasa a «Sin confirmar» y deja de bloquear.
        self.hook(self.event('UserPromptSubmit', session, 'turn-2', prompt=SECRET_PROMPT))
        self.assertEqual(self.state(session, 'call_1')['status'], 'unconfirmed')
        state = self.wait_status(session, 'sent', call='call_2')  # Despachada por el propio evento.
        self.assertEqual(state['transport_mode'], 'steer')

    def test_m4_result_is_derived_from_final_state(self):
        sys.path.insert(0, str(ROOT / 'src'))
        from pancho_bridge import codex as adapter
        from pancho_bridge.codex_ipc import IPCError
        from pancho_bridge.store import Store
        self.heartbeat()
        session = str(uuid.uuid4())
        questions = self.ask(session)
        self.hook(self.event('Stop', session, 'turn-1'))
        answers = self.answer(session)
        envelope = self.envelope('call_1', answers, questions)
        test = self

        class Client:
            def discover_owner(self, thread):
                return OWNER_ID

            def send_reply(self, **kw):
                # Codex registró y terminó el turno, pero el ack se perdió.
                test.hook(test.event('UserPromptSubmit', session, 'r', prompt=envelope))
                test.hook(test.event('Stop', session, 'r'))
                error = IPCError('ack perdido')
                error.outcome_unknown = True
                raise error
        result, state = adapter.send(Store(self.home), key_for(session, 'call_1'), Client)
        self.assertEqual((result, state['status']), ('sent', 'continued'))

    def test_bridge_ipc_module_is_the_verified_poc_module(self):
        if not (ROOT / 'src' / 'codex_ipc.py').exists():
            self.skipTest('Sin la PoC original (no se publica en el repositorio)')
        self.assertEqual((ROOT / 'src' / 'pancho_bridge' / 'codex_ipc.py').read_bytes(),
                         (ROOT / 'src' / 'codex_ipc.py').read_bytes())


if __name__ == '__main__':
    unittest.main()
