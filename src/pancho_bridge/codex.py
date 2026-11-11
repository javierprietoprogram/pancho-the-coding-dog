"""Adaptador Codex Desktop (`request_user_input_async`) para Pancho (T2).

Generaliza la PoC verificada (`src/codex_bridge.py`, `scripts/codex_hook.py`) sin
marcador, sin `CODEX_WAIT`, sin recibo de prueba y en cualquier cwd. Reutiliza:
- el envelope canónico `<send_user_message_question_reply>` y los IDs nativos
  `["request_user_input_async",call_id,i]` en JSON compacto;
- el pin de la app, leído de `<raíz>/install/codex-app-pin.json`. Desde 2026-11-11 (decisión
  de Javier) es por INTERFAZ (`mode: interface`): bundle `com.openai.codex` y, en los `.js` de
  `.vite/build/` del ASAR, todas las piezas de `INTERFACE`; la versión ya no se fija, así que
  una actualización que conserva esas piezas no bloquea. El pin antiguo (versión + sha256 de
  módulos, lógica de `pinned_app`) se sigue entendiendo;
- el transporte IPC existente (`codex_ipc.py`, copia exacta del módulo de la PoC):
  `thread-owner-discovery` y después, según el chat (r2, DECISIONS 2026-11-09):
  * turno activo (el de origen sin su Stop, o un UPS posterior sin su Stop) →
    `thread-follower-steer-turn` v1 (modo `steer`), como hace el submit nativo de Codex;
  * chat inactivo → `thread-follower-start-turn` v2 (modo `start`), ≥ 1 s tras el último Stop.
  Cualquier error tras iniciar el envío, o un steer sin `{turnId}`, es incierto: nunca hay
  fallback de steer a start ni reintento;
- el bloqueo por conversación: con una respuesta en `sending`/`sent` o en envío incierto
  en un chat, la siguiente de ese chat queda «En cola» (`unconfirmed` no bloquea).

Fin del turno de origen: su Stop o, si se perdió, cualquier UPS/Stop raíz posterior de la
misma sesión con otro `turn_id` (los turnos son secuenciales). SubagentStop no se procesa.

Nunca bloquea a Codex: la pregunta ya está publicada en su app, así que los hooks solo
registran y salen. No se lee `transcript_path` ni el historial. Del prompt de
UserPromptSubmit solo se compara (en memoria) con el envelope; nunca se guarda.

Confirmación de uso normal (DECISIONS.md):
- `sent`: ack del IPC (transporte; en steer, `{result:{turnId}}`).
- UserPromptSubmit de la sesión con el envelope exacto (cadena, o lista de ítems idéntica)
  → se anota su `turn_id`.
- Stop de ESE `turn_id` → `accepted` y, en el mismo evento, `continued`.
- Sin UPS: otro prompt en el chat, un Stop de otro turno, SessionEnd o 120 s sin UPS
  desde el ack (evaluado en el siguiente evento Codex de esa sesión o en el siguiente
  `codex-send`) → `unconfirmed`. Con UPS pero sin su Stop: se queda en `sent` hasta un
  Stop de otro turno o SessionEnd → `unconfirmed`. Sin plazo fijo en esa fase: un turno
  puede trabajar mucho tiempo.
- Respuesta nativa (UPS con un envelope para estas preguntas que no es el de Pancho)
  antes de iniciar el envío → `resolved_elsewhere`, también si estaba en cola. El
  PostToolUse de la herramienta async solo prueba la PUBLICACIÓN (`{"accepted":true}`),
  nunca una respuesta.

`PANCHO_CODEX_UPS_SECONDS` acorta los 120 s SOLO para tests (nunca los amplía).
"""
import hashlib
import json
import os
from pathlib import Path
import secrets
import time

from . import procs
from .store import LockBusy, append_jsonl, atomic_json, read_json, request_key, session_key
from .sessions import project_name, valid_cwd

PROVIDER = 'codex'
HOST = 'codex-desktop'
TOOL = 'request_user_input_async'
HOOK_EVENTS = ('SessionStart', 'UserPromptSubmit', 'PreToolUse', 'PostToolUse', 'Stop', 'SessionEnd')
OPEN = '<send_user_message_question_reply>'
CLOSE = '</send_user_message_question_reply>'
ALIVE_MAX_AGE = 6.0
SETTLE_AFTER_STOP = 1.0   # Como la PoC: start v2 al menos 1 s después del Stop.
UPS_SECONDS = 120.0
UPS_ENV = 'PANCHO_CODEX_UPS_SECONDS'
IPC_TIMEOUT = 8
SEND_LOCK_SECONDS = 10.0
PIN_FILE = 'codex-app-pin.json'
BUNDLE_ID = 'com.openai.codex'
# Piezas de Codex Desktop que usa Pancho: los tres métodos IPC, el envelope y la herramienta.
INTERFACE = ('thread-owner-discovery', 'thread-follower-steer-turn', 'thread-follower-start-turn',
             OPEN[1:-1], TOOL)
