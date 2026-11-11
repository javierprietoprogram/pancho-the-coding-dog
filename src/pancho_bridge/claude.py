"""Adaptador Claude Code (Claude Desktop, pestaña Code) para `AskUserQuestion`.

Generaliza la PoC verificada (`src/bridge.py`, `scripts/claude_hook.py`): cualquier
cwd, sin marcador, sin límite de llamadas. El retorno es el mismo protocolo
verificado: `hookSpecificOutput` PreToolUse `allow` con el `updatedInput`
original más `answers`.

Política de espera (R7, DECISIONS.md y «Revisión 1 del contrato»):
- App no viva al llegar la pregunta (latido > 6 s o pid inexistente): `released`, sin
  stdout; solo se guardan metadatos (n.º de preguntas, ids), ni textos ni input.json.
- App viva: se espera hasta `expires_at` (1800 s) sondeando cada 0,15 s, con
  `waiter_pid`/`waiter_start` del hook en state.json.
  `response.json` válida → stdout y `sent`; `release.json` → `released`;
  pid de la app muerto, o latido parado > 60 s con el pid vivo → `released`
  (la cuenta se reinicia tras un salto de reloj); plazo agotado → `expired`.
- Claude dejó de esperar → `expired` («Claude dejó de esperar; no se entregó»): falla
  el emit, se cierra el lector de stdout, desaparece el padre (o el abuelo si el padre
  es el `sh` del `|| true`), o el hook recibe SIGTERM/SIGHUP.
- Respuesta inválida: se renombra a `response.rejected-<sello>.json`, el estado pasa a
  `error` con `can_retry=true` (nada se envió) y se sigue esperando otra respuesta.
- Estados terminales: siempre `can_retry=false` y sin `waiter_pid`.

Avisos de permiso (`PermissionRequest`, DECISIONS.md 2026-11-11): misma espera y mismos
archivos, con `kind='permission'` y una única pregunta sintética (Permitir una vez /
Permitir siempre / Rechazar). El retorno es `hookSpecificOutput.decision` con
`behavior` allow|deny; «siempre» devuelve en `updatedPermissions` exactamente las
`permission_suggestions` recibidas. El evento no trae `tool_use_id`: la identidad es
propia y no hay confirmación observable, así que tras el emit queda `accepted` con el
texto «Decisión enviada a Claude». Si el turno termina con el aviso aún pendiente, se
resolvió en Claude (`resolved_elsewhere`). Sin decisión, Claude muestra su aviso normal.

`PANCHO_CLAUDE_WAIT_SECONDS` y `PANCHO_HEARTBEAT_STALL_SECONDS` acortan plazos SOLO
para tests (nunca los amplían).
"""
import copy
import hashlib
import json
import os
import re
import secrets
import signal
import time

from . import procs
from .store import LockBusy, atomic_json, append_jsonl, pid_alive, read_json, request_key, session_key
from .sessions import project_name, valid_cwd

PROVIDER = 'claude'
HOST = 'claude-code'
TOOL = 'AskUserQuestion'
WAIT_SECONDS = 1800.0
WAIT_ENV = 'PANCHO_CLAUDE_WAIT_SECONDS'
PREFS_MIN_WAIT = 60.0
STALL_SECONDS = 60.0
STALL_ENV = 'PANCHO_HEARTBEAT_STALL_SECONDS'
CLOCK_JUMP = 5.0
POLL_SECONDS = 0.15
ALIVE_MAX_AGE = 6.0
STEP_LOCK_SECONDS = 0.5   # Dentro de la espera: si el bloqueo está ocupado, se reintenta.
SETTLE_SECONDS = 10.0     # Tras el emit: el resultado se registra con más paciencia.
WAITING = ('pending', 'error')
WAITER_STATES = ('pending', 'error', 'sending')
TERMINAL = ('accepted', 'continued', 'released', 'expired', 'resolved_elsewhere')
PERMISSION_EVENT = 'PermissionRequest'
PERMISSION_KIND = 'permission'
PERMISSION_ID = 'permission'
PERMISSION_SKIP = (TOOL, 'ExitPlanMode')  # Tienen su propia interfaz; no son un sí/no.
ALLOW_ONCE, ALLOW_ALWAYS, DENY = 'Permitir una vez', 'Permitir siempre', 'Rechazar'
DETAIL_MAX = 4000
TURN_END_EVENTS = ('Stop', 'StopFailure', 'SessionEnd')
# Recuadro «siguiente paso» de la app de Claude (herramienta wrap_up). Experimental: la pregunta
# se anota al llamarse la herramienta y se espera la respuesta en el Stop de ese turno; si se
# contesta en Pancho, el Stop devuelve decision=block y Claude sigue con esa respuesta.
NEXT_TOOL = 'mcp__ccd_turn__wrap_up'
NEXT_KIND = 'next_step'
NEXT_ID = 'next_step'
NEXT_MAX_AGE = 900.0      # Una nota más vieja no es de este final de turno.
NEXT_TEXT_MAX = 2000
DESTINATIONS = dict(session='esta sesión', localSettings='este proyecto (solo en este Mac)',
                    projectSettings='este proyecto', userSettings='todos tus proyectos')
