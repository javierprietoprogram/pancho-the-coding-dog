"""Instalación global de los hooks Claude de Pancho (T1).

- Copia este paquete a `<raíz>/bridge-versions/<sello>/` y publica `<raíz>/bridge` como
  enlace simbólico reemplazado atómicamente (os.replace), de modo que un hook que arranca
  nunca encuentra la ruta a medio copiar.
- Modifica `~/.claude/settings.json` añadiendo solo entradas propias (`--owner pancho-v1`),
  tras guardar la copia exacta previa (o la constancia de ausencia) en
  `<raíz>/backups/<sello>/` y el manifiesto en `<raíz>/install/manifest.json`.
- Compare-and-swap: si el archivo cambia entre la lectura y la publicación, se aborta sin
  sobrescribir.
- `uninstall` retira solo las entradas idénticas al manifiesto; conserva y comunica las
  modificadas por terceros. No borra código, datos ni copias.
- Migración PoC (`--migrate-poc`): retirada selectiva existente de `src/installation.py`
  sobre `<proyecto>/.claude/settings.local.json`. No toca nada de Codex.
- El comando instalado pasa siempre `--home` explícito y el intérprete elegido
  (`--python`, ruta absoluta sin resolver enlaces). `status` lo muestra y avisa si ya no existe.
- Entradas con `--owner pancho-v1` que no están en el manifiesto (copias manuales o
  entradas propias modificadas): `status` las lista y avisa; `install` no añade otra
  junto a una equivalente modificada salvo con `--force`.
"""
import argparse
import contextlib
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile

from . import OWNER, VERSION
from .store import atomic_json, default_root, ensure_private_dir, read_json, resolve_root

POC_OWNER = 'desktop-questions-poc-v1'
MATCHER = '^AskUserQuestion$'
# PreToolUse escucha además el recuadro «siguiente paso» de la app de Claude y el lanzamiento de
# subagentes (Agent; Task en versiones antiguas) para el panel de agentes: ambos solo se anotan.
PRE_MATCHER = '^(AskUserQuestion|mcp__ccd_turn__wrap_up|Agent|Task)$'
EVENTS = (  # (evento, matcher, timeout)
    ('SessionStart', None, 10),
    ('UserPromptSubmit', None, 10),
    ('PreToolUse', PRE_MATCHER, 1860),
    # Cualquier herramienta: así se nota al instante un permiso concedido en el propio Claude.
    ('PostToolUse', None, 10),
    ('PostToolUseFailure', None, 10),
    ('PermissionRequest', None, 1860),  # Avisos de permiso de cualquier herramienta.
    ('Stop', None, 1860),  # Puede esperar la respuesta al recuadro «siguiente paso».
    ('StopFailure', None, 10),  # Válido en Claude Code 2.1.317 (PoC verificada).
    ('SessionEnd', None, 10),
    ('SubagentStart', None, 10),  # Panel de agentes: inicio y fin de cada subagente.
    ('SubagentStop', None, 10),
)
MIN_PYTHON = (3, 9)
OWNER_PATTERN = re.compile(r"--owner\s+'?" + re.escape(OWNER) + r"'?(\s|$)")


class InstallError(Exception):
    """Instalación rechazada sin cambios (o con el detalle de lo hecho en el mensaje)."""


class ConcurrentChange(InstallError):
    pass


def default_settings():
    return Path(os.path.expanduser('~')) / '.claude' / 'settings.json'


def _stamp():
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + secrets.token_hex(4)


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