INTERFACE_PREFIX = '.vite/build/'
ASAR_HEADER_MAX = 64 * 1024 * 1024
TERMINAL = ('accepted', 'continued', 'released', 'expired', 'resolved_elsewhere')
IN_FLIGHT = ('sending', 'sent')

EXIT = dict(sent=0, queued=10, already=11, rejected=20, blocked=21, uncertain=22, not_applicable=23)

MESSAGES = dict(
    pending='Esperando tu respuesta',
    closed='Pancho estaba cerrado; contesta en Codex',
    queued='En cola: espera a que Codex confirme la respuesta anterior de este chat',
    queued_brief='En cola: se enviará en un momento',
    sending='Enviando…',
    sent='Enviada; esperando confirmación de Codex',
    sent_steer='Enviada a Codex mientras trabaja; esperando confirmación',
    reply_seen='Codex recibió el mensaje; esperando a que termine el turno',
    continued='Codex recibió la respuesta y continuó',
    working='Codex recibió la respuesta y sigue trabajando',
    unconfirmed='Sin confirmar; respuesta conservada',
    resolved='Resuelta en Codex',
    publish_failed='Codex no confirmó la publicación de la pregunta; contesta en Codex',
    pin='Codex Desktop cambió su interfaz interna; Pancho no envía nada. Contesta en Codex',
    connect='No se pudo conectar con Codex; no se envió nada',
    uncertain='No se sabe si llegó a Codex; respuesta conservada. No se reenvía',
    invalid='No se puede enviar a este chat (identificador o carpeta no válidos); no se envió nada',
)


class PinError(Exception):
    """La app instalada no coincide con el pin: no se envía nada."""


def ups_seconds():
    value = os.environ.get(UPS_ENV)
    if value:
        try:
            seconds = float(value)
            if 0 < seconds < UPS_SECONDS:
                return seconds
        except ValueError:
            pass
    return UPS_SECONDS


def question_id(call, index):
    return json.dumps(['request_user_input_async', call, index], separators=(',', ':'))


def build_envelope(questions, answers):
    items = [dict(questionItemId=q['id'], question=q['question'], answer=answers[q['id']]) for q in questions]
    return OPEN + '\n' + json.dumps(items, ensure_ascii=False, separators=(',', ':')) + '\n' + CLOSE


def parse_reply_items(text):
    """Lista de ítems de un envelope bien formado (mismas reglas que la PoC) o None."""
    if not isinstance(text, str):
        return None
    text = text.strip()
    if not text.startswith(OPEN + '\n') or not text.endswith('\n' + CLOSE):
        return None
    try:
        items = json.loads(text[len(OPEN) + 1:-len(CLOSE) - 1])
    except ValueError:
        return None
    if not isinstance(items, list) or not items:
        return None
    for item in items:
        if not isinstance(item, dict) or set(item) != {'questionItemId', 'question', 'answer'} \
                or any(not isinstance(v, str) for v in item.values()):
            return None
    return items


def is_our_reply(prompt, envelope):
    """Igualdad exacta de cadena (admite el salto final que añade el IPC) o, como respaldo,
    igualdad exacta de la lista de ítems parseada, como `codex_bridge.py` de la PoC."""
    if not isinstance(prompt, str) or not isinstance(envelope, str):
        return False
    if prompt == envelope or prompt.strip() == envelope:
        return True
    items = parse_reply_items(prompt)
    return items is not None and items == parse_reply_items(envelope)


def parse_reply_ids(text):
    """IDs de un envelope de respuesta bien formado, o None. Solo en memoria."""
    if not isinstance(text, str):
        return None
    text = text.strip()
    if not text.startswith(OPEN + '\n') or not text.endswith('\n' + CLOSE):
        return None
    try:
        items = json.loads(text[len(OPEN) + 1:-len(CLOSE) - 1])
    except ValueError:
        return None
    if not isinstance(items, list) or not items:
        return None
    ids = set()
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get('questionItemId'), str):
            return None
        ids.add(item['questionItemId'])
    return ids


def parse_questions(tool_input, call):
    """Lista normalizada (misma validación que la PoC) o None si no se gestiona."""
    raw = tool_input.get('questions') if isinstance(tool_input, dict) else None
    if not isinstance(raw, list) or not raw:
        return None
    result = []
    for index, q in enumerate(raw):
        if not isinstance(q, dict) or not isinstance(q.get('title'), str) or not q['title'].strip():
            return None
        options = q.get('options', [])
        if options is None:
            options = []
        if not isinstance(options, list) or any(not isinstance(o, str) or not o.strip() for o in options):
            return None
        result.append(dict(id=question_id(call, index), header=None, question=q['title'], multiSelect=False,
                           options=[dict(label=o, description='') for o in options]))
    return result