SWEEP_EVENTS = ('SessionStart', 'UserPromptSubmit', 'Stop', 'StopFailure', 'SessionEnd')

MESSAGES = dict(
    pending='Esperando tu respuesta',
    closed='Pancho estaba cerrado; contesta en Claude',
    release='Liberada: contesta en Claude',
    lost='Pancho se cerró; la pregunta vuelve a Claude',
    expired='Tiempo agotado; contesta en Claude. No se envió nada',
    orphan='Claude dejó de esperar; no se entregó',
    sending='Enviando…',
    sent='Enviada; esperando confirmación de Claude',
    accepted='Claude recibió la respuesta',
    continued='Claude recibió la respuesta y continuó',
    stop_failed='Claude recibió la respuesta, pero el turno falló',
    unconfirmed='Sin confirmar; respuesta conservada',
    resolved='Respondida en Claude',
    decided='Decisión enviada a Claude',
    perm_resolved='Resuelto en Claude',
    perm_denied='Rechazado desde Pancho',
    next_sent='Respuesta enviada a Claude',
    next_resolved='Respondida en Claude',
)


def _shortened(env, default):
    value = os.environ.get(env)
    if value:
        try:
            seconds = float(value)
            if 0 < seconds < default:
                return seconds
        except ValueError:
            pass
    return default


def wait_seconds(store=None):
    """Plazo de espera. La app puede acortarlo (nunca alargarlo) en `v1/app/prefs.json`."""
    base = WAIT_SECONDS
    if store is not None:
        try:
            value = read_json(store.v1 / 'app' / 'prefs.json').get('wait_seconds')
            if isinstance(value, (int, float)) and not isinstance(value, bool) \
                    and PREFS_MIN_WAIT <= value < WAIT_SECONDS:
                base = float(value)
        except (OSError, ValueError, AttributeError):
            pass
    return _shortened(WAIT_ENV, base)


def stall_seconds():
    return _shortened(STALL_ENV, STALL_SECONDS)


# --- persistencia -----------------------------------------------------------

def load(store, key):
    try:
        state = read_json(store.request_dir(key) / 'state.json')
        return state if isinstance(state, dict) else None
    except (OSError, ValueError):
        return None


def save(store, state, event, **details):
    """Escribe state.json y una línea de events.jsonl, con las invariantes del contrato."""
    if state['status'] in TERMINAL:
        state['can_retry'] = False
    if state['status'] not in WAITER_STATES:
        state['waiter_pid'] = None
        state['waiter_start'] = None
    directory = store.request_dir(state['key'])
    state['updated_at'] = time.time()
    atomic_json(directory / 'state.json', state)
    entry = dict(at=state['updated_at'], event=event, status=state['status'],
                 generation=state['generation'])
    entry.update(details)
    append_jsonl(directory / 'events.jsonl', entry)
    return state


def _index_path(store, session):
    return store.dir('bridge', 'session-requests') / (session_key(PROVIDER, session) + '.json')


def _index_add(store, session, key):
    path = _index_path(store, session)
    try:
        keys = read_json(path).get('keys', [])
    except (OSError, ValueError, AttributeError):
        keys = []
    if key not in keys:
        keys.append(key)
    atomic_json(path, dict(keys=keys))


def _index_keys(store, session):
    path = store.v1 / 'bridge' / 'session-requests' / (session_key(PROVIDER, session) + '.json')
    try:
        keys = read_json(path).get('keys', [])
        return [k for k in keys if isinstance(k, str)]
    except (OSError, ValueError, AttributeError):
        return []


# --- preguntas ---------------------------------------------------------------

def parse_questions(tool_input):
    """Devuelve la lista normalizada o None si no se puede gestionar sin ambigüedad."""
    if not isinstance(tool_input, dict):
        return None
    questions = tool_input.get('questions')
    if not isinstance(questions, list) or not questions:
        return None
    result = []
    for q in questions:
        if not isinstance(q, dict):
            return None
        text, options = q.get('question'), q.get('options')
        if not isinstance(text, str) or not text or not isinstance(options, list):
            return None
        header = q.get('header')
        parsed_options = []
        for option in options:
            if not isinstance(option, dict) or not isinstance(option.get('label'), str):
                return None
            description = option.get('description', '')
            if not isinstance(description, str):
                return None
            parsed_options.append(dict(label=option['label'], description=description))
        result.append(dict(id=text, header=header if isinstance(header, str) else None,
                           question=text, multiSelect=q.get('multiSelect') is True,
                           options=parsed_options))
    # El mapa `answers` usa el enunciado como clave: deben ser únicos (como en la PoC).
    if len({q['id'] for q in result}) != len(result):
        return None
    return result


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


