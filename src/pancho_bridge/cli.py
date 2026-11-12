"""Línea de órdenes del puente.

  hook --provider claude|codex --owner pancho-v1 [--home RAÍZ]
      stdin: evento del hook; stdout: solo protocolo (Codex: nunca escribe). Siempre exit 0;
      ante cualquier error interno, sin stdout y una línea mínima en v1/logs/hook-errors.log.
  codex-send --key K [--home RAÍZ]
      La app lo invoca UNA vez tras crear response.json de una solicitud Codex. Una línea
      JSON en stdout y un código de salida (ver codex.EXIT y CONTRACT.md «Codex (T2)»).
  status [--home RAÍZ]
      Resumen sin contenido de preguntas ni respuestas.
  simulate ask|session [--home RAÍZ_SIMULACIÓN] ...
      Datos falsos en una raíz separada marcada con v1/app/mode.json {"simulation":true}.
  version
"""
import argparse
import json
import os
import secrets
import sys
import time

from . import OWNER, VERSION
from . import agents, claude, sessions
from .store import LockBusy, Store, atomic_json, default_root, read_json, resolve_root

HOOK_EVENTS = ('SessionStart', 'UserPromptSubmit', 'PreToolUse', 'PostToolUse',
               'PostToolUseFailure', 'Stop', 'StopFailure', 'SessionEnd', 'PermissionRequest',
               'SubagentStart', 'SubagentStop')
SUBAGENT_EVENTS = ('SubagentStart', 'SubagentStop')


# --- hook ------------------------------------------------------------------------

def _emit(output):
    data = json.dumps(output, ensure_ascii=False).encode('utf-8')
    try:
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
    except BaseException:
        # Lo no escrito no debe reintentarse al cerrar el intérprete (exit 120 y stderr).
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, 1)
            os.close(devnull)
        except OSError:
            pass
        raise


def _parse_hook_args(argv):
    """Analizador propio: nunca imprime ni sale con código distinto de 0."""
    values = dict(provider=None, owner=None, home=None, codex_ipc_socket=None)
    i = 0
    while i < len(argv):
        name = argv[i]
        if name in ('--provider', '--owner', '--home', '--codex-ipc-socket') and i + 1 < len(argv):
            values[name[2:].replace('-', '_')] = argv[i + 1]
            i += 2
        else:
            return None
    return values


def run_hook(argv, stdin=None):
    parent_pid = os.getppid()  # Lo primero: antes de leer stdin o crear nada.
    store = None
    stage = 'start'
    try:
        args = _parse_hook_args(argv)
        if args is None or args['owner'] != OWNER or args['provider'] not in ('claude', 'codex'):
            return 0
        store = Store(resolve_root(args['home']))
        stage = 'stdin'
        raw = (stdin if stdin is not None else sys.stdin.buffer).read()
        payload = json.loads(raw.decode('utf-8'))
        if not isinstance(payload, dict):
            raise ValueError('evento no es un objeto')
        name = payload.get('hook_event_name')
        if args['provider'] == 'codex':
            stage = 'codex'
            return _run_codex_hook(store, payload, args)
        if name not in HOOK_EVENTS:
            return 0
        now = time.time()
        if name in SUBAGENT_EVENTS:
            stage = 'agents'
            (agents.start if name == 'SubagentStart' else agents.stop)(store, claude.PROVIDER, payload, now)
            return 0
        stage = 'session'
        question = (name == 'PreToolUse' and payload.get('tool_name') == claude.TOOL) \
            or name == claude.PERMISSION_EVENT
        try:
            sessions.record(store, claude.PROVIDER, payload, now)
        except LockBusy:
            if not question:
                raise  # Salida silenciosa (exit 0); queda una línea en el registro.
            # Una pregunta no se pierde por el registro de actividad: se sigue.
        stage = name
        if name == 'PreToolUse' and payload.get('tool_name') in agents.AGENT_TOOLS:
            agents.note_launch(store, payload, now)  # Solo el título corto; nunca espera ni responde.
        elif name == 'PreToolUse' and payload.get('tool_name') == claude.NEXT_TOOL:
            claude.note_next_step(store, payload, now)
        elif name == 'PreToolUse':
            claude.pre_tool_use(store, payload, _emit, now, parent_pid)
        elif name == claude.PERMISSION_EVENT:
            claude.permission_request(store, payload, _emit, now, parent_pid)
        elif name in ('PostToolUse', 'PostToolUseFailure'):
            claude.post_tool_use(store, payload, failure=(name == 'PostToolUseFailure'))
            if name == 'PostToolUse' and payload.get('tool_name') in agents.AGENT_TOOLS:
                agents.confirm(store, payload, now)
        if name in claude.SWEEP_EVENTS:
            claude.session_event(store, payload, name)
        if name == 'SessionEnd':
            agents.end_session(store, claude.PROVIDER, payload, now)
        if name == 'UserPromptSubmit' and isinstance(payload.get('session_id'), str) and payload['session_id']:
            claude._drop_next_note(store, payload['session_id'])
        if name == 'Stop':
            stage = 'next_step'
            if claude.next_step_at_stop(store, payload, _emit, time.time(), parent_pid):
                # Claude sigue trabajando con la respuesta: la conversación vuelve a «trabajando».
                sessions.record(store, claude.PROVIDER, dict(payload, hook_event_name='UserPromptSubmit'), time.time())
    except BaseException as exc:  # noqa: B902 — el hook nunca debe fallar hacia Claude.
        if store is not None:
            store.log_error(stage, exc)
    return 0