# --- pin de la app ---------------------------------------------------------------------

def pin_path(store):
    return store.root / 'install' / PIN_FILE


def _asar_entries(handle):
    """(ruta, tamaño, offset absoluto) de los ficheros empaquetados de un ASAR.

    Formato: cuatro uint32 LE [4][S][S-4][L], cabecera JSON de L bytes; los datos empiezan
    en 8 + S. Los ficheros `unpacked` (fuera del archivo) no tienen offset y se ignoran.
    """
    import struct
    prefix = handle.read(16)
    if len(prefix) != 16:
        raise ValueError('ASAR corto')
    first, size, _, length = struct.unpack('<IIII', prefix)
    if first != 4 or not 0 < length <= size - 4 <= ASAR_HEADER_MAX:
        raise ValueError('cabecera ASAR no válida')
    header = json.loads(handle.read(length).decode('utf-8'))
    base = 8 + size
    stack = [('', header)]
    while stack:
        prefix_path, node = stack.pop()
        for name, child in (node.get('files') or {}).items():
            path = prefix_path + name
            if 'files' in child:
                stack.append((path + '/', child))
            elif 'offset' in child and not child.get('unpacked'):
                yield path, int(child['size']), base + int(child['offset'])


def _verify_interface(pin, info):
    if pin.get('bundle_id') != BUNDLE_ID or info.get('CFBundleIdentifier') != BUNDLE_ID:
        raise PinError('no es Codex Desktop')
    missing = {piece.encode('utf-8') for piece in INTERFACE}
    try:
        with open(str(pin['archive_path']), 'rb') as handle:
            for path, size, offset in list(_asar_entries(handle)):
                if not missing:
                    break
                if not (path.startswith(INTERFACE_PREFIX) and path.endswith('.js')):
                    continue
                handle.seek(offset)
                data = handle.read(size)
                missing = {piece for piece in missing if piece not in data}
    except (OSError, ValueError, KeyError, TypeError, AttributeError, UnicodeDecodeError):
        raise PinError('ASAR ilegible') from None
    if missing:
        raise PinError('la interfaz interna de Codex cambió (falta: '
                       + ', '.join(sorted(p.decode('utf-8') for p in missing)) + ')')
    return True


def verify_pin(path):
    """Pin por interfaz (`mode: interface`) o, si es un pin antiguo, la comprobación exacta
    de `pinned_app` de la PoC (versión + sha256 de módulos), con rutas tomadas del pin."""
    import plistlib
    try:
        pin = read_json(path)
        info = plistlib.loads((Path(pin['app_path']) / 'Contents' / 'Info.plist').read_bytes())
    except (OSError, ValueError, KeyError, TypeError, plistlib.InvalidFileException):
        raise PinError('pin o app ilegibles') from None
    if pin.get('mode') == 'interface':
        return _verify_interface(pin, info)
    if info.get('CFBundleIdentifier') != pin.get('bundle_id', 'com.openai.codex') \
            or info.get('CFBundleShortVersionString') != pin.get('expected_app_version'):
        raise PinError('versión de Codex Desktop no validada')
    try:
        with open(str(pin['archive_path']), 'rb') as handle:
            for entry in pin['files']:
                handle.seek(int(pin['data_offset']) + int(entry['offset']))
                if hashlib.sha256(handle.read(int(entry['size']))).hexdigest() != entry['sha256']:
                    raise PinError('la interfaz interna de Codex cambió')
    except (OSError, ValueError, KeyError, TypeError):
        raise PinError('ASAR ilegible') from None
    if not pin['files']:
        raise PinError('pin vacío')
    return True


# --- persistencia ------------------------------------------------------------------------

def load(store, key):
    try:
        state = read_json(store.request_dir(key) / 'state.json')
        return state if isinstance(state, dict) and state.get('provider') == PROVIDER else None
    except (OSError, ValueError):
        return None


def save(store, state, event, **details):
    if state['status'] in TERMINAL:
        state['can_retry'] = False
    directory = store.request_dir(state['key'])
    state['updated_at'] = time.time()
    atomic_json(directory / 'state.json', state)
    entry = dict(at=state['updated_at'], event=event, status=state['status'], generation=state['generation'])
    entry.update(details)
    append_jsonl(directory / 'events.jsonl', entry)
    return state


def _index_path(store, session):
    return store.dir('bridge', 'session-requests') / (session_key(PROVIDER, session) + '.json')


