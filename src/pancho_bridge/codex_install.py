"""Instalación global de los hooks Codex de Pancho (T2).

- Fuente de usuario documentada (learn.chatgpt.com/docs/hooks, leída el 2026-11-09):
  `~/.codex/hooks.json` con `{"hooks": {Evento: [{"matcher"?, "hooks": [{"type": "command",
  "command", "timeout"}]}]}}`. Timeout en segundos; SessionEnd admite como máximo 3 s.
  Matcher: regex sobre `tool_name` en PreToolUse/PostToolUse; se omite en los demás.
- Comando ESTABLE: apunta a `<raíz>/bridge/pancho_hook.py` (enlace simbólico), no a una
  versión. Actualizar Pancho cambia el destino del enlace, no el texto del hook, así que
  Codex no pide revisarlo de nuevo.
- Copia exacta previa (o constancia de ausencia) en `<raíz>/backups/<sello>/`, manifiesto
  propio `<raíz>/install/codex-manifest.json`, compare-and-swap al publicar, idempotencia y
  desinstalación selectiva (solo entradas idénticas al manifiesto).
- Migración PoC (`--migrate-poc`), ANTES de activar los globales: `control.enabled=false`
  y retirada selectiva de las entradas `--owner desktop-questions-codex-poc-v1` de
  `<mascota>/.codex/hooks.json`, conservando las ajenas; el archivo solo se borra si queda
  vacío y no existía antes de la PoC (según su `install.json`, que no se modifica). Nunca se
  ejecuta `codex_poc.py retirar`.
- Pin de la app (desde 2026-11-11, por interfaz): escribe `<raíz>/install/codex-app-pin.json`
  con `mode: interface`, la ruta de la app y su ASAR; no fija versión ni hashes (ver
  `codex.verify_pin`). `--codex-pin` se acepta por compatibilidad y ya no se usa. Si la app
  instalada no tiene la interfaz, se instala igual y `status` lo avisa: `codex-send` no enviará nada.
- Confianza: Pancho NUNCA lee ni escribe `~/.codex/config.toml` (`hooks.state`). `status`
  lista las entradas que Javier debe revisar en Codex («Review hooks» o Settings > Hooks).
"""
import contextlib
import fcntl
import json
import os
from pathlib import Path
import shlex
import sys

from . import OWNER, VERSION
from . import codex
from . import installer as _installer
from .installer import (ConcurrentChange, InstallError, OWNER_PATTERN, _delete_settings, _install_lock,
                        _read_settings, _sha256, _stamp, _write_settings, check_python)
from .store import atomic_json, ensure_private_dir, read_json, resolve_root

POC_OWNER = 'desktop-questions-codex-poc-v1'
MATCHER = '^request_user_input_async$'
EVENTS = (  # (evento, matcher, timeout en segundos)
    ('SessionStart', None, 10),
    ('UserPromptSubmit', None, 10),
    ('PreToolUse', MATCHER, 10),
    ('PostToolUse', MATCHER, 10),
    ('Stop', None, 30),
    ('SessionEnd', None, 3),   # Máximo documentado: 3 s.
    ('SubagentStart', None, 10),  # Panel de agentes: inicio y fin de cada subagente.
    ('SubagentStop', None, 10),
)
APP_PATH = '/Applications/ChatGPT.app'
MANIFEST = 'codex-manifest.json'
REVIEW_HOW = ('En Codex Desktop: pulsa «Review hooks» en el compositor (o abre Settings > Hooks), '
              'revisa las entradas «pancho_hook.py … --provider codex» de ~/.codex/hooks.json y apruébalas. '
              'Pancho no escribe esa confianza.')


def default_hooks():
    return Path(os.path.expanduser('~')) / '.codex' / 'hooks.json'


def _manifest_path(root):
    return root / 'install' / MANIFEST


def _load_manifest(root):
    path = _manifest_path(root)
    return read_json(path) if path.exists() else None


def build_command(python, root):
    parts = [str(python), '-I', str(root / 'bridge' / 'pancho_hook.py'), 'hook', '--provider', 'codex',
             '--owner', OWNER, '--home', str(root)]
    return ' '.join(shlex.quote(p) for p in parts) + ' || true'


def build_entries(python, root):
    command = build_command(python, root)
    entries = {}
    for event, matcher, timeout in EVENTS:
        entry = {}
        if matcher:
            entry['matcher'] = matcher
        entry['hooks'] = [dict(type='command', command=command, timeout=timeout)]
        entries[event] = entry
    return entries


