"""Subagentes de Claude Code y Codex para el panel de agentes de Pancho.

Un JSON por subagente en `<raíz>/v1/agents/` con lista blanca de campos: proveedor, conversación
de origen, proyecto, tipo, título corto (solo Claude), inicio, fin y estado. Nunca el encargo
(`prompt`), la respuesta (`last_assistant_message`, `tool_response.content`) ni rutas de
transcripción.

Título en Claude: el `PreToolUse` de la herramienta Agent/Task deja un «lanzamiento pendiente»
con su `description`; el siguiente `SubagentStart` de esa conversación (mismo tipo si lo hay) se lo
queda. El `PostToolUse` de Agent trae `agentId` y confirma la pareja; si dos lanzamientos simultáneos
se habían cruzado, se intercambian los títulos.
"""
import os

from .sessions import project_name, valid_cwd
from .store import LockBusy, atomic_json, read_json, sha256_hex, session_key

AGENT_TOOLS = ('Agent', 'Task')
TITLE_MAX = 80
TYPE_MAX = 48
DONE_KEEP = 30 * 60          # Terminados: se ven media hora.
RUNNING_MAX = 12 * 3600      # En marcha sin fin tras 12 h: agente caído, se descarta.
PENDING_MAX = 10 * 60        # Lanzamientos sin SubagentStart: se olvidan a los 10 min.


def _clean(value, limit):
    if not isinstance(value, str):
        return None
    text = ' '.join(value.split())
    if not text:
        return None
    return text if len(text) <= limit else text[:limit - 1].rstrip() + '…'


def _ident(value):
    return value if isinstance(value, str) and value and len(value) <= 200 else None


def _agent_path(store, provider, session, agent):
    return store.dir('agents') / (sha256_hex(provider + '|' + session + '|' + agent) + '.json')


def _pending_path(store, session):
    return store.dir('agents') / ('pending-' + session_key('claude', session) + '.json')


def _read(path):
    try:
        data = read_json(path)
        return data if isinstance(data, (dict, list)) else None
    except (OSError, ValueError):
        return None


def _locked(store, fn):
    """El registro de subagentes es cosmético: con el almacén ocupado se anota el fallo y se sigue."""
    try:
        with store.lock():
            return fn()
    except LockBusy as exc:
        store.log_error('agents', exc)
        return None


# --- Claude: título del encargo ------------------------------------------------------

def note_launch(store, payload, now):
    """PreToolUse de Agent/Task: guarda solo el título corto y el tipo, nunca el encargo."""
    session, call = _ident(payload.get('session_id')), _ident(payload.get('tool_use_id'))
    tool_input = payload.get('tool_input') if isinstance(payload.get('tool_input'), dict) else {}
    title = _clean(tool_input.get('description'), TITLE_MAX)
    if not session or not call or not title:
        return

    def run():
        path = _pending_path(store, session)
        pending = [p for p in (_read(path) or []) if isinstance(p, dict) and now - p.get('at', 0) < PENDING_MAX]
        if not any(p.get('call') == call for p in pending):
            pending.append(dict(call=call, title=title, type=_clean(tool_input.get('subagent_type'), TYPE_MAX),
                                at=now, agent=None))
        atomic_json(path, pending)
    _locked(store, run)


def confirm(store, payload, now):
    """PostToolUse de Agent/Task: `agentId` fija qué subagente lleva qué título."""
    session, call = _ident(payload.get('session_id')), _ident(payload.get('tool_use_id'))
    response = payload.get('tool_response') if isinstance(payload.get('tool_response'), dict) else {}
    agent = _ident(response.get('agentId'))
    if not session or not call or not agent:
        return

    def run():
        pending_path = _pending_path(store, session)
        pending = [p for p in (_read(pending_path) or []) if isinstance(p, dict)]
        entry = next((p for p in pending if p.get('call') == call), None)
        title = _clean(response.get('description'), TITLE_MAX) or (entry or {}).get('title')
        path = _agent_path(store, 'claude', session, agent)
        record = _read(path)
        if not isinstance(record, dict) or not title:
            return
        previous_call = record.get('title_call')
        if previous_call != call:
            # Cruce: el subagente que se quedó con este título recibe el que tenía este.
            for other_path in store.dir('agents').glob('*.json'):
                other = _read(other_path)
                if (isinstance(other, dict) and other.get('session_id') == session and other.get('agent_id') != agent
                        and other.get('title_call') == call and not other.get('title_confirmed')):
                    other.update(title=record.get('title'), title_call=previous_call)
                    atomic_json(other_path, other)
        record.update(title=title, title_call=call, title_confirmed=True)
        atomic_json(path, record)
        if entry is not None:
            pending.remove(entry)
            atomic_json(pending_path, pending)
    _locked(store, run)