@contextlib.contextmanager
def _install_lock(root):
    ensure_private_dir(root / 'install', root)
    fd = os.open(str(root / 'install' / '.lock'), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _manifest_path(root):
    return root / 'install' / 'manifest.json'


def _load_manifest(root):
    path = _manifest_path(root)
    return read_json(path) if path.exists() else None


# --- configuración de Claude -------------------------------------------------------------

def _read_settings(path):
    if path.is_symlink():
        raise InstallError('La configuración es un enlace simbólico; no se modifica: ' + str(path))
    if not path.exists():
        return None, {}
    raw = path.read_bytes()
    try:
        data = json.loads(raw.decode('utf-8'))
    except ValueError:
        raise InstallError('La configuración no es JSON válido; no se modifica: ' + str(path))
    if not isinstance(data, dict):
        raise InstallError('La configuración no es un objeto JSON; no se modifica')
    hooks = data.get('hooks', {})
    if not isinstance(hooks, dict) or any(not isinstance(v, list) for v in hooks.values()):
        raise InstallError('`hooks` no tiene una forma compatible; no se modifica')
    return raw, data


def _write_settings(path, data, expected, mode):
    """Publica `data` solo si el archivo sigue siendo exactamente `expected`."""
    if not path.parent.exists():
        os.makedirs(str(path.parent), 0o700)
    payload = (json.dumps(data, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    fd, tmp = tempfile.mkstemp(prefix='.pancho-', dir=str(path.parent))
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, 'wb') as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        current = path.read_bytes() if path.exists() else None
        if current != expected:
            raise ConcurrentChange('La configuración cambió durante la operación; se conserva tal cual y no se publica nada')
        os.replace(tmp, str(path))
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _delete_settings(path, expected):
    current = path.read_bytes() if path.exists() else None
    if current != expected:
        raise ConcurrentChange('La configuración cambió durante la retirada; se conserva tal cual')
    path.unlink()


def build_command(python, root):
    launcher = root / 'bridge' / 'pancho_hook.py'
    # `--home` siempre explícito: el hook no depende de $PANCHO_HOME ni del HOME del host.
    parts = [str(python), '-I', str(launcher), 'hook', '--provider', 'claude', '--owner', OWNER,
             '--home', str(root)]
    # `|| true`: aunque falte el intérprete o el código, el hook nunca bloquea (nunca exit 2).
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
    """[(evento, índice, grupo)] de los grupos cuyo comando lleva `--owner pancho-v1`."""
    found = []
    for event, groups in (hooks or {}).items():
        for index, group in enumerate(groups if isinstance(groups, list) else []):
            inner = group.get('hooks') if isinstance(group, dict) else None
            if any(isinstance(h, dict) and isinstance(h.get('command'), str) and OWNER_PATTERN.search(h['command'])
                   for h in (inner if isinstance(inner, list) else [])):
                found.append((event, index, group))
    return found


def unmanaged_entries(hooks, manifest):
    """Entradas `--owner pancho-v1` que no son exactamente las del manifiesto activo."""
    managed = (manifest or {}).get('entries', {}) if (manifest or {}).get('active') else {}
    return [dict(event=event, index=index) for event, index, group in owner_entries(hooks)
            if managed.get(event) != group]


def _unmanaged_warning(entries):
    return ('Hay entradas de Pancho (--owner pancho-v1) que no están en el manifiesto: '
            + ', '.join('%s[%d]' % (e['event'], e['index']) for e in entries)
            + '. Revísalas a mano; no se tocan.')


def same_installation(previous, entries, hooks):
    """La instalación activa coincide con `entries`: las añadidas (`entries` del manifiesto)
    más las idénticas que ya estaban (`preexisting_identical_entries`, solo nombres de evento;
    `[]` en manifiestos antiguos) cubren exactamente los mismos eventos, y todas siguen
    presentes en la configuración (r1, M3)."""
    added = previous.get('entries', {})
    preexisting = previous.get('preexisting_identical_entries') or []
    if set(added) | set(preexisting) != set(entries) or set(added) & set(preexisting):
        return False
    if any(added[e] != entries[e] for e in added):
        return False
    return all(isinstance(hooks.get(e), list) and entries[e] in hooks[e] for e in entries)


def missing_events(previous, entries, hooks):
    """Eventos que esta versión añade o cambia respecto a una instalación anterior intacta, o
    None si la anterior no se reconoce tal cual en la configuración (entonces no se toca)."""
    added = previous.get('entries', {})
    preexisting = previous.get('preexisting_identical_entries') or []
    known = set(added) | set(preexisting)
    if not known or not known <= set(entries) or set(added) & set(preexisting):
        return None
    # Lo que Pancho añadió debe seguir exactamente como lo dejó; lo que ya estaba, idéntico a lo actual.
    if not all(isinstance(hooks.get(e), list) and added[e] in hooks[e] for e in added):
        return None
    if not all(isinstance(hooks.get(e), list) and entries[e] in hooks[e] for e in preexisting):
        return None
    return [e for e in entries if e not in known or (e in added and added[e] != entries[e])]


def pending_events(manifest, hooks, builder):
    """Eventos que esta versión de Pancho añade o cambia y la configuración aún no tiene (para que
    la app ofrezca «Actualizar»). [] si está al día; None si la instalación no se reconoce."""
    python = (manifest.get('python') or {}).get('path')
    if not python:
        return None
    entries = builder(Path(python), Path(manifest.get('root')))
    return missing_events(manifest, entries, hooks)


# --- intérprete y copia del código ---------------------------------------------------------

def check_python(python):
    python = Path(python)
    if not python.is_absolute() or not os.access(str(python), os.X_OK):
        raise InstallError('Intérprete no ejecutable: ' + str(python))
    probe = subprocess.run([str(python), '-I', '-c',
                            'import sys,json;print(json.dumps([list(sys.version_info[:3]),sys.version]))'],
                           capture_output=True, timeout=30)
    if probe.returncode != 0:
        raise InstallError('El intérprete no arranca: ' + str(python))
    info, version = json.loads(probe.stdout.decode('utf-8'))
    if tuple(info[:2]) < MIN_PYTHON:
        raise InstallError('Se necesita Python 3.9 o posterior; encontrado ' + '.'.join(map(str, info)))
    return dict(path=str(python), realpath=os.path.realpath(str(python)),
                version='.'.join(map(str, info)), version_full=version)


def _hash_tree(directory):
    result = {}
    for path in sorted(directory.rglob('*')):
        if path.is_file():
            result[str(path.relative_to(directory))] = _sha256(path.read_bytes())
    return result


def _py_tree(directory):
    return {name: digest for name, digest in _hash_tree(directory).items() if name.endswith('.py')} if directory.is_dir() else {}


def bridge_outdated(root):
    """¿La copia instalada del puente difiere del código de esta versión de Pancho? (la app ofrece «Actualizar»)."""
    installed = Path(root) / 'bridge' / 'pancho_bridge'
    if not installed.is_dir():
        return False
    return _py_tree(installed) != _py_tree(Path(__file__).resolve().parent)


def _deploy_bridge(root, python):
    package = Path(__file__).resolve().parent
    versions = ensure_private_dir(root / 'bridge-versions', root)
    name = _stamp()
    target = versions / name
    shutil.copytree(str(package), str(target / 'pancho_bridge'),
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    shutil.copyfile(str(package / 'launcher.py'), str(target / 'pancho_hook.py'))
    for path in [target] + list(target.rglob('*')):
        os.chmod(str(path), 0o700 if path.is_dir() else 0o600)
    check = subprocess.run([str(python), '-I', str(target / 'pancho_hook.py'), 'version'],
                           capture_output=True, timeout=30)
    if check.returncode != 0 or check.stdout.decode('utf-8').strip() != VERSION:
        shutil.rmtree(str(target), ignore_errors=True)
        raise InstallError('La copia del puente no supera la comprobación con ese intérprete')
    link = root / 'bridge'
    if link.exists() and not link.is_symlink():
        os.replace(str(link), str(versions / ('legacy-' + name)))
    temp_link = root / ('.bridge-' + secrets.token_hex(4))
    os.symlink(os.path.join('bridge-versions', name), str(temp_link))
    os.replace(str(temp_link), str(link))  # Reemplazo atómico.
    # Se conservan la versión actual y la inmediatamente anterior.
    keep = sorted((p for p in versions.iterdir() if p.is_dir()), key=lambda p: p.stat().st_mtime)
    for old in keep[:-2]:
        if old != target:
            shutil.rmtree(str(old), ignore_errors=True)
    return dict(path=str(link), version_dir=str(target), version=VERSION, files=_hash_tree(target))


# --- PoC de mascota ---------------------------------------------------------------------------

def inspect_poc(poc_root):
    if poc_root is None:
        return dict(found=False, checked=False)
    poc_root = Path(poc_root).absolute()
    config = poc_root / '.claude' / 'settings.local.json'
    info = dict(found=False, checked=True, config=str(config), entries=[], consistent=False)
    if not config.exists():
        return info
    try:
        hooks = json.loads(config.read_text(encoding='utf-8')).get('hooks', {})
    except (ValueError, AttributeError):
        raise InstallError('No se puede leer ' + str(config) + '; revísalo antes de instalar')
    found = []
    if isinstance(hooks, dict):
        for event, groups in hooks.items():
            for group in groups if isinstance(groups, list) else []:
                commands = [h.get('command', '') for h in (group.get('hooks', []) if isinstance(group, dict) else [])
                            if isinstance(h, dict)]
                if any(isinstance(c, str) and POC_OWNER in c for c in commands):
                    found.append((event, group))
    info['found'] = bool(found)
    info['entries'] = [event for event, _ in found]
    manifest_path = poc_root / '.local-test' / 'claude' / 'install.json'
    try:
        manifest = read_json(manifest_path)
        # Misma condición que exige installation.uninstall (ruta resuelta idéntica).
        info['consistent'] = bool(
            manifest.get('active') and manifest.get('config') == str(config.resolve())
            and all(manifest.get('entries', {}).get(event) == group for event, group in found)
            and (poc_root / 'src' / 'installation.py').exists())
    except (OSError, ValueError, AttributeError):
        info['consistent'] = False
    return info


def migrate_poc(poc_root, python):
    """Retirada selectiva existente de la PoC (misma lógica que `scripts/poc.py retirar`)."""
    poc_root = Path(poc_root).absolute()
    control = poc_root / '.local-test' / 'claude' / 'control.json'
    control_disabled = False
    if control.exists():
        value = read_json(control)
        value['enabled'] = False
        atomic_json(control, value)
        control_disabled = True
    code = ('import json,sys;sys.path.insert(0,sys.argv[1]);from installation import uninstall;'
            'print(json.dumps(uninstall(sys.argv[2],sys.argv[3])))')
    run = subprocess.run([str(python), '-I', '-c', code, str(poc_root / 'src'), str(poc_root),
                          str(poc_root / '.claude' / 'settings.local.json')], capture_output=True, timeout=60)
    if run.returncode != 0:
        raise InstallError('La retirada PoC falló (' + run.stderr.decode('utf-8', 'replace').strip()[-300:]
                           + '); los hooks globales ya están instalados. Revisa la PoC a mano.')
    result = json.loads(run.stdout.decode('utf-8'))
    after = inspect_poc(poc_root)
    return dict(at=_stamp(), poc_root=str(poc_root), config=str(poc_root / '.claude' / 'settings.local.json'),
                control_disabled=control_disabled, poc_uninstall=result,
                remaining_poc_entries=after['entries'], evidence_and_backups='conservadas')


# --- operaciones ---------------------------------------------------------------------------

def _extend(root, settings, python, data, original_bytes, hooks, previous, entries, missing, manifest_path=None):
    """Amplía una instalación anterior con los eventos nuevos, con copia previa propia."""
    manifest_path = manifest_path or _manifest_path(root)
    stamp = _stamp()
    backup_dir = ensure_private_dir(root / 'backups' / stamp, root)
    backup = backup_dir / settings.name
    fd = os.open(str(backup), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as handle:
        handle.write(original_bytes)
    added = []
    for event in missing:
        groups = hooks.setdefault(event, [])
        old = previous['entries'].get(event)
        if old is not None:  # Entrada propia que cambia: se sustituye en su mismo sitio.
            groups[groups.index(old)] = entries[event]
            previous['entries'][event] = entries[event]
            added.append(event)
        elif entries[event] in groups:
            previous.setdefault('preexisting_identical_entries', []).append(event)
        else:
            groups.append(entries[event])
            previous['entries'][event] = entries[event]
            added.append(event)
    previous.setdefault('extensions', []).append(
        dict(at=stamp, events=added, backup=str(backup), backup_sha256=_sha256(original_bytes)))
    previous.update(version=VERSION, refreshed_at=stamp)
    atomic_json(manifest_path, previous)  # Antes de mutar: datos de recuperación.
    data['hooks'] = hooks
    _write_settings(settings, data, original_bytes, settings.stat().st_mode & 0o777)
    previous['bridge'] = _deploy_bridge(root, python)
    atomic_json(manifest_path, previous)
    extra = unmanaged_entries(hooks, previous)
    return dict(previous, already_installed=True, extended_events=added, unmanaged_owner_entries=extra,
                warnings=[_unmanaged_warning(extra)] if extra else [])


def install(home=None, settings=None, poc_root=None, migrate=False, python=None, force=False):
    root = resolve_root(home)
    settings = Path(os.path.expanduser(str(settings))).absolute() if settings else default_settings()
    python = Path(python or sys.executable).absolute()
    ensure_private_dir(root, root)
    with _install_lock(root):
        interpreter = check_python(python)
        original_bytes, data = _read_settings(settings)
        if data.get('disableAllHooks'):
            raise InstallError('Los hooks están desactivados (disableAllHooks); no se cambia esa preferencia')
        poc = inspect_poc(poc_root)
        if poc['found'] and not migrate:
            raise InstallError('Hay hooks de la PoC en ' + poc['config'] + ' (' + ', '.join(poc['entries'])
                               + '). Repite con --migrate-poc para retirarlos de forma selectiva.')
        if poc['found'] and not poc['consistent']:
            raise InstallError('Los hooks PoC no coinciden con su manifiesto; no se migra automáticamente. Revísalos a mano.')
        entries = build_entries(python, root)
        hooks = data.get('hooks', {})
        previous = _load_manifest(root)
        if previous and previous.get('active'):
            if previous['settings_path'] != str(settings):
                raise InstallError('Ya instalado en otra configuración: ' + previous['settings_path'])
            missing = missing_events(previous, entries, hooks)
            if missing:
                return _extend(root, settings, python, data, original_bytes, hooks, previous, entries, missing)
            if not same_installation(previous, entries, hooks):
                raise InstallError('La instalación existente es distinta o fue modificada; ejecuta uninstall primero')
            previous['bridge'] = _deploy_bridge(root, python)
            previous['refreshed_at'] = _stamp()
            if poc['found']:
                previous.setdefault('poc_migrations', []).append(migrate_poc(poc_root, python))
            atomic_json(_manifest_path(root), previous)
            extra = unmanaged_entries(hooks, previous)
            warnings = [_unmanaged_warning(extra)] if extra else []
            return dict(previous, already_installed=True, unmanaged_owner_entries=extra, warnings=warnings)
        # Entradas propias modificadas o copiadas a mano en los mismos eventos: no se duplican.
        conflicts = [dict(event=event, index=index) for event, index, group in owner_entries(hooks)
                     if event in entries and group != entries[event]]
        if conflicts and not force:
            raise InstallError(_unmanaged_warning(conflicts).replace(' Revísalas a mano; no se tocan.', '')
                               + '. No se añade otra para no duplicar el hook: retírala o repite con --force.')
        bridge = _deploy_bridge(root, python)
        stamp = _stamp()
        backup_dir = ensure_private_dir(root / 'backups' / stamp, root)
        if original_bytes is not None:
            backup = backup_dir / settings.name
            fd = os.open(str(backup), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as handle:
                handle.write(original_bytes)
        else:
            backup = backup_dir / 'original-absent.json'
            atomic_json(backup, dict(path=str(settings), existed=False, at=stamp))
        added, preexisting = {}, []
        for event, entry in entries.items():
            groups = hooks.setdefault(event, [])
            if entry in groups:
                preexisting.append(event)  # Idéntica ya presente: no se duplica ni se reclama.
            else:
                groups.append(entry)
                added[event] = entry
        manifest = dict(schema=1, owner=OWNER, active=True, version=VERSION, installed_at=stamp,
                        root=str(root), settings_path=str(settings),
                        settings_existed=original_bytes is not None,
                        original_hooks_present='hooks' in data, backup=str(backup),
                        backup_sha256=_sha256(original_bytes) if original_bytes is not None else None,
                        entries=added, preexisting_identical_entries=preexisting,
                        python=interpreter, bridge=bridge, poc_migrations=[],
                        forced_over_unmanaged=conflicts if force else [])
        atomic_json(_manifest_path(root), manifest)  # Antes de mutar: datos de recuperación.
        data['hooks'] = hooks
        mode = (settings.stat().st_mode & 0o777) if original_bytes is not None else 0o600
        try:
            _write_settings(settings, data, original_bytes, mode)
        except ConcurrentChange:
            manifest.update(active=False, failure='concurrent_configuration_change')
            atomic_json(_manifest_path(root), manifest)
            raise
        if poc['found']:
            manifest['poc_migrations'].append(migrate_poc(poc_root, python))
            atomic_json(_manifest_path(root), manifest)
        extra = unmanaged_entries(hooks, manifest)
        return dict(manifest, unmanaged_owner_entries=extra, warnings=[_unmanaged_warning(extra)] if extra else [])


def uninstall(home=None, settings=None):
    root = resolve_root(home)
    with _install_lock(root):
        manifest = _load_manifest(root)
        if not manifest or not manifest.get('active'):
            return dict(removed=False, message='No hay instalación activa; nada que retirar')
        path = Path(manifest['settings_path'])
        if settings and Path(os.path.expanduser(str(settings))).absolute() != path:
            raise InstallError('Esa configuración no es la del manifiesto: ' + manifest['settings_path'])
        removed, retained = [], []
        if path.exists():
            original_bytes, data = _read_settings(path)
            hooks = data.get('hooks', {})
            for event, entry in manifest['entries'].items():
                groups = hooks.get(event)
                if isinstance(groups, list) and entry in groups:
                    groups.remove(entry)  # Solo la entrada exacta añadida por Pancho.
                    removed.append(event)
                    if not groups:
                        hooks.pop(event, None)
                else:
                    retained.append(event)
            if hooks or manifest['original_hooks_present']:
                data['hooks'] = hooks
            else:
                data.pop('hooks', None)
            if not data and not manifest['settings_existed']:
                _delete_settings(path, original_bytes)
            elif removed:
                _write_settings(path, data, original_bytes, path.stat().st_mode & 0o777)
        else:
            retained = []
        manifest.update(active=False, uninstalled_at=_stamp(), removed_entries=removed,
                        retained_modified_or_missing=retained)
        atomic_json(_manifest_path(root), manifest)
        message = 'Entradas de Pancho retiradas. Código, datos y copias conservados en ' + str(root)
        if retained:
            message += ('. Conservadas sin tocar por haber cambiado o desaparecido: ' + ', '.join(retained))
        return dict(removed=bool(removed), removed_entries=removed, retained_modified_or_missing=retained,
                    backup=manifest['backup'], message=message)


def install_status(home=None, settings=None, poc_root=None):
    root = resolve_root(home)
    manifest = _load_manifest(root)
    result = dict(root=str(root), installed=bool(manifest and manifest.get('active')),
                  bridge_present=(root / 'bridge' / 'pancho_hook.py').exists(), poc=inspect_poc(poc_root),
                  warnings=[])
    path = Path(manifest['settings_path']) if manifest else (
        Path(os.path.expanduser(str(settings))).absolute() if settings else default_settings())
    try:
        hooks = _read_settings(path)[1].get('hooks', {})
    except InstallError as exc:
        hooks, result['settings_error'] = {}, str(exc)
    result['settings_path'] = str(path)
    if manifest:
        python = manifest.get('python') or {}
        present = bool(python.get('path')) and os.access(python['path'], os.X_OK)
        result.update(backup=manifest.get('backup'), python=python, version=manifest.get('version'),
                      python_present=present,
                      python_realpath_now=os.path.realpath(python['path']) if present else None,
                      command=build_command(python.get('path', '?'), Path(manifest.get('root', str(root)))),
                      entries={e: ('presente' if isinstance(hooks.get(e), list) and entry in hooks[e]
                                   else 'ausente o modificada') for e, entry in manifest['entries'].items()},
                      poc_migrations=manifest.get('poc_migrations', []))
        if manifest.get('active'):
            result['missing_events'] = pending_events(manifest, hooks, build_entries)
            result['bridge_outdated'] = bridge_outdated(root)
        if manifest.get('active') and not present:
            result['warnings'].append('El intérprete de los hooks no existe o no es ejecutable: '
                                      + str(python.get('path')) + '. Los hooks no hacen nada (|| true); '
                                      'reinstala con --python <ruta absoluta>.')
    extra = unmanaged_entries(hooks, manifest)
    result['unmanaged_owner_entries'] = extra
    if extra:
        result['warnings'].append(_unmanaged_warning(extra))
    return result


def main(argv=None, default_poc_root=None):
    parser = argparse.ArgumentParser(prog='pancho_install', description='Instalación global de Pancho (Claude o, con --codex, Codex)')
    parser.add_argument('action', choices=['install', 'status', 'uninstall'])
    parser.add_argument('--codex', action='store_true', help='Opera sobre los hooks Codex (~/.codex/hooks.json)')
    parser.add_argument('--codex-hooks', help='Ruta de hooks.json de Codex (por defecto ~/.codex/hooks.json)')
    parser.add_argument('--codex-pin', help='app-pin.json de la PoC (por defecto <poc-root>/.local-test/codex/app-pin.json)')
    parser.add_argument('--codex-app', help='Ruta de la app Codex (por defecto /Applications/ChatGPT.app)')
    parser.add_argument('--home', help='Raíz de Pancho (por defecto $PANCHO_HOME o ~/Library/Application Support/Pancho)')
    parser.add_argument('--claude-settings', help='Ruta de settings.json de Claude (por defecto ~/.claude/settings.json)')
    parser.add_argument('--python', help='Intérprete absoluto para los hooks (por defecto el actual)')
    parser.add_argument('--poc-root', default=default_poc_root, help='Proyecto con la PoC (mascota)')
    parser.add_argument('--migrate-poc', action='store_true', help='Retira de forma selectiva los hooks PoC')
    parser.add_argument('--force', action='store_true',
                        help='Instala aunque haya entradas --owner pancho-v1 modificadas fuera del manifiesto')
    args = parser.parse_args(argv)
    try:
        if args.codex:
            from . import codex_install
            if args.action == 'install':
                result = codex_install.install(args.home, args.codex_hooks, args.poc_root, args.migrate_poc,
                                               args.python, args.codex_pin, args.codex_app, args.force)
            elif args.action == 'uninstall':
                result = codex_install.uninstall(args.home, args.codex_hooks)
            else:
                result = codex_install.status(args.home, args.codex_hooks, args.poc_root)
        elif args.action == 'install':
            result = install(args.home, args.claude_settings, args.poc_root, args.migrate_poc, args.python,
                             args.force)
        elif args.action == 'uninstall':
            result = uninstall(args.home, args.claude_settings)
        else:
            result = install_status(args.home, args.claude_settings, args.poc_root)
    except InstallError as exc:
        print(json.dumps(dict(ok=False, error=str(exc)), ensure_ascii=False, indent=2))
        return 3
    print(json.dumps(dict(result, ok=True), ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