# --- PreToolUse ----------------------------------------------------------------

def create_request(store, payload, now, check_app=True, simulation=False, waiter=None):
    """Crea la solicitud. Devuelve el estado creado o None si no se intercepta."""
    session, call = payload.get('session_id'), payload.get('tool_use_id')
    if not isinstance(session, str) or not session or not isinstance(call, str) or not call:
        return None
    if payload.get('tool_name') != TOOL:
        return None
    tool_input = payload.get('tool_input')
    questions = parse_questions(tool_input)
    if questions is None:
        return None
    return _claim(store, payload, session, call, questions, tool_input, now, check_app,
                  simulation, waiter)


def _claim(store, payload, session, call, questions, stored_input, now, check_app, simulation,
           waiter, **extra):
    tool_input = stored_input
    key = request_key(PROVIDER, HOST, session, call)
    store.dir('requests')
    directory = store.request_dir(key)
    cwd = valid_cwd(payload.get('cwd'))
    waiter = waiter or procs.identity(os.getpid())
    with store.lock():
        try:
            os.mkdir(str(directory), 0o700)  # Reclamo atómico de la identidad.
            os.chmod(str(directory), 0o700)
        except FileExistsError:
            return None  # Repetición de la misma llamada: no se intercepta dos veces.
        alive = store.app_alive(ALIVE_MAX_AGE, now) if check_app else True
        if alive:
            atomic_json(directory / 'input.json', tool_input)
        # Privacidad: con la app cerrada solo quedan metadatos (ni textos ni input.json).
        state = dict(schema=1, key=key, provider=PROVIDER, host=HOST, session_id=session,
                     call_id=call, generation=secrets.token_hex(16), cwd=cwd,
                     project=project_name(cwd) if cwd else None, created_at=now,
                     expires_at=(now + wait_seconds(store)) if alive else None,
                     questions=questions if alive else [], question_count=len(questions),
                     status='pending' if alive else 'released',
                     message=MESSAGES['pending'] if alive else MESSAGES['closed'],
                     sent=False, accepted=False, continued=False, answers=None,
                     can_retry=False, waiter_pid=waiter['pid'] if alive else None,
                     waiter_start=waiter['start'] if alive else None,
                     simulation=bool(simulation or store.is_simulation()))
        state.update(extra)
        _index_add(store, session, key)
        return save(store, state, 'created', app_alive=alive)


# --- avisos de permiso ---------------------------------------------------------------

def _permission_detail(tool, tool_input):
    detail = None
    if isinstance(tool_input, dict):
        if tool == 'Bash' and isinstance(tool_input.get('command'), str):
            detail = tool_input['command']
        elif isinstance(tool_input.get('file_path'), str):
            detail = tool_input['file_path']
    if detail is None:
        detail = json.dumps(tool_input, ensure_ascii=False, indent=2, sort_keys=True)
    if len(detail) > DETAIL_MAX:
        detail = detail[:DETAIL_MAX] + '\n… (recortado: míralo entero en Claude antes de permitir)'
    return detail


def describe_suggestions(suggestions):
    """Texto de lo que aplicaría «Permitir siempre», o None si no se puede ofrecer."""
    if not isinstance(suggestions, list) or not suggestions:
        return None
    parts = []
    for item in suggestions:
        if not isinstance(item, dict):
            return None
        kind = item.get('type')
        where = DESTINATIONS.get(item.get('destination'), str(item.get('destination')))
        if kind in ('addRules', 'replaceRules', 'removeRules') and isinstance(item.get('rules'), list):
            rules = []
            for rule in item['rules']:
                if not isinstance(rule, dict) or not isinstance(rule.get('toolName'), str):
                    return None
                content = rule.get('ruleContent')
                rules.append(rule['toolName'] + ('(' + content + ')' if isinstance(content, str) and content else ''))
            verb = dict(addRules='Añade', replaceRules='Sustituye', removeRules='Quita')[kind]
            parts.append('%s la regla «%s» %s en %s' % (verb, str(item.get('behavior')), ', '.join(rules), where))
        elif kind == 'setMode':
            parts.append('Cambia el modo de permisos a «%s» en %s' % (item.get('mode'), where))
        elif kind in ('addDirectories', 'removeDirectories') and isinstance(item.get('directories'), list):
            parts.append('%s carpetas de trabajo (%s) en %s' % (
                'Añade' if kind == 'addDirectories' else 'Quita',
                ', '.join(str(d) for d in item['directories']), where))
        else:
            parts.append('Aplica: ' + json.dumps(item, ensure_ascii=False, sort_keys=True))
    return '. '.join(parts)