def _index_keys(store, session):
    try:
        keys = read_json(store.v1 / 'bridge' / 'session-requests' / (session_key(PROVIDER, session) + '.json'))
        return [k for k in keys.get('keys', []) if isinstance(k, str)]
    except (OSError, ValueError, AttributeError):
        return []


def _index_add(store, session, key):
    keys = _index_keys(store, session)
    if key not in keys:
        keys.append(key)
    atomic_json(_index_path(store, session), dict(keys=keys))


def _turn_path(store, session):
    return store.dir('bridge', 'codex-turns') / (session_key(PROVIDER, session) + '.json')


def _turns(store, session):
    """Solo IDs y horas: turno activo (UPS sin Stop) y último Stop del chat."""
    try:
        value = read_json(_turn_path(store, session))
        if isinstance(value, dict):
            return value
    except (OSError, ValueError):
        pass
    return dict(active_turn_id=None, last_stop_at=None)


def _session_states(store, session):
    states = []
    for key in _index_keys(store, session):
        state = load(store, key)
        if state is not None:
            states.append(state)
    states.sort(key=lambda s: (s.get('created_at') or 0, s['key']))
    return states


# --- eventos del hook ------------------------------------------------------------------------

def create_request(store, payload, now, simulation=False):
    session, call, turn = payload.get('session_id'), payload.get('tool_use_id'), payload.get('turn_id')
    if payload.get('tool_name') != TOOL:
        return None
    if not all(isinstance(v, str) and v for v in (session, call, turn)):
        return None
    questions = parse_questions(payload.get('tool_input'), call)
    if questions is None:
        return None
    key = request_key(PROVIDER, HOST, session, call)
    store.dir('requests')
    directory = store.request_dir(key)
    cwd = valid_cwd(payload.get('cwd'))
    with store.lock():
        try:
            os.mkdir(str(directory), 0o700)  # Reclamo atómico: la misma llamada no se registra dos veces.
            os.chmod(str(directory), 0o700)
        except FileExistsError:
            return None
        alive = store.app_alive(ALIVE_MAX_AGE, now)
        # Privacidad: con la app cerrada solo metadatos (ni enunciados ni opciones).
        state = dict(schema=1, key=key, provider=PROVIDER, host=HOST, session_id=session, call_id=call,
                     generation=secrets.token_hex(16), cwd=cwd, project=project_name(cwd) if cwd else None,
                     created_at=now, expires_at=None, questions=questions if alive else [],
                     question_count=len(questions), status='pending' if alive else 'released',
                     message=MESSAGES['pending'] if alive else MESSAGES['closed'],
                     sent=False, accepted=False, continued=False, answers=None, can_retry=False,
                     waiter_pid=None, waiter_start=None, simulation=bool(simulation or store.is_simulation()),
                     origin_turn_id=turn, question_published=False, origin_stopped=False,
                     origin_stopped_at=None, transport_started=False, message_id=None, envelope=None,
                     sent_at=None, reply_observed=False, reply_turn_id=None, sender_pid=None,
                     sender_start=None, transport_mode=None, steer_turn_id=None)
        _index_add(store, session, key)
        return save(store, state, 'created', app_alive=alive)


def _published(payload):
    result = payload.get('tool_response')
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except ValueError:
            return False
    return isinstance(result, dict) and result.get('accepted') is True


def post_tool_use(store, payload):
    session, call = payload.get('session_id'), payload.get('tool_use_id')
    if payload.get('tool_name') != TOOL or not isinstance(session, str) or not isinstance(call, str):
        return None
    key = request_key(PROVIDER, HOST, session, call)
    if not (store.request_dir(key) / 'state.json').exists():
        return None
    published = _published(payload)
    with store.lock():
        state = load(store, key)
        if state is None:
            return None
        if published:
            if state['question_published']:
                return None
            state['question_published'] = True
            return save(store, state, 'question_published', note='solo publicación, no respuesta')
        if state['status'] in ('pending', 'queued') and not state['transport_started']:
            state.update(status='error', can_retry=False, message=MESSAGES['publish_failed'])
            return save(store, state, 'question_publication_failed')
        return save(store, state, 'question_publication_failed')


def uncertain(state):
    """Envío iniciado con resultado desconocido (`error` sin reintento tras el inicio)."""
    return state['status'] == 'error' and state.get('transport_started') and not state.get('can_retry')


def blocks_conversation(state):
    """Bloqueo por conversación: en vuelo o incierta. `unconfirmed` no bloquea (r1, M8).
    Una respuesta ya aceptada (Codex la entregó a su modelo) tampoco: la siguiente del chat
    sale enseguida por steer al turno en curso, sin esperar a que termine (r4, 2026-11-11)."""
    return state['status'] in IN_FLIGHT or uncertain(state)


