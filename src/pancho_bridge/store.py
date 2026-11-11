"""Almacén v1: rutas, permisos privados, escrituras atómicas y bloqueo global.

Directorios 0700 y archivos 0600. Escritura normal: temporal + fsync + rename.
Creación única: temporal + link(), que falla si el destino ya existe.
"""
import contextlib
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import tempfile
import time

LOG_LIMIT_BYTES = 256 * 1024
LOCK_TIMEOUT = 2.0
LOCK_RETRY = 0.02


class LockBusy(Exception):
    """No se obtuvo el bloqueo global dentro del plazo."""


def default_root():
    return Path(os.path.expanduser('~')) / 'Library' / 'Application Support' / 'Pancho'


def resolve_root(explicit=None):
    """`--home` explícito; si no, `$PANCHO_HOME`; si no, la ruta por defecto."""
    if explicit:
        return Path(os.path.expanduser(str(explicit))).absolute()
    env = os.environ.get('PANCHO_HOME')
    if env:
        return Path(os.path.expanduser(env)).absolute()
    return default_root()


def sha256_hex(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def request_key(provider, host, session_id, call_id):
    """sha256 en hexadecimal de "provider|host|session_id|call_id" (UTF-8)."""
    return sha256_hex('|'.join([provider, host, session_id, call_id]))


def session_key(provider, session_id):
    return sha256_hex(provider + '|' + session_id)


def utc_stamp():
    return (datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
            + '-' + secrets.token_hex(3))


def ensure_private_dir(path, top):
    """Crea `path` y los directorios que falten desde `top` (incluido) con 0700."""
    path, top = Path(path), Path(top)
    chain = []
    current = path
    while True:
        chain.append(current)
        if current == top or current.parent == current:
            break
        current = current.parent
    if chain[-1] != top:
        raise ValueError('Ruta fuera de la raíz privada')
    if not top.parent.exists():
        os.makedirs(str(top.parent), exist_ok=True)
    for directory in reversed(chain):
        try:
            os.mkdir(str(directory), 0o700)
            os.chmod(str(directory), 0o700)
        except FileExistsError:
            if not directory.is_dir():
                raise
    return path


def _write_temp(directory, data):
    fd, name = tempfile.mkstemp(prefix='.write-', dir=str(directory))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'wb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        if os.path.exists(name):
            os.unlink(name)
        raise
    return name


def dump_json(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8')


def atomic_json(path, value):
    path = Path(path)
    name = _write_temp(path.parent, dump_json(value))
    try:
        os.replace(name, str(path))
    finally:
        if os.path.exists(name):
            os.unlink(name)


def create_once_json(path, value):
    """Publica `path` solo si no existe (FileExistsError en caso contrario)."""
    path = Path(path)
    name = _write_temp(path.parent, dump_json(value))
    try:
        os.link(name, str(path))
    finally:
        os.unlink(name)


def read_json(path):
    with open(str(path), 'rb') as handle:
        return json.loads(handle.read().decode('utf-8'))


def append_jsonl(path, value):
    line = (json.dumps(value, ensure_ascii=False) + '\n').encode('utf-8')
    fd = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, line)
    finally:
        os.close(fd)


def pid_alive(pid):
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


class Store:
    def __init__(self, root):
        self.root = Path(root)
        self.v1 = self.root / 'v1'

    # --- rutas -------------------------------------------------------------
    def dir(self, *parts):
        return ensure_private_dir(self.v1.joinpath(*parts), self.root)

    def request_dir(self, key):
        if len(key) != 64 or any(c not in '0123456789abcdef' for c in key):
            raise ValueError('Identificador de solicitud inválido')
        return self.v1 / 'requests' / key

    def heartbeat_path(self):
        return self.v1 / 'app' / 'heartbeat.json'

    def is_simulation(self):
        try:
            return read_json(self.v1 / 'app' / 'mode.json').get('simulation') is True
        except (OSError, ValueError, AttributeError):
            return False

    # --- bloqueo global ----------------------------------------------------
    @contextlib.contextmanager
    def lock(self, timeout=LOCK_TIMEOUT):
        """flock LOCK_EX|LOCK_NB con reintentos hasta `timeout` s; si no, LockBusy.

        Nunca se bloquea indefinidamente: un hook que no consigue el bloqueo sale en
        silencio (exit 0) y la espera de AskUserQuestion lo reintenta dentro de su plazo.
        """
        self.dir()
        fd = os.open(str(self.v1 / '.lock'), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            deadline = time.monotonic() + max(0.0, timeout)
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise LockBusy('bloqueo ocupado')
                    time.sleep(LOCK_RETRY)
            yield
        finally:
            os.close(fd)

    # --- latido de la app --------------------------------------------------
    def heartbeat(self):
        try:
            value = read_json(self.heartbeat_path())
        except (OSError, ValueError):
            return None
        if not isinstance(value, dict):
            return None
        ts, pid = value.get('ts'), value.get('pid')
        if isinstance(ts, bool) or not isinstance(ts, (int, float)):
            return None
        if isinstance(pid, bool) or not isinstance(pid, int):
            return None
        return dict(ts=float(ts), pid=pid)

    def app_alive(self, max_age, now=None):
        """Viva si el latido tiene como mucho `max_age` segundos y su pid existe."""
        beat = self.heartbeat()
        if beat is None:
            return False
        now = time.time() if now is None else now
        return now - beat['ts'] <= max_age and pid_alive(beat['pid'])

    # --- registro de errores mínimo (sin payload) ---------------------------
    def log_error(self, stage, exc):
        try:
            logs = self.dir('logs')
            path = logs / 'hook-errors.log'
            try:
                if path.stat().st_size > LOG_LIMIT_BYTES:
                    os.replace(str(path), str(logs / 'hook-errors.log.1'))
            except FileNotFoundError:
                pass
            append_jsonl(path, dict(at=time.time(), stage=str(stage), error=type(exc).__name__))
        except Exception:
            pass