SIMPLE_COMMAND = re.compile(r"^[A-Za-z0-9_./@:=,+%~ -]+$")
SUBCOMMAND = re.compile(r'^[a-z][a-z0-9-]*$')


def default_suggestions(tool, tool_input):
    """Regla propia para «Permitir siempre» cuando Claude no sugiere ninguna (visto en Claude
    Desktop: `permission_suggestions: []`). Siempre se muestra entera en la tarjeta."""
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    if tool in ('Edit', 'Write', 'MultiEdit', 'NotebookEdit'):
        return [dict(type='setMode', mode='acceptEdits', destination='session')]
    rule = dict(toolName=tool)
    command, url = tool_input.get('command'), tool_input.get('url')
    if tool == 'Bash' and isinstance(command, str) and command.strip():
        words = command.split()
        if SIMPLE_COMMAND.match(command) and '..' not in command:
            # Orden sencilla: prefijo (programa y, si lo hay, subcomando). Si no, la orden exacta.
            prefix = words[:2] if len(words) > 1 and SUBCOMMAND.match(words[1]) else words[:1]
            rule['ruleContent'] = ' '.join(prefix) + ':*'
        else:
            rule['ruleContent'] = command
    elif tool == 'WebFetch' and isinstance(url, str):
        host = url.split('://', 1)[-1].split('/', 1)[0].split('@')[-1].split(':')[0]
        if host:
            rule['ruleContent'] = 'domain:' + host
    return [dict(type='addRules', rules=[rule], behavior='allow', destination='localSettings')]


def permission_questions(tool, tool_input, suggestions):
    always = describe_suggestions(suggestions)
    options = [dict(label=ALLOW_ONCE, description='Solo esta vez.')]
    if always is not None:
        options.append(dict(label=ALLOW_ALWAYS, description=always + '.'))
    options.append(dict(label=DENY, description='Claude no lo ejecuta. Si escribes un texto en vez de '
                                                'elegir, se rechaza y Claude recibe ese texto.'))
    text = 'Claude pide permiso para usar %s:\n%s' % (tool, _permission_detail(tool, tool_input))
    return [dict(id=PERMISSION_ID, header='Permiso', question=text, multiSelect=False, options=options)]


def create_permission(store, payload, now, check_app=True, simulation=False, waiter=None):
    session, tool = payload.get('session_id'), payload.get('tool_name')
    if not isinstance(session, str) or not session or not isinstance(tool, str) or not tool \
            or tool in PERMISSION_SKIP or payload.get('hook_event_name') != PERMISSION_EVENT:
        return None
    tool_input = payload.get('tool_input')
    suggestions = payload.get('permission_suggestions')
    own = not isinstance(suggestions, list) or not suggestions
    if own:
        suggestions = default_suggestions(tool, tool_input)
    questions = permission_questions(tool, tool_input, suggestions)
    stored = dict(tool_name=tool, tool_input=tool_input, permission_suggestions=suggestions)
    call = 'perm-' + secrets.token_hex(12)  # El evento no trae tool_use_id.
    return _claim(store, payload, session, call, questions, stored, now, check_app, simulation,
                  waiter, kind=PERMISSION_KIND, tool=tool,
                  always_rule='pancho' if own else 'claude')


def permission_output(original, answer):
    """(salida del hook, motivo de rechazo). Texto libre = rechazar con ese mensaje."""
    choice = answer.strip()
    if choice == ALLOW_ONCE:
        decision = dict(behavior='allow')
    elif choice == ALLOW_ALWAYS:
        suggestions = original.get('permission_suggestions') if isinstance(original, dict) else None
        if describe_suggestions(suggestions) is None:
            return None, 'Claude no ofreció «Permitir siempre» para este aviso'
        decision = dict(behavior='allow', updatedPermissions=copy.deepcopy(suggestions))
    elif choice == DENY:
        decision = dict(behavior='deny', message=MESSAGES['perm_denied'])
    else:
        decision = dict(behavior='deny', message=answer)
    return dict(hookSpecificOutput=dict(hookEventName=PERMISSION_EVENT, decision=decision)), None


# --- recuadro «siguiente paso» -----------------------------------------------------

def _next_note(store, session):
    return store.v1 / 'next-step' / (hashlib.sha256(session.encode('utf-8')).hexdigest()[:32] + '.json')


def _drop_next_note(store, session):
    try:
        os.unlink(str(_next_note(store, session)))
    except OSError:
        pass