def _origin_done_by_later_turn(store, state, turn, now, event):
    """Los turnos de un chat son secuenciales: un UPS o Stop raíz de OTRO turno, posterior a la
    creación, implica que el turno de origen terminó aunque su Stop se perdiera (r1, I1).
    SubagentStop no se instala ni se procesa, así que nunca cuenta como Stop de origen."""
    if state['status'] in TERMINAL:
        return
    if not state['origin_stopped'] and isinstance(turn, str) and turn and turn != state.get('origin_turn_id'):
        state.update(origin_stopped=True, origin_stopped_at=now)
        save(store, state, 'origin_turn_inferred_stopped', at_event=event, turn_id=turn)


def _unconfirm(store, state, event, **details):
    state.update(status='unconfirmed', message=MESSAGES['unconfirmed'])
    return save(store, state, event, **details)


def _accept_observed(store, state, turn):
    """Codex entregó a su modelo nuestro mensaje exacto (UPS con el envelope): eso ya es la
    aceptación, sin esperar al fin del turno, que puede tardar minutos (r3, 2026-11-11)."""
    state.update(status='accepted', accepted=True, sent=True, can_retry=False, message=MESSAGES['working'])
    return save(store, state, 'accepted', turn_id=turn)


def user_prompt_submit(store, payload, now):
    session, turn, prompt = payload.get('session_id'), payload.get('turn_id'), payload.get('prompt')
    if not isinstance(session, str) or not session:
        return
    with store.lock():
        turns = _turns(store, session)
        turns['active_turn_id'] = turn if isinstance(turn, str) and turn else None
        atomic_json(_turn_path(store, session), turns)
        states = _session_states(store, session)
        if not states:
            return
        ids = parse_reply_ids(prompt)  # En memoria; el prompt nunca se guarda.
        for state in states:
            _origin_done_by_later_turn(store, state, turn, now, 'UserPromptSubmit')
            status = state['status']
            if status == 'accepted' and state.get('reply_observed') and not state.get('continued') \
                    and isinstance(turn, str) and turn and turn != state.get('reply_turn_id'):
                # Empieza otro turno: el de la respuesta terminó aunque su Stop se perdiera.
                state.update(status='continued', continued=True, message=MESSAGES['continued'])
                save(store, state, 'continued', turn_id=turn, at_event='UserPromptSubmit')
                continue
            if status in ('accepted', 'continued', 'resolved_elsewhere'):
                continue
            ours = {question_id(state['call_id'], i) for i in range(state.get('question_count') or 0)}
            if state['transport_started'] and is_our_reply(prompt, state.get('envelope')) \
                    and isinstance(turn, str) and turn:
                state.update(reply_observed=True, reply_turn_id=turn, sent=True)
                save(store, state, 'reply_observed', turn_id=turn)
                if status in ('sent', 'unconfirmed', 'error', 'expired'):
                    _accept_observed(store, state, turn)
            elif ids is not None and ids & ours:
                if not state['transport_started']:
                    state.update(status='resolved_elsewhere', message=MESSAGES['resolved'])
                    save(store, state, 'resolved_elsewhere', turn_id=turn)
                else:
                    save(store, state, 'foreign_reply_observed', turn_id=turn)
            elif (status == 'sent' and not state['reply_observed']) or uncertain(state):
                _unconfirm(store, state, 'unconfirmed_other_prompt', turn_id=turn)


def _blocked(state, states):
    return any(o['key'] != state['key'] and blocks_conversation(o) for o in states)


def _sendable(state, states, turns):
    """Se puede enviar ya (salvo el margen tras un Stop si va por start): en cola, publicada
    y sin otra respuesta del chat en vuelo o incierta (bloqueo por conversación)."""
    if state['status'] != 'queued' or not state['question_published']:
        return False
    return not _blocked(state, states)


def transport_mode(state, turns):
    """r2 (DECISIONS 2026-11-09): con un turno activo en el chat (el de origen sin su Stop, o
    un UPS posterior sin su Stop) → steer v1 al turno en curso; chat inactivo → start v2."""
    if not state['origin_stopped'] or turns.get('active_turn_id'):
        return 'steer'
    return 'start'


def _next_sendable(store, session):
    states = _session_states(store, session)
    turns = _turns(store, session)
    for state in states:
        if _sendable(state, states, turns):
            return state, turns
    return None, turns


