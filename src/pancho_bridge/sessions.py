"""Registro mínimo de sesiones observadas por eventos.

Solo se guardan proveedor, sesión, cwd, proyecto, tipo de evento y hora.
Nunca el texto del prompt, `last_assistant_message` ni `transcript_path`.
"""
import os

from .store import atomic_json, read_json, session_key

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
        if event in ACTIVITY:
            record['activity'] = ACTIVITY[event]
        elif event == 'SessionStart' and record['activity'] == 'ended':
            record['activity'] = 'observed'
        atomic_json(path, record)
        return record