def _run_codex_hook(store, payload, args):
    """Codex: registra y sale; nunca escribe en stdout (sería contexto del modelo)."""
    from . import codex  # Import diferido: el hook Claude no paga su coste de arranque.
    name = payload.get('hook_event_name')
    now = time.time()
    if name in SUBAGENT_EVENTS:
        (agents.start if name == 'SubagentStart' else agents.stop)(store, codex.PROVIDER, payload, now)
        return 0
    if name not in codex.HOOK_EVENTS:
        return 0
    if payload.get('agent_id'):
        # Evento dentro de un subagente: no es una conversación tuya (ya sale en el panel de agentes).
        for key in codex.handle(store, payload, now):
            spawn_sender(store.root, key, args.get('codex_ipc_socket'))
        return 0
    try:
        sessions.record(store, codex.PROVIDER, payload, now)
    except LockBusy as exc:
        # El registro de actividad es cosmético: nunca impide procesar el evento (r1, I1).
        store.log_error('codex-session-record', exc)
    for key in codex.handle(store, payload, now):
        spawn_sender(store.root, key, args.get('codex_ipc_socket'))
    return 0


def spawn_sender(root, key, ipc_socket=None):
    """Proceso desacoplado de un disparo (`codex-send --key K`): el hook Stop no espera."""
    import subprocess
    package_parent = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
    code = ('import sys;sys.path.insert(0,sys.argv[1]);from pancho_bridge.cli import main;'
            'sys.exit(main(sys.argv[2:]))')
    argv = [sys.executable, '-I', '-c', code, package_parent, 'codex-send', '--key', key,
            '--home', str(root)]
    if ipc_socket:
        argv += ['--ipc-socket', ipc_socket]
    subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True, close_fds=True, cwd='/')


# --- codex-send ------------------------------------------------------------------------

def codex_send(args):
    from . import codex
    store = Store(resolve_root(args.home))
    try:
        store.request_dir(args.key)
    except ValueError:
        print(json.dumps(dict(ok=False, result='not_applicable', key=args.key, status=None, can_retry=False)))
        return codex.EXIT['not_applicable']
    try:
        from .codex_ipc import DesktopIPC
        import uuid

        def factory():
            return DesktopIPC(socket_path=args.ipc_socket, timeout=codex.IPC_TIMEOUT,
                              client_id='pancho-' + uuid.uuid4().hex)
        result, state = codex.send(store, args.key, factory)
    except Exception as exc:  # Nunca traza con contenido: solo el tipo.
        store.log_error('codex-send', exc)
        print(json.dumps(dict(ok=False, result='internal_error', error=type(exc).__name__)))
        return 1
    print(json.dumps(dict(ok=result in ('sent', 'queued', 'already'), result=result, key=args.key,
                          status=state.get('status') if state else None,
                          can_retry=state.get('can_retry') if state else False), ensure_ascii=False))
    return codex.EXIT[result]


# --- status ------------------------------------------------------------------------

def status(root):
    store = Store(root)
    result = dict(root=str(root), simulation=store.is_simulation(),
                  app_alive=store.app_alive(claude.ALIVE_MAX_AGE), sessions={}, requests=[])
    for path in sorted((store.v1 / 'sessions').glob('*.json')):
        try:
            activity = read_json(path).get('activity', 'unknown')
        except (OSError, ValueError, AttributeError):
            activity = 'unreadable'
        result['sessions'][activity] = result['sessions'].get(activity, 0) + 1
    for path in sorted((store.v1 / 'requests').glob('*/state.json')):
        try:
            state = read_json(path)
            result['requests'].append(dict(
                key=state['key'][:12], provider=state.get('provider'), project=state.get('project'),
                session=str(state.get('session_id', ''))[:8], status=state.get('status'),
                created_at=state.get('created_at'), simulation=state.get('simulation', False)))
        except (OSError, ValueError, KeyError, TypeError):
            result['requests'].append(dict(path=str(path.parent.name)[:12], status='unreadable'))
    result['requests'].sort(key=lambda r: (r.get('created_at') or 0, r.get('key', '')))
    return result


# --- simulate ------------------------------------------------------------------------

SAMPLE_QUESTIONS = [
    dict(header='SIMULACIÓN', multiSelect=False,
         question='SIMULACIÓN — no procede de Claude.\n¿Qué color prefieres?  Texto con dos espacios, '
                  'tildes (áéíóú, ñ) y emoji 🙂 al final.  ',
         options=[dict(label='Azul', description='Descripción íntegra de Azul. ' + 'detalle ' * 40 + 'FIN'),
                  dict(label='Verde', description='Descripción de Verde.')]),
    dict(header='Varias', multiSelect=True,
         question='SIMULACIÓN — elige una o varias opciones, o escribe texto libre.',
         options=[dict(label='Uno', description='Primera'), dict(label='Dos', description='Segunda'),
                  dict(label='Tres', description='Tercera')]),
]