def stop(store, payload, now):
    """Registra el fin de turno; devuelve las claves en cola que ya se pueden enviar."""
    session, turn = payload.get('session_id'), payload.get('turn_id')
    if not isinstance(session, str) or not session:
        return []
    with store.lock():
        turns = _turns(store, session)
        turns.update(active_turn_id=None, last_stop_at=now)
        atomic_json(_turn_path(store, session), turns)
        for state in _session_states(store, session):
            if isinstance(turn, str) and state.get('origin_turn_id') == turn and not state['origin_stopped']:
                state.update(origin_stopped=True, origin_stopped_at=now)
                save(store, state, 'origin_turn_stopped', turn_id=turn)
            else:
                _origin_done_by_later_turn(store, state, turn, now, 'Stop')
            if state['status'] in ('continued', 'resolved_elsewhere', 'released'):
                continue
            if state['status'] == 'accepted':
                if state['reply_observed']:  # Aceptada al ver el mensaje; el turno ya terminó.
                    state.update(status='continued', continued=True, message=MESSAGES['continued'])
                    save(store, state, 'continued', turn_id=turn)
                continue
            if state['reply_observed'] and state['reply_turn_id'] == turn and state['transport_started']:
                state.update(status='accepted', accepted=True, sent=True, message=MESSAGES['continued'])
                save(store, state, 'accepted', turn_id=turn)
                state.update(status='continued', continued=True)
                save(store, state, 'continued', turn_id=turn)
            elif state['status'] == 'sent' or uncertain(state):
                _unconfirm(store, state, 'unconfirmed_at_stop', turn_id=turn)
        return []


def session_end(store, payload):
    session = payload.get('session_id')
    if not isinstance(session, str) or not session:
        return
    with store.lock():
        turns = _turns(store, session)
        turns['active_turn_id'] = None
        atomic_json(_turn_path(store, session), turns)
        for state in _session_states(store, session):
            if state['status'] == 'sent' or uncertain(state):
                _unconfirm(store, state, 'unconfirmed_session_end')


def sweep(store, session, now):
    """Plazo sin UPS tras el ack y emisores muertos a mitad de envío."""
    if not isinstance(session, str) or not session:
        return
    states = _session_states(store, session)
    limit = ups_seconds()
    late = [s for s in states if ((s['status'] == 'sent' and not s['reply_observed']) or uncertain(s))
            and (s.get('sent_at') or s.get('transport_started_at') or now) + limit < now]
    lost = [s for s in states if s['status'] == 'sending'
            and not procs.same_process(s.get('sender_pid'), s.get('sender_start'))]
    if not late and not lost:
        return
    with store.lock():
        for state in late + lost:
            current = load(store, state['key'])
            if current is None:
                continue
            if (current['status'] == 'sent' and not current['reply_observed']) or uncertain(current):
                _unconfirm(store, current, 'unconfirmed_no_prompt', seconds=limit)
            elif current['status'] == 'sending' and not procs.same_process(
                    current.get('sender_pid'), current.get('sender_start')):
                current.update(status='error', can_retry=False, message=MESSAGES['uncertain'])
                save(store, current, 'sender_lost')


def handle(store, payload, now):
    """Procesa un evento del hook. Devuelve las claves que hay que despachar (Stop)."""
    name = payload.get('hook_event_name')
    session = payload.get('session_id')
    dispatch = []
    if name == 'PreToolUse':
        create_request(store, payload, now)
    elif name == 'PostToolUse':
        post_tool_use(store, payload)
    elif name == 'UserPromptSubmit':
        user_prompt_submit(store, payload, now)
    elif name == 'Stop':
        dispatch = stop(store, payload, now)
    elif name == 'SessionEnd':
        session_end(store, payload)
    sweep(store, session, now)
    if isinstance(session, str) and session:
        # Tras cualquier evento: si una respuesta del chat quedó en cola y ya puede salir, se despacha.
        candidate, _ = _next_sendable(store, session)
        if candidate is not None:
            dispatch = [candidate['key']]
    return dispatch


def _new_message_id():
    import uuid
    return str(uuid.uuid4())


# --- codex-send ----------------------------------------------------------------------------------

def validate_response(response, state):
    if not isinstance(response, dict):
        return 'no es un objeto JSON'
    expected = (state['key'], PROVIDER, state['session_id'], state['call_id'], state['generation'])
    got = tuple(response.get(k) for k in ('key', 'provider', 'session_id', 'call_id', 'generation'))
    if got != expected:
        return 'no corresponde a esta pregunta'
    answers = response.get('answers')
    if not isinstance(answers, dict) or set(answers) != {q['id'] for q in state['questions']}:
        return 'no responde exactamente a todas las preguntas'
    if any(not isinstance(v, str) or not v.strip() for v in answers.values()):
        return 'hay respuestas vacías'
    return None


class _Locked:
    """Bloqueo global con reintentos (codex-send no es un hook: puede esperar un poco)."""

    def __init__(self, store, seconds=SEND_LOCK_SECONDS):
        self.store, self.deadline = store, time.monotonic() + seconds

    def __enter__(self):
        while True:
            try:
                self.context = self.store.lock()
                return self.context.__enter__()
            except LockBusy:
                if time.monotonic() >= self.deadline:
                    raise

    def __exit__(self, *exc):
        return self.context.__exit__(*exc)