def parse_next_step(tool_input):
    """(pregunta, [etiquetas de respuesta]) o None. Los botones de acción de la app no cuentan."""
    if not isinstance(tool_input, dict):
        return None
    ask, actions = tool_input.get('ask'), tool_input.get('actions')
    if isinstance(actions, str):  # Algunos clientes mandan la lista como texto JSON.
        try:
            actions = json.loads(actions)
        except ValueError:
            return None
    if not isinstance(ask, str) or not ask.strip() or len(ask) > NEXT_TEXT_MAX or not isinstance(actions, list):
        return None
    labels = []
    for action in actions:
        if not isinstance(action, dict) or action.get('action'):
            continue
        label = action.get('label')
        if isinstance(label, str) and label.strip() and len(label) <= 200 and label not in labels:
            labels.append(label)
    return (ask, labels) if labels else None


def note_next_step(store, payload, now):
    """PreToolUse de wrap_up: nunca espera ni escribe en stdout; solo deja la nota del turno."""
    session = payload.get('session_id')
    if not isinstance(session, str) or not session:
        return
    parsed = parse_next_step(payload.get('tool_input'))
    if parsed is None or not store.app_alive(ALIVE_MAX_AGE, now):
        _drop_next_note(store, session)  # Privacidad: con Pancho cerrado no se guarda el texto.
        return
    call = payload.get('tool_use_id')
    store.dir('next-step')
    atomic_json(_next_note(store, session),
                dict(session_id=session, call_id=call if isinstance(call, str) and call else None,
                     ask=parsed[0], labels=parsed[1], at=now))


def create_next_step(store, payload, now, check_app=True, simulation=False, waiter=None):
    session = payload.get('session_id')
    if not isinstance(session, str) or not session:
        return None
    path = _next_note(store, session)
    try:
        note = read_json(path)
    except (OSError, ValueError):
        return None
    _drop_next_note(store, session)  # Una nota sirve para un único final de turno.
    if not isinstance(note, dict) or note.get('session_id') != session \
            or not isinstance(note.get('at'), (int, float)) or not 0 <= now - note['at'] <= NEXT_MAX_AGE:
        return None
    parsed = parse_next_step(dict(ask=note.get('ask'), actions=[dict(label=l) for l in note.get('labels') or []
                                                                 if isinstance(l, str)]))
    if parsed is None:
        return None
    ask, labels = parsed
    if check_app and not store.app_alive(ALIVE_MAX_AGE, now):
        return None  # Pancho cerrado: el turno termina y el recuadro sale en Claude.
    questions = [dict(id=NEXT_ID, header='Siguiente paso', question=ask, multiSelect=False,
                      options=[dict(label=l, description='') for l in labels])]
    call = 'next-' + (note.get('call_id') or secrets.token_hex(12))
    return _claim(store, payload, session, call, questions, dict(ask=ask, labels=labels), now,
                  check_app, simulation, waiter, kind=NEXT_KIND)


def next_step_output(original, answer):
    ask = original.get('ask') if isinstance(original, dict) else ''
    reason = ('Javier ha respondido desde Pancho a la pregunta del recuadro final «%s»: «%s». '
              'Es su respuesta: continúa el trabajo con ella, igual que si la hubiera escrito en el chat.'
              % (ask, answer))
    return dict(decision='block', reason=reason)


def next_step_at_stop(store, payload, emit, now, parent_pid):
    """Stop: si el turno dejó una pregunta en el recuadro, espera la respuesta de Pancho.
    Devuelve True si se envió una respuesta (Claude sigue trabajando)."""
    if parent_pid <= 1 or os.getppid() != parent_pid:
        return False
    host = procs.host_chain(parent_pid)
    if not procs.chain_alive(host):
        return False
    state = create_next_step(store, payload, now, waiter=procs.identity(os.getpid()))
    if state is None:
        return False
    _wait_as_hook(store, state, emit, host)
    final = load(store, state['key'])
    return bool(final and final.get('sent'))


def _transition(store, key, status, message, event, only_from=WAITING, timeout=STEP_LOCK_SECONDS,
                when=None, **fields):
    with store.lock(timeout):
        state = load(store, key)
        if state is None or state['status'] not in only_from or state['sent']:
            return None
        if when is not None and not when(state):
            return None
        state.update(status=status, message=message, **fields)
        return save(store, state, event)


def _settle(store, key, status, message, event, **fields):
    """Registra el resultado tras el emit, reintentando el bloqueo hasta SETTLE_SECONDS."""
    deadline = time.monotonic() + SETTLE_SECONDS
    while True:
        try:
            return _transition(store, key, status, message, event, only_from=('sending',), **fields)
        except LockBusy:
            if time.monotonic() >= deadline:
                return None