def owner_entries(hooks):
    found = []
    for event, groups in (hooks or {}).items():
        for index, group in enumerate(groups if isinstance(groups, list) else []):
            inner = group.get('hooks') if isinstance(group, dict) else None
            if any(isinstance(h, dict) and isinstance(h.get('command'), str) and OWNER_PATTERN.search(h['command'])
                   and '--provider codex' in h['command'] for h in (inner if isinstance(inner, list) else [])):
                found.append((event, index, group))
    return found


def unmanaged_entries(hooks, manifest):
    managed = (manifest or {}).get('entries', {}) if (manifest or {}).get('active') else {}
    return [dict(event=e, index=i) for e, i, g in owner_entries(hooks) if managed.get(e) != g]


def _python_default(root):
    try:
        claude = read_json(root / 'install' / 'manifest.json')
        if claude.get('active') and claude.get('python', {}).get('path'):
            return claude['python']['path']
    except (OSError, ValueError, AttributeError):
        pass
    return sys.executable


# --- pin ----------------------------------------------------------------------------------

def _install_pin(root, pin_source, poc_root, app_path):
    """Pin por interfaz (decisión 2026-11-11). `pin_source` y `poc_root` ya no se usan."""
    app = Path(str(app_path or APP_PATH)).absolute()
    pin = dict(schema=2, mode='interface', app_path=str(app), bundle_id=codex.BUNDLE_ID,
               archive_path=str(app / 'Contents' / 'Resources' / 'app.asar'),
               interface=list(codex.INTERFACE), written_at=_stamp())
    ensure_private_dir(root / 'install', root)
    atomic_json(root / 'install' / codex.PIN_FILE, pin)
    return pin


def pin_check(root):
    try:
        codex.verify_pin(root / 'install' / codex.PIN_FILE)
        return 'ok'
    except codex.PinError as exc:
        return 'no coincide: ' + str(exc)


# --- PoC Codex de mascota -----------------------------------------------------------------------

def inspect_poc(poc_root):
    if poc_root is None:
        return dict(found=False, checked=False, entries=[])
    poc_root = Path(poc_root).absolute()
    config = poc_root / '.codex' / 'hooks.json'
    info = dict(found=False, checked=True, config=str(config), entries=[], consistent=False)
    if not config.exists():
        return info
    try:
        hooks = json.loads(config.read_text(encoding='utf-8')).get('hooks', {})
    except (ValueError, AttributeError):
        raise InstallError('No se puede leer ' + str(config) + '; revísalo antes de instalar')
    found = []
    for event, groups in (hooks.items() if isinstance(hooks, dict) else []):
        for group in groups if isinstance(groups, list) else []:
            inner = group.get('hooks', []) if isinstance(group, dict) else []
            if any(isinstance(h, dict) and POC_OWNER in str(h.get('command', '')) for h in inner):
                found.append((event, group))
    info.update(found=bool(found), entries=[e for e, _ in found])
    try:
        manifest = read_json(poc_root / '.local-test' / 'codex' / 'install.json')
        info['consistent'] = bool(
            manifest.get('active') and manifest.get('owner') == POC_OWNER
            and manifest.get('config') == str(poc_root.resolve() / '.codex' / 'hooks.json')
            and all(manifest.get('entries', {}).get(e) == g for e, g in found)
            and len(found) == len(set(e for e, _ in found)))
    except (OSError, ValueError, AttributeError):
        info['consistent'] = False
    return info