def _record_response(store, key):
    """pending/error(can_retry) + response.json → queued. Devuelve (resultado|None, estado)."""
    with _Locked(store):
        state = load(store, key)
        if state is None:
            return 'not_applicable', None
        status = state['status']
        if status in ('accepted', 'continued'):
            return 'already', state
        if status in TERMINAL:
            return 'not_applicable', state
        if status in IN_FLIGHT or status == 'unconfirmed' or state['transport_started']:
            return 'already', state
        if status == 'queued':
            return None, state
        if status == 'error' and not state['can_retry']:
            return 'not_applicable', state
        directory = store.request_dir(key)
        try:
            response = read_json(directory / 'response.json')
        except FileNotFoundError:
            return 'rejected', state
        except (OSError, ValueError):
            response = None
        reason = validate_response(response, state) if response is not None else 'JSON ilegible'
        if reason is not None:
            stamp = time.strftime('%Y%m%dT%H%M%S') + '-' + secrets.token_hex(3)
            try:
                os.replace(str(directory / 'response.json'),
                           str(directory / ('response.rejected-' + stamp + '.json')))
            except FileNotFoundError:
                pass
            state.update(status='error', can_retry=True, sent=False,
                         message='Respuesta no válida (' + reason + '); no se envió. Corrígela y vuelve a enviar')
            save(store, state, 'response_rejected')
            return 'rejected', state
        answers = dict(response['answers'])
        blocked = _blocked(state, _session_states(store, state['session_id']))
        state.update(status='queued', message=MESSAGES['queued' if blocked else 'queued_brief'], answers=answers,
                     can_retry=False,
                     envelope=build_envelope(state['questions'], answers),
                     message_id=state.get('message_id') or _new_message_id())
        save(store, state, 'response_recorded')
        return None, state


def _fail_before_send(store, key, message, event, **details):
    with _Locked(store):
        state = load(store, key)
        if state is not None and state['status'] == 'queued':
            state.update(status='error', can_retry=True, sent=False, message=message)
            save(store, state, event, **details)


def _check_send_inputs(state, session):
    """Validación local previa al envío (las mismas reglas que `codex_ipc`): un fallo aquí
    no inicia nada y se puede reintentar (r1, M2)."""
    from .codex_ipc import _native_reply, _uuid
    _uuid(session, 'thread ID')
    _uuid(state.get('message_id'), 'message ID')
    cwd = state.get('cwd')
    if not isinstance(cwd, str) or not cwd.startswith('/') or '\0' in cwd:
        raise ValueError('cwd no absoluto')
    _native_reply(state.get('envelope'))


def _settle_wait(candidate, turns):
    """Segundos que faltan para poder usar start (≥ 1 s tras el último Stop, como la PoC)."""
    settle = max(candidate.get('origin_stopped_at') or 0, turns.get('last_stop_at') or 0) + SETTLE_AFTER_STOP
    return settle - time.time()