def _try_send(store, key, emit, progress):
    """'sent', 'rejected', 'stop' o 'failed'. LockBusy se propaga (se reintenta)."""
    directory = store.request_dir(key)
    with store.lock(STEP_LOCK_SECONDS):
        state = load(store, key)
        if state is None or state['status'] not in WAITING or state['sent']:
            return 'stop'
        try:
            response = read_json(directory / 'response.json')
        except FileNotFoundError:
            return 'rejected'
        except (OSError, ValueError):
            response = None
        reason = validate_response(response, state) if response is not None else 'JSON ilegible'
        permission = state.get('kind') == PERMISSION_KIND
        next_step = state.get('kind') == NEXT_KIND
        output = None
        if reason is None:
            original = read_json(directory / 'input.json')
            answers = copy.deepcopy(response['answers'])
            if permission:
                output, reason = permission_output(original, answers[PERMISSION_ID])
            elif next_step:
                output = next_step_output(original, answers[NEXT_ID])
            else:
                updated = copy.deepcopy(original)
                updated['answers'] = answers
                output = dict(hookSpecificOutput=dict(hookEventName='PreToolUse',
                                                      permissionDecision='allow', updatedInput=updated))
        if reason is not None:
            stamp = time.strftime('%Y%m%dT%H%M%S') + '-' + secrets.token_hex(3)
            try:
                os.replace(str(directory / 'response.json'),
                           str(directory / ('response.rejected-' + stamp + '.json')))
            except FileNotFoundError:
                pass
            state.update(status='error', can_retry=True,
                         message='Respuesta no válida (' + reason + '); no se envió. Corrígela y vuelve a enviar')
            save(store, state, 'response_rejected')
            return 'rejected'
        state.update(status='sending', message=MESSAGES['sending'], answers=answers, can_retry=False)
        save(store, state, 'sending')
    progress['sending'] = True
    try:
        emit(output)  # Desde aquí el resultado es incierto: nunca se reintenta.
    except BaseException as exc:
        progress['settled'] = True
        _settle(store, key, 'expired', MESSAGES['orphan'], 'emit_failed', error=type(exc).__name__)
        if not isinstance(exc, Exception):
            raise
        return 'failed'
    progress['settled'] = True
    if permission:  # Sin tool_use_id no hay confirmación posterior que esperar.
        _settle(store, key, 'accepted', MESSAGES['decided'], 'decided', sent=True, accepted=True)
    elif next_step:  # Tampoco hay confirmación: Claude sigue el turno con la respuesta.
        _settle(store, key, 'accepted', MESSAGES['next_sent'], 'next_step_sent', sent=True, accepted=True)
    else:
        _settle(store, key, 'sent', MESSAGES['sent'], 'sent', sent=True)
    return 'sent'


class HeartbeatWatch:
    """Decide si la app se perdió durante la espera.

    Se pierde si el pid del latido ya no existe, o si el latido lleva más de
    `stall` segundos sin cambiar con el pid vivo (app colgada). El tiempo parado se mide
    con el reloj monótono y la cuenta se reinicia cuando el reloj de pared y el monótono
    divergen más de CLOCK_JUMP s entre dos comprobaciones (reposo, despertar o ajuste).
    """

    def __init__(self, read_beat, stall=None, clock=time.time, mono=time.monotonic, alive=pid_alive):
        self.read_beat, self.clock, self.mono, self.alive = read_beat, clock, mono, alive
        self.stall = stall_seconds() if stall is None else stall
        beat = read_beat()
        wall, now = clock(), mono()
        self.pid = beat['pid'] if beat else None
        self.ts = beat['ts'] if beat else None
        age = max(0.0, wall - beat['ts']) if beat else 0.0
        self.changed_at = now - min(age, self.stall)
        self.previous = (wall, now)

    def lost(self):
        beat = self.read_beat()
        wall, now = self.clock(), self.mono()
        last_wall, last_mono = self.previous
        self.previous = (wall, now)
        if abs((wall - last_wall) - (now - last_mono)) > CLOCK_JUMP:
            self.changed_at = now  # Salto de reloj: se reinicia la cuenta del latido.
        if beat is not None and (beat['pid'], beat['ts']) != (self.pid, self.ts):
            self.pid, self.ts, self.changed_at = beat['pid'], beat['ts'], now
        if self.pid is None or not self.alive(self.pid):
            return True
        return now - self.changed_at > self.stall


class Terminated(BaseException):
    """SIGTERM/SIGHUP durante la espera: Claude canceló o agotó su timeout."""


def _abandon(store, key, deadline, progress):
    """Al salir de la espera, ninguna solicitud propia queda esperando a un hook muerto."""
    statuses = WAITING + (('sending',) if progress.get('sending') and not progress.get('settled') else ())
    timed_out = deadline is not None and time.time() >= deadline
    try:
        _transition(store, key, 'expired', MESSAGES['expired' if timed_out else 'orphan'],
                    'expired' if timed_out else 'waiter_exit', only_from=statuses,
                    timeout=2.0, when=lambda s: s.get('waiter_pid') == os.getpid())
    except Exception:
        pass


