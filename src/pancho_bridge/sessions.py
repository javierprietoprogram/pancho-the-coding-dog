"""Registro mínimo de sesiones observadas por eventos.

Solo se guardan proveedor, sesión, cwd, proyecto, tipo de evento y hora; en Claude, además, de dónde viene la
conversación (para abrirla desde Pancho): id de la sesión de escritorio, punto de entrada y app de terminal.
Nunca el texto del prompt, `last_assistant_message` ni `transcript_path`, ni nada más del entorno.
"""
import os
import re

from .store import atomic_json, read_json, session_key

HOST_SESSION_RE = re.compile(r'^local_[A-Za-z0-9-]{1,64}$')
ENTRYPOINT_RE = re.compile(r'^[a-z0-9][a-z0-9_-]{0,31}$')
# Apps de terminal que Pancho sabe traer al frente (TERM_PROGRAM → la app la elige Pancho.app).
TERM_PROGRAMS = ('Apple_Terminal', 'iTerm.app', 'WarpTerminal', 'ghostty', 'vscode', 'WezTerm', 'Hyper', 'Tabby', 'kitty',
                 'Alacritty', 'zed')


def claude_origin(environ):
    """Lista blanca del entorno del hook de Claude: solo valores con formato válido."""
    host = environ.get('CLAUDE_CODE_HOST_SESSION_ID') or ''
    entry = environ.get('CLAUDE_CODE_ENTRYPOINT') or ''
    term = environ.get('TERM_PROGRAM') or ''
    # Solo la app de escritorio tiene sesión propia; un `claude` lanzado desde dentro hereda la variable pero no es ella.
    if entry != 'claude-desktop':
        host = ''
    return dict(host_session=host if HOST_SESSION_RE.match(host) else None,
                entrypoint=entry if ENTRYPOINT_RE.match(entry) else None,
                term_program=term if term in TERM_PROGRAMS else None)


ACTIVITY = {'UserPromptSubmit': 'working', 'Stop': 'idle', 'SessionEnd': 'ended'}
# Nombres de evento admitidos por el contrato; PostToolUseFailure cuenta como PostToolUse
# y StopFailure como Stop (el turno terminó, aunque fallara).
CONTRACT_EVENT = {
    'SessionStart': 'SessionStart', 'UserPromptSubmit': 'UserPromptSubmit', 'Stop': 'Stop',
    'SessionEnd': 'SessionEnd', 'PreToolUse': 'PreToolUse', 'PostToolUse': 'PostToolUse',
    'PostToolUseFailure': 'PostToolUse', 'StopFailure': 'Stop', 'PermissionRequest': 'PreToolUse',
}


def project_name(cwd):
    stripped = cwd.rstrip('/')
    return os.path.basename(stripped) if stripped else '/'


def valid_cwd(value):
    return value if isinstance(value, str) and value.startswith('/') else None


def record(store, provider, payload, now):
    session = payload.get('session_id')
    if not isinstance(session, str) or not session:
        return None
    event = CONTRACT_EVENT.get(payload.get('hook_event_name'))
    if event is None:
        return None
    cwd = valid_cwd(payload.get('cwd'))
    directory = store.dir('sessions')
    path = directory / (session_key(provider, session) + '.json')
    with store.lock():
        try:
            current = read_json(path)
            if not isinstance(current, dict):
                current = None
        except (OSError, ValueError):
            current = None
        if current is None:
            current = dict(provider=provider, session_id=session, cwd=cwd,
                           project=project_name(cwd) if cwd else None, first_seen=now,
                           last_event=event, last_event_at=now, activity='observed')
        # Lista blanca: solo campos del contrato.
        record = dict(provider=provider, session_id=session,
                      cwd=cwd or current.get('cwd'),
                      project=project_name(cwd) if cwd else current.get('project'),
                      first_seen=current.get('first_seen', now), last_event=event,
                      last_event_at=now, activity=current.get('activity', 'observed'))
        if provider == 'claude':
            origin = claude_origin(os.environ)
            # Con entorno de Claude se toma tal cual (p. ej. reanudada en otra terminal); un evento sin entorno
            # (relanzado por Pancho) no borra lo que ya se sabía.
            known = origin if origin['entrypoint'] else {k: current.get(k) for k in origin}
            record.update(known)
        if event in ACTIVITY:
            record['activity'] = ACTIVITY[event]
        elif event == 'SessionStart' and record['activity'] == 'ended':
            record['activity'] = 'observed'
        atomic_json(path, record)
        return record