def _dispatch_one(store, session, ipc_factory, pin, sleep):
    """Envía como mucho UNA respuesta en cola de este chat, por steer o start según el turno.

    Devuelve (clave elegida o None, True si ESTE proceso inició su transporte).
    """
    candidate, turns = _next_sendable(store, session)
    if candidate is None:
        return None, False
    if transport_mode(candidate, turns) == 'start':
        wait = _settle_wait(candidate, turns)
        if wait > 0:
            sleep(min(wait, SETTLE_AFTER_STOP + 0.5))
    key = candidate['key']
    try:
        _check_send_inputs(candidate, session)
    except (ValueError, TypeError) as exc:
        _fail_before_send(store, key, MESSAGES['invalid'], 'send_inputs_invalid', error=type(exc).__name__)
        return key, False
    try:
        verify_pin(pin)
    except PinError as exc:
        _fail_before_send(store, key, MESSAGES['pin'], 'pin_blocked', reason=str(exc))
        return key, False
    try:
        client = ipc_factory()
        owner = client.discover_owner(session)
    except Exception as exc:  # Nada enviado: se puede reintentar.
        _fail_before_send(store, key, MESSAGES['connect'], 'owner_discovery_failed', error=type(exc).__name__)
        return key, False
    with _Locked(store):
        states = _session_states(store, session)
        turns = _turns(store, session)
        state = next((s for s in states if s['key'] == key), None)
        if state is None or not _sendable(state, states, turns):
            return key, False  # Algo cambió (respuesta nativa, otra respuesta en vuelo): no se envía.
        mode = transport_mode(state, turns)  # Decisión final, bajo el bloqueo.
        if mode == 'start' and _settle_wait(state, turns) > 0:
            return key, False  # Un Stop recién llegado: su propio evento lo despachará.
        me = procs.identity(os.getpid())
        state.update(status='sending', transport_started=True, transport_started_at=time.time(),
                     transport_mode=mode, message=MESSAGES['sending'], sender_pid=me['pid'],
                     sender_start=me['start'], can_retry=False)
        save(store, state, 'transport_started', message_id=state['message_id'], mode=mode)
    from .codex_ipc import IPCError
    try:
        result = client.send_reply(thread_id=session, cwd=state['cwd'], envelope=state['envelope'],
                                   message_id=state['message_id'], owner_id=owner, mode=mode)
    except Exception as exc:
        # Sin forma acreditada de «turno no activo» en el contrato IPC inspeccionado: cualquier
        # error tras iniciar un steer es incierto. Nunca hay fallback a start ni reintento.
        if isinstance(exc, IPCError):
            unknown = bool(exc.outcome_unknown)
        else:  # Validación local (ValueError/TypeError) = no iniciado; cualquier otra cosa, incierta.
            unknown = not isinstance(exc, (ValueError, TypeError))
        with _Locked(store):
            current = load(store, key)
            if current is not None and current['status'] == 'sending':
                if unknown:
                    current.update(status='error', can_retry=False, message=MESSAGES['uncertain'])
                    save(store, current, 'transport_uncertain', error=type(exc).__name__,
                         remote=getattr(exc, 'remote_code', None), mode=mode)
                else:
                    current.update(status='error', can_retry=True, transport_started=False,
                                   message=MESSAGES['connect'])
                    save(store, current, 'transport_not_started', error=type(exc).__name__, mode=mode)
        return key, True
    steer_turn = None
    if mode == 'steer':
        body = result.get('result') if isinstance(result, dict) else None
        steer_turn = body.get('turnId') if isinstance(body, dict) else None
    with _Locked(store):
        current = load(store, key)
        if current is not None and current['status'] == 'sending':
            if mode == 'steer' and (not isinstance(steer_turn, str) or not steer_turn):
                # Éxito sin {turnId} (visto en real el 2026-11-11: el mensaje sí llegó). Sigue sin
                # contar como entrega y no hay fallback ni reintento, pero ya no se enseña un
                # error: queda «enviada» a la espera del UPS, y sin él pasa a «sin confirmar».
                # Solo se anotan los nombres de los campos devueltos, nunca su contenido.
                save(store, current, 'transport_ambiguous', mode=mode,
                     result_keys=sorted(str(k) for k in result)[:12] if isinstance(result, dict) else None,
                     body_keys=sorted(str(k) for k in body)[:12] if isinstance(body, dict) else None)
                steer_turn = None
            current.update(status='sent', sent=True, sent_at=time.time(), steer_turn_id=steer_turn,
                           message=MESSAGES['sent_steer' if mode == 'steer' else 'sent'])
            save(store, current, 'sent', mode=mode, steer_turn_id=steer_turn,
                 resultType=result.get('resultType') if isinstance(result, dict) else None,
                 handledByClientId=result.get('handledByClientId') if isinstance(result, dict) else None)
            if current['reply_observed']:  # El UPS llegó antes que el ack del IPC.
                _accept_observed(store, current, current.get('reply_turn_id'))
    return key, True


def outcome(state, mine):
    """Resultado de `codex-send` derivado del estado FINAL de la solicitud (r1, M4).

    `mine`: este proceso inició el transporte de esta misma solicitud.
    """
    if state is None:
        return 'not_applicable'
    status = state['status']
    if status in ('sent', 'accepted', 'continued', 'unconfirmed'):
        return 'sent' if mine else 'already'
    if status == 'sending':
        return 'already'
    if status == 'queued':
        return 'queued'
    if status == 'error':
        if state.get('transport_started') and not state.get('can_retry'):
            return 'uncertain' if mine else 'already'
        if state.get('can_retry'):
            return 'blocked'
        return 'not_applicable'
    return 'not_applicable'  # released, resolved_elsewhere, pending sin respuesta…


def send(store, key, ipc_factory, sleep=time.sleep):
    """`codex-send --key K`: registra la respuesta y envía lo que toque de ese chat.

    Devuelve (resultado, estado final de K). Resultados: ver EXIT.
    """
    early, state = _record_response(store, key)
    if early is not None:
        return early, state
    session = state['session_id']
    sweep(store, session, time.time())
    chosen, started = _dispatch_one(store, session, ipc_factory, pin_path(store), sleep)
    state = load(store, key)
    mine = chosen == key and started
    return outcome(state, mine), state