def wait_for_answer(store, state, emit, check_app=True, host=None, stdout_fd=None, sleep=time.sleep):
    key = state['key']
    directory = store.request_dir(key)
    deadline = state['expires_at']
    beat = HeartbeatWatch(store.heartbeat) if check_app else None
    reader = procs.StdoutWatch(stdout_fd) if stdout_fd is not None else None
    progress = {}

    def step():
        if reader is not None and reader.closed():
            _transition(store, key, 'expired', MESSAGES['orphan'], 'reader_closed')
            return True
        if host is not None and not procs.chain_alive(host):
            _transition(store, key, 'expired', MESSAGES['orphan'], 'host_gone')
            return True
        if os.path.exists(str(directory / 'response.json')):
            return _try_send(store, key, emit, progress) in ('sent', 'stop', 'failed')
        if os.path.exists(str(directory / 'release.json')):
            _transition(store, key, 'released', MESSAGES['release'], 'released_by_app')
            return True
        if beat is not None and beat.lost():
            _transition(store, key, 'released', MESSAGES['lost'], 'app_lost')
            return True
        if time.time() >= deadline:
            _transition(store, key, 'expired', MESSAGES['expired'], 'expired')
            return True
        current = load(store, key)
        return current is None or current['status'] not in WAITING

    try:
        while True:
            try:
                if step():
                    return
            except LockBusy:
                if time.time() >= deadline:
                    return  # `_abandon` lo intenta una última vez; si no, el barrido.
            sleep(POLL_SECONDS)
    finally:
        if reader is not None:
            reader.close()
        _abandon(store, key, deadline, progress)


def _raise_terminated(signum, frame):
    raise Terminated()


def pre_tool_use(store, payload, emit, now, parent_pid):
    """`parent_pid` se toma al arrancar el hook, antes de leer stdin o crear nada."""
    if payload.get('tool_name') != TOOL:
        return
    if parent_pid <= 1 or os.getppid() != parent_pid:
        return  # El padre ya no está: nadie esperaría la respuesta.
    host = procs.host_chain(parent_pid)
    if not procs.chain_alive(host):
        return
    state = create_request(store, payload, now, waiter=procs.identity(os.getpid()))
    _wait_as_hook(store, state, emit, host)


def permission_request(store, payload, emit, now, parent_pid):
    if payload.get('tool_name') in PERMISSION_SKIP:
        return
    if parent_pid <= 1 or os.getppid() != parent_pid:
        return
    host = procs.host_chain(parent_pid)
    if not procs.chain_alive(host):
        return
    state = create_permission(store, payload, now, waiter=procs.identity(os.getpid()))
    _wait_as_hook(store, state, emit, host)


def _wait_as_hook(store, state, emit, host):
    if state is None or state['status'] != 'pending':
        return
    previous = {}
    for sig in (signal.SIGTERM, signal.SIGHUP):
        previous[sig] = signal.signal(sig, _raise_terminated)
    try:
        wait_for_answer(store, state, emit, check_app=True, host=host, stdout_fd=1)
    except Terminated:
        pass
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


# --- PostToolUse / Stop / SessionEnd ------------------------------------------------

def _native_answers(payload):
    result = payload.get('tool_response')
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except ValueError:
            return None, False
    if not isinstance(result, dict):
        return None, False
    failed = bool(result.get('error') or result.get('is_error'))
    answers = result.get('answers')
    return (answers if isinstance(answers, dict) else None), failed


def _permission_ran(store, payload):
    """La herramienta de un aviso de permiso aún pendiente ya se ejecutó: se permitió en Claude.

    Se llama en cada PostToolUse de cualquier herramienta, así que sin avisos pendientes en la
    sesión no toma el bloqueo ni escribe nada. Solo compara; no guarda nada del evento.
    """
    session, tool = payload.get('session_id'), payload.get('tool_name')
    if not isinstance(session, str) or not session or not isinstance(tool, str) or not tool:
        return None
    tool_input = payload.get('tool_input')

    def matches(state):
        if state is None or state.get('kind') != PERMISSION_KIND or state.get('tool') != tool \
                or state.get('status') not in WAITING or state.get('sent'):
            return False
        try:
            original = read_json(store.request_dir(state['key']) / 'input.json')
        except (OSError, ValueError):
            return False
        return isinstance(original, dict) and original.get('tool_name') == tool \
            and original.get('tool_input') == tool_input

    candidates = []
    for key in _index_keys(store, session):
        try:
            if matches(load(store, key)):
                candidates.append(key)
        except ValueError:
            continue
    if not candidates:
        return None
    with store.lock():
        for key in candidates:  # Una ejecución resuelve un solo aviso (el más antiguo).
            state = load(store, key)
            if matches(state):
                state.update(status='resolved_elsewhere', message=MESSAGES['perm_resolved'])
                return save(store, state, 'resolved_elsewhere', at_event='PostToolUse')
    return None