@contextlib.contextmanager
def _poc_lock(local):
    path = local / '.installation.lock'
    if not local.is_dir():
        yield
        return
    with open(str(path), 'a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def _put_bytes(path, data, mode):
    """Restaura bytes exactos (temporal + rename) o borra si `data` es None."""
    if data is None:
        if path.exists():
            path.unlink()
        return
    tmp = path.with_name('.pancho-restore-' + os.urandom(4).hex())
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(fd, 'wb') as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(str(tmp), str(path))


def rollback_poc(undo):
    """Deja la PoC como estaba antes de `migrate_poc` (r1, M5). Devuelve lo que no pudo."""
    problems = []
    config = Path(undo['config'])
    current = config.read_bytes() if config.exists() else None
    if current == undo['config_after']:
        _put_bytes(config, undo['config_before'], undo['config_mode'])
    else:
        problems.append('la configuración PoC cambió después de migrar; no se restaura: ' + str(config))
    if undo.get('control_before') is not None:
        _put_bytes(Path(undo['control']), undo['control_before'], undo['control_mode'])
    return problems


def migrate_poc(poc_root, undo=None):
    """Retirada selectiva de los hooks PoC Codex (sin ejecutar `codex_poc.py retirar`).

    `undo` (dict) recibe los bytes previos para `rollback_poc`. Si la propia migración
    falla, se restaura `control.json` antes de propagar el error.
    """
    poc_root = Path(poc_root).absolute()
    local = poc_root / '.local-test' / 'codex'
    config = poc_root / '.codex' / 'hooks.json'
    undo = {} if undo is None else undo
    control = local / 'control.json'
    undo.update(config=str(config), control=str(control),
                control_before=control.read_bytes() if control.exists() else None,
                control_mode=(control.stat().st_mode & 0o777) if control.exists() else 0o600,
                config_before=config.read_bytes() if config.exists() else None,
                config_mode=(config.stat().st_mode & 0o777) if config.exists() else 0o600)
    try:
        return _migrate_poc(poc_root, local, config, control, undo)
    except BaseException:
        if undo.get('control_before') is not None:
            _put_bytes(control, undo['control_before'], undo['control_mode'])
        raise


def _migrate_poc(poc_root, local, config, control, undo):
    with _poc_lock(local):
        control_disabled = False
        if control.exists():
            value = read_json(control)
            value['enabled'] = False  # Primero se desactiva: sus hooks pasan a no hacer nada.
            atomic_json(control, value)
            control_disabled = True
        manifest = read_json(local / 'install.json')
        raw, data = _read_settings(config)
        hooks = data.get('hooks', {})
        original_hooks = (manifest.get('original_shape') or {}).get('hooks', {})
        removed, retained = [], []
        for event, entry in manifest['entries'].items():
            groups = hooks.get(event, [])
            if groups.count(entry) == 1:
                groups.remove(entry)
                removed.append(event)
                if not groups and event not in original_hooks:
                    hooks.pop(event, None)
            else:
                retained.append(event)
        if not hooks and 'hooks' not in manifest.get('original_top_keys', []):
            data.pop('hooks', None)
        if manifest.get('description_added') and data.get('description') == manifest.get('installed_description'):
            data.pop('description', None)
        deleted = False
        if not data and not manifest.get('original_existed'):
            _delete_settings(config, raw)
            deleted = True
        else:
            _write_settings(config, data, raw, config.stat().st_mode & 0o777)
        undo['config_after'] = config.read_bytes() if config.exists() else None
    remaining = inspect_poc(poc_root)['entries']
    if remaining:
        raise InstallError('Quedan hooks PoC Codex en ' + str(config) + ' (' + ', '.join(remaining)
                           + '); la PoC ya está desactivada. Revísalos a mano antes de instalar.')
    return dict(at=_stamp(), poc_root=str(poc_root), config=str(config), control_disabled=control_disabled,
                removed=removed, retained=retained, config_deleted=deleted,
                poc_manifest='sin cambios (no se ejecuta codex_poc.py retirar)',
                evidence_and_backups='conservadas')


# --- operaciones -------------------------------------------------------------------------------

def _review(entries):
    return dict(entries=sorted(entries), how=REVIEW_HOW, trust_state_read=False,
                note='Codex ejecuta un hook solo cuando Javier lo aprueba; si cambia su definición, lo vuelve a pedir.')


def install(home=None, hooks_path=None, poc_root=None, migrate=False, python=None, pin_source=None,
            app_path=None, force=False):
    root = resolve_root(home)
    hooks_path = Path(os.path.expanduser(str(hooks_path))).absolute() if hooks_path else default_hooks()
    ensure_private_dir(root, root)
    python = Path(python or _python_default(root)).absolute()
    with _install_lock(root):
        interpreter = check_python(python)
        original_bytes, data = _read_settings(hooks_path)
        poc = inspect_poc(poc_root)
        if poc['found'] and not migrate:
            raise InstallError('Hay hooks de la PoC Codex en ' + poc['config'] + ' (' + ', '.join(poc['entries'])
                               + '). Repite con --migrate-poc para retirarlos de forma selectiva.')
        if poc['found'] and not poc['consistent']:
            raise InstallError('Los hooks PoC Codex no coinciden con su manifiesto; no se migran. Revísalos a mano.')
        entries = build_entries(python, root)
        hooks = data.get('hooks', {})
        previous = _load_manifest(root)
        if previous and previous.get('active'):
            if previous['hooks_path'] != str(hooks_path):
                raise InstallError('Ya instalado en otra configuración: ' + previous['hooks_path'])
            missing = _installer.missing_events(previous, entries, hooks)
            if missing and original_bytes is not None:
                # Versión nueva de Pancho con eventos nuevos: se amplía (copia previa) y Codex pide
                # el visto bueno solo de esas entradas.
                result = _installer._extend(root, hooks_path, python, data, original_bytes, hooks, previous,
                                            entries, missing, manifest_path=_manifest_path(root))
                result['pin'] = previous['pin'] = _install_pin(root, pin_source, poc_root, app_path)
                atomic_json(_manifest_path(root), previous)
                return dict(result, review=_review(result['extended_events']), pin_check=pin_check(root))
            if not _installer.same_installation(previous, entries, hooks):
                raise InstallError('La instalación Codex existente es distinta o fue modificada; '
                                   'ejecuta uninstall --codex primero')
            previous['pin'] = _install_pin(root, pin_source, poc_root, app_path)
            previous['bridge'] = _installer._deploy_bridge(root, python)
            if poc['found']:
                previous.setdefault('poc_migrations', []).append(migrate_poc(poc_root))
            previous['refreshed_at'] = _stamp()
            atomic_json(_manifest_path(root), previous)
            return dict(previous, already_installed=True, review=_review([]), pin_check=pin_check(root),
                        unmanaged_owner_entries=unmanaged_entries(hooks, previous))
        conflicts = [dict(event=e, index=i) for e, i, g in owner_entries(hooks) if e in entries and g != entries[e]]
        if conflicts and not force:
            raise InstallError('Hay entradas Codex de Pancho fuera del manifiesto: '
                               + ', '.join('%s[%d]' % (c['event'], c['index']) for c in conflicts)
                               + '. Retíralas o repite con --force.')
        # Orden (r1, M5): pin y puente primero (solo tocan la raíz de Pancho); después la PoC; al
        # final ~/.codex/hooks.json. Si falla la publicación, la PoC se restaura.
        pin = _install_pin(root, pin_source, poc_root, app_path)
        bridge = _installer._deploy_bridge(root, python)
        undo = {}
        migrations = [migrate_poc(poc_root, undo)] if poc['found'] else []
        if migrations:
            original_bytes, data = _read_settings(hooks_path)  # Relectura tras la migración.
            hooks = data.get('hooks', {})
        stamp = _stamp()
        backup_dir = ensure_private_dir(root / 'backups' / stamp, root)
        if original_bytes is not None:
            backup = backup_dir / 'codex-hooks.json'
            fd = os.open(str(backup), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as handle:
                handle.write(original_bytes)
        else:
            backup = backup_dir / 'codex-hooks-original-absent.json'
            atomic_json(backup, dict(path=str(hooks_path), existed=False, at=stamp))
        added, preexisting = {}, []
        for event, entry in entries.items():
            groups = hooks.setdefault(event, [])
            if entry in groups:
                preexisting.append(event)
            else:
                groups.append(entry)
                added[event] = entry
        manifest = dict(schema=1, owner=OWNER, provider='codex', active=True, version=VERSION,
                        installed_at=stamp, root=str(root), hooks_path=str(hooks_path),
                        hooks_existed=original_bytes is not None, original_hooks_present='hooks' in data,
                        backup=str(backup),
                        backup_sha256=_sha256(original_bytes) if original_bytes is not None else None,
                        entries=added, preexisting_identical_entries=preexisting, command=build_command(python, root),
                        python=interpreter, bridge=bridge, pin=pin, poc_migrations=migrations,
                        send_argv=[str(python), '-I', str(root / 'bridge' / 'pancho_hook.py'), 'codex-send',
                                   '--home', str(root), '--key'],
                        forced_over_unmanaged=conflicts if force else [])
        atomic_json(_manifest_path(root), manifest)
        data['hooks'] = hooks
        mode = (hooks_path.stat().st_mode & 0o777) if original_bytes is not None else 0o600
        try:
            _write_settings(hooks_path, data, original_bytes, mode)
        except BaseException as exc:
            failure = 'concurrent_configuration_change' if isinstance(exc, ConcurrentChange) else type(exc).__name__
            manifest.update(active=False, failure=failure)
            message = 'No se publicaron los hooks Codex (' + str(exc) + ').'
            if migrations:
                problems = rollback_poc(undo)
                manifest['poc_rollback'] = problems or 'PoC restaurada'
                message += (' PoC restaurada tal como estaba (hooks de proyecto y control).' if not problems
                            else ' ATENCIÓN, PoC sin restaurar del todo: ' + '; '.join(problems))
            atomic_json(_manifest_path(root), manifest)
            if isinstance(exc, ConcurrentChange):
                raise ConcurrentChange(message) from None
            raise
        return dict(manifest, review=_review(added), pin_check=pin_check(root),
                    unmanaged_owner_entries=unmanaged_entries(hooks, manifest))


def uninstall(home=None, hooks_path=None):
    root = resolve_root(home)
    with _install_lock(root):
        manifest = _load_manifest(root)
        if not manifest or not manifest.get('active'):
            return dict(removed=False, message='No hay instalación Codex activa; nada que retirar')
        path = Path(manifest['hooks_path'])
        if hooks_path and Path(os.path.expanduser(str(hooks_path))).absolute() != path:
            raise InstallError('Esa configuración no es la del manifiesto: ' + manifest['hooks_path'])
        removed, retained = [], []
        if path.exists():
            original_bytes, data = _read_settings(path)
            hooks = data.get('hooks', {})
            for event, entry in manifest['entries'].items():
                groups = hooks.get(event)
                if isinstance(groups, list) and entry in groups:
                    groups.remove(entry)
                    removed.append(event)
                    if not groups:
                        hooks.pop(event, None)
                else:
                    retained.append(event)
            if hooks or manifest['original_hooks_present']:
                data['hooks'] = hooks
            else:
                data.pop('hooks', None)
            if not data and not manifest['hooks_existed']:
                _delete_settings(path, original_bytes)
            elif removed:
                _write_settings(path, data, original_bytes, path.stat().st_mode & 0o777)
        manifest.update(active=False, uninstalled_at=_stamp(), removed_entries=removed,
                        retained_modified_or_missing=retained)
        atomic_json(_manifest_path(root), manifest)
        message = 'Entradas Codex de Pancho retiradas. Código, datos y copias conservados en ' + str(root)
        if retained:
            message += '. Conservadas sin tocar por haber cambiado o desaparecido: ' + ', '.join(retained)
        return dict(removed=bool(removed), removed_entries=removed, retained_modified_or_missing=retained,
                    backup=manifest['backup'], message=message)


def status(home=None, hooks_path=None, poc_root=None):
    root = resolve_root(home)
    manifest = _load_manifest(root)
    path = Path(manifest['hooks_path']) if manifest else (
        Path(os.path.expanduser(str(hooks_path))).absolute() if hooks_path else default_hooks())
    result = dict(root=str(root), installed=bool(manifest and manifest.get('active')), hooks_path=str(path),
                  bridge_present=(root / 'bridge' / 'pancho_hook.py').exists(), poc=inspect_poc(poc_root),
                  warnings=[])
    try:
        hooks = _read_settings(path)[1].get('hooks', {})
    except InstallError as exc:
        hooks, result['hooks_error'] = {}, str(exc)
    if manifest:
        python = manifest.get('python') or {}
        present = bool(python.get('path')) and os.access(python['path'], os.X_OK)
        entries = {e: ('presente' if isinstance(hooks.get(e), list) and entry in hooks[e] else 'ausente o modificada')
                   for e, entry in manifest['entries'].items()}
        result.update(version=manifest.get('version'), backup=manifest.get('backup'), python=python,
                      python_present=present, command=manifest.get('command'), entries=entries,
                      send_argv=manifest.get('send_argv'), poc_migrations=manifest.get('poc_migrations', []),
                      review=_review([e for e, v in entries.items() if v == 'presente'] if manifest.get('active') else []),
                      pin_check=pin_check(root))
        if manifest.get('active'):
            result['missing_events'] = _installer.pending_events(manifest, hooks, build_entries)
            result['bridge_outdated'] = _installer.bridge_outdated(root)
        if manifest.get('active') and not present:
            result['warnings'].append('El intérprete de los hooks Codex no existe: ' + str(python.get('path')))
        if manifest.get('active') and result['pin_check'] != 'ok':
            result['warnings'].append('El pin de Codex Desktop no coincide con la app instalada (' + result['pin_check']
                                      + '): Pancho registra preguntas pero no envía respuestas a Codex.')
    if result['poc']['found']:
        result['warnings'].append('Siguen los hooks PoC Codex en ' + result['poc']['config']
                                  + ': se ejecutarían a la vez. Instala con --migrate-poc.')
    extra = unmanaged_entries(hooks, manifest)
    result['unmanaged_owner_entries'] = extra
    if extra:
        result['warnings'].append('Hay entradas Codex de Pancho fuera del manifiesto; revísalas a mano.')
    return result