def prepare_simulation_root(root):
    root = os.path.abspath(os.path.expanduser(str(root)))
    if root == str(default_root()):
        raise SystemExit('La simulación necesita una raíz separada (p. ej. <raíz>/simulation)')
    store = Store(root)
    if store.v1.exists() and not store.is_simulation():
        raise SystemExit('Esa raíz ya contiene datos no simulados; usa otra para la simulación')
    store.dir('app')
    atomic_json(store.v1 / 'app' / 'mode.json', dict(simulation=True))
    return store


def simulate(args):
    store = prepare_simulation_root(args.home or (str(default_root() / 'simulation')))
    session = args.session or ('SIMULACION-' + secrets.token_hex(4))
    cwd = args.cwd or '/simulacion/proyecto-demo'
    now = time.time()
    base = dict(session_id=session, cwd=cwd, tool_name=claude.TOOL)
    if args.what == 'session':
        sessions.record(store, claude.PROVIDER, dict(base, hook_event_name=args.event), now)
        print(json.dumps(dict(simulation=True, session_id=session), ensure_ascii=False))
        return 0
    questions = SAMPLE_QUESTIONS
    if args.questions:
        with open(args.questions, 'rb') as handle:
            questions = json.loads(handle.read().decode('utf-8'))
    call = 'sim-' + secrets.token_hex(6)
    sessions.record(store, claude.PROVIDER, dict(base, hook_event_name='UserPromptSubmit'), now)
    payload = dict(base, hook_event_name='PreToolUse', tool_use_id=call,
                   tool_input=dict(questions=questions))
    state = claude.create_request(store, payload, now, check_app=False, simulation=True)
    if state is None:
        raise SystemExit('Preguntas no válidas para la simulación')
    summary = dict(simulation=True, key=state['key'], session_id=session, call_id=call,
                   directory=str(store.request_dir(state['key'])))
    if args.wait <= 0:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    state['expires_at'] = min(state['expires_at'], now + args.wait)
    outputs = []
    claude.wait_for_answer(store, state, outputs.append, check_app=False)
    if outputs and args.auto_accept:
        answers = outputs[0]['hookSpecificOutput']['updatedInput']['answers']
        claude.post_tool_use(store, dict(base, hook_event_name='PostToolUse', tool_use_id=call,
                                         tool_response=dict(answers=answers)), failure=False)
        sessions.record(store, claude.PROVIDER, dict(base, hook_event_name='Stop'), time.time())
        claude.end_of_turn(store, dict(base, hook_event_name='Stop'), 'Stop')
    final = claude.load(store, state['key'])
    summary.update(hook_output=outputs[0] if outputs else None, status=final['status'])
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


# --- main ------------------------------------------------------------------------

def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ['hook']:
        return run_hook(argv[1:])
    parser = argparse.ArgumentParser(prog='pancho_bridge', description='Puente local de Pancho')
    sub = parser.add_subparsers(dest='command', required=True)
    p_status = sub.add_parser('status', help='Resumen del almacén')
    p_status.add_argument('--home')
    p_send = sub.add_parser('codex-send', help='Envía a Codex la respuesta de una solicitud (lo invoca la app)')
    p_send.add_argument('--key', required=True)
    p_send.add_argument('--home')
    p_send.add_argument('--ipc-socket', help=argparse.SUPPRESS)  # Solo tests: servidor IPC falso.
    sub.add_parser('version')
    sub.add_parser('hook', help='Hook de Claude (stdin → stdout)')
    p_sim = sub.add_parser('simulate', help='Datos falsos en una raíz de simulación separada')
    p_sim.add_argument('what', choices=['ask', 'session'])
    p_sim.add_argument('--home', help='Raíz de simulación (por defecto <raíz>/simulation)')
    p_sim.add_argument('--session')
    p_sim.add_argument('--cwd')
    p_sim.add_argument('--event', default='UserPromptSubmit', choices=list(sessions.CONTRACT_EVENT))
    p_sim.add_argument('--questions', help='JSON con la lista questions de AskUserQuestion')
    p_sim.add_argument('--wait', type=float, default=0.0,
                       help='Segundos esperando response.json como el hook (0 = solo crear)')
    p_sim.add_argument('--auto-accept', action='store_true',
                       help='Tras enviar, simula PostToolUse idéntico y Stop')
    args = parser.parse_args(argv)
    if args.command == 'version':
        print(VERSION)
        return 0
    if args.command == 'status':
        print(json.dumps(status(resolve_root(args.home)), ensure_ascii=False, indent=2))
        return 0
    if args.command == 'simulate':
        return simulate(args)
    if args.command == 'codex-send':
        return codex_send(args)
    return 0