def post_tool_use(store, payload, failure):
    session, call = payload.get('session_id'), payload.get('tool_use_id')
    if payload.get('tool_name') != TOOL:
        return _permission_ran(store, payload)
    if not isinstance(session, str) or not session or not isinstance(call, str) or not call:
        return None
    key = request_key(PROVIDER, HOST, session, call)
    if not (store.request_dir(key) / 'state.json').exists():
        return None
    answers, failed = _native_answers(payload)
    with store.lock():
        state = load(store, key)
        if state is None:
            return None
        status = state['status']
        identical = not failure and not failed and answers is not None and answers == state['answers']
        if status in ('sending', 'sent'):
            if identical:
                state.update(status='accepted', accepted=True, message=MESSAGES['accepted'])
                return save(store, state, 'accepted')
            state.update(status='unconfirmed', message=MESSAGES['unconfirmed'])
            return save(store, state, 'unconfirmed', native_failure=bool(failure or failed))
        if status == 'expired' and state['answers'] is not None and identical:
            # Se dio por perdida en pleno envío, pero Claude confirma lo enviado.
            state.update(status='accepted', accepted=True, sent=True, message=MESSAGES['accepted'])
            return save(store, state, 'accepted_after_expired')
        if status in ('released', 'expired', 'pending', 'error') and not state['sent'] \
                and not failure and answers:
            # Las respuestas nativas no se copian: solo se registra que existieron.
            state.update(status='resolved_elsewhere', message=MESSAGES['resolved'])
            return save(store, state, 'resolved_elsewhere')
        return None


def _orphaned(state):
    return state.get('status') in WAITER_STATES and not state.get('sent') \
        and not procs.same_process(state.get('waiter_pid'), state.get('waiter_start'))


def _permission_left_behind(state, event):
    return state.get('kind') == PERMISSION_KIND and event in TURN_END_EVENTS \
        and state.get('status') in WAITING and not state.get('sent')


def _next_step_left_behind(state, event):
    # Llegó un mensaje nuevo con el recuadro aún pendiente: se contestó escribiendo en Claude.
    return state.get('kind') == NEXT_KIND and event in ('UserPromptSubmit', 'SessionEnd') \
        and state.get('status') in WAITING and not state.get('sent')


def _needs_change(state, event):
    status = state.get('status')
    if _orphaned(state):
        return True
    if _next_step_left_behind(state, event):
        return True
    if _permission_left_behind(state, event):
        return True
    if event in ('Stop', 'StopFailure'):
        return status in ('accepted', 'sending', 'sent')
    return False


def session_event(store, payload, event):
    """Barrido de huérfanos en cualquier evento de la sesión y cierre de turno.

    - pending/error/sending cuyo hook (waiter_pid + hora de inicio) ya no vive → expired.
    - Stop: accepted → continued; sending/sent → unconfirmed.
    - StopFailure: accepted se queda en accepted con «el turno falló»; sending/sent → unconfirmed.
    """
    session = payload.get('session_id')
    if not isinstance(session, str) or not session or event not in SWEEP_EVENTS:
        return
    keys = _index_keys(store, session)
    candidates = []
    for key in keys:  # Lectura sin bloqueo: los eventos sin cambios no compiten por él.
        try:
            state = load(store, key)
        except ValueError:
            continue
        if state is not None and _needs_change(state, event):
            candidates.append(key)
    if not candidates:
        return
    with store.lock():
        for key in candidates:
            state = load(store, key)
            if state is None:
                continue
            status = state['status']
            if _next_step_left_behind(state, event):
                state.update(status='resolved_elsewhere', message=MESSAGES['next_resolved'])
                save(store, state, 'resolved_elsewhere', at_event=event)
            elif _orphaned(state):
                state.update(status='expired', message=MESSAGES['orphan'])
                save(store, state, 'orphaned', at_event=event)
            elif _permission_left_behind(state, event):
                # El turno terminó con el aviso pendiente: se decidió en Claude.
                state.update(status='resolved_elsewhere', message=MESSAGES['perm_resolved'])
                save(store, state, 'resolved_elsewhere', at_event=event)
            elif event == 'Stop' and status == 'accepted':
                state.update(status='continued', continued=True, message=MESSAGES['continued'])
                save(store, state, 'continued')
            elif event == 'StopFailure' and status == 'accepted':
                if state.get('message') != MESSAGES['stop_failed']:
                    state.update(message=MESSAGES['stop_failed'])
                    save(store, state, 'stop_failure')
            elif event in ('Stop', 'StopFailure') and status in ('sending', 'sent'):
                state.update(status='unconfirmed', message=MESSAGES['unconfirmed'])
                save(store, state, 'unconfirmed_at_stop', at_event=event)


# Compatibilidad con el nombre anterior.
end_of_turn = session_event