def _claim_title(store, session, kind, agent, now):
    path = _pending_path(store, session)
    pending = [p for p in (_read(path) or []) if isinstance(p, dict) and now - p.get('at', 0) < PENDING_MAX]
    free = [p for p in pending if not p.get('agent')]
    entry = next((p for p in free if p.get('type') == kind), None) or (free[0] if free else None)
    if entry is None:
        return None, None
    entry['agent'] = agent
    atomic_json(path, pending)
    return entry.get('title'), entry.get('call')


# --- Inicio y fin (Claude y Codex) ----------------------------------------------------

def start(store, provider, payload, now):
    session, agent = _ident(payload.get('session_id')), _ident(payload.get('agent_id'))
    if not session or not agent:
        return
    kind = _clean(payload.get('agent_type'), TYPE_MAX)
    cwd = valid_cwd(payload.get('cwd'))

    def run():
        path = _agent_path(store, provider, session, agent)
        current = _read(path) if path.exists() else None
        record = current if isinstance(current, dict) else dict(
            provider=provider, session_id=session, agent_id=agent, title=None, title_call=None,
            title_confirmed=False, ended_at=None)
        record.update(agent_type=kind, cwd=cwd, project=project_name(cwd) if cwd else None,
                      started_at=now, status='running', ended_at=None)
        if provider == 'claude' and not record.get('title'):
            record['title'], record['title_call'] = _claim_title(store, session, kind, agent, now)
        atomic_json(path, record)
        _sweep(store, now)
    _locked(store, run)


def stop(store, provider, payload, now):
    session, agent = _ident(payload.get('session_id')), _ident(payload.get('agent_id'))
    if not session or not agent:
        return
    cwd = valid_cwd(payload.get('cwd'))

    def run():
        path = _agent_path(store, provider, session, agent)
        current = _read(path) if path.exists() else None
        record = current if isinstance(current, dict) else dict(
            provider=provider, session_id=session, agent_id=agent, agent_type=_clean(payload.get('agent_type'), TYPE_MAX),
            cwd=cwd, project=project_name(cwd) if cwd else None, title=None, title_call=None,
            title_confirmed=False, started_at=None)
        record.update(status='done', ended_at=now)
        atomic_json(path, record)
        _sweep(store, now)
    _locked(store, run)


def end_session(store, provider, payload, now):
    """Fin de la conversación: sus subagentes sin SubagentStop (interrumpidos) pasan a terminados."""
    session = _ident(payload.get('session_id'))
    if not session:
        return

    def run():
        for path in store.dir('agents').glob('*.json'):
            if path.name.startswith('pending-'):
                continue
            record = _read(path)
            if (isinstance(record, dict) and record.get('provider') == provider and record.get('session_id') == session
                    and record.get('status') != 'done'):
                record.update(status='done', ended_at=now)
                atomic_json(path, record)
        _sweep(store, now)
    _locked(store, run)


# --- Limpieza ---------------------------------------------------------------------------

def _sweep(store, now):
    for path in store.dir('agents').glob('*.json'):
        data = _read(path)
        drop = False
        if path.name.startswith('pending-'):
            if isinstance(data, list):
                kept = [p for p in data if isinstance(p, dict) and now - p.get('at', 0) < PENDING_MAX]
                if not kept:
                    drop = True
                elif len(kept) != len(data):
                    atomic_json(path, kept)
            else:
                drop = True
        elif not isinstance(data, dict):
            drop = True
        elif data.get('status') == 'done':
            drop = now - (data.get('ended_at') or 0) > DONE_KEEP
        else:
            drop = now - (data.get('started_at') or 0) > RUNNING_MAX
        if drop:
            try:
                os.unlink(str(path))
            except OSError:
                pass


