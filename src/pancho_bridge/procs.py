"""Identidad de procesos y vigilancia del lector de stdout (macOS).

- Identidad = pid + hora de inicio, para que un pid reutilizado no pase por el mismo
  proceso. En macOS se lee con libproc (`proc_pidinfo` PROC_PIDTBSDINFO, una llamada al
  sistema, sin subprocesos); si no está disponible se recurre a `ps -o lstart=`.
  Límite: si la identidad no se puede leer (p. ej. EPERM con procesos de otro usuario),
  solo se comprueba que el pid exista (`kill(pid, 0)`).
- El hook de Claude se instala como `sh -c '<python> … || true'`: por el `|| true` el
  padre directo es `sh` (en macOS su comm es `bash`) y Claude es el abuelo. Se vigilan
  ambos.
- Lector de stdout: kqueue EVFILT_WRITE con EV_EOF (verificado en macOS para tuberías y
  socketpair); si no hay kqueue, poll() con POLLHUP/POLLERR.
"""
import ctypes
import os
import select
import subprocess

from .store import pid_alive

SHELLS = frozenset(['sh', 'bash', 'zsh', 'dash', 'ksh'])
PROC_PIDTBSDINFO = 3


class _BsdInfo(ctypes.Structure):
    _fields_ = [('flags', ctypes.c_uint32), ('status', ctypes.c_uint32), ('xstatus', ctypes.c_uint32),
                ('pid', ctypes.c_uint32), ('ppid', ctypes.c_uint32), ('uid', ctypes.c_uint32),
                ('gid', ctypes.c_uint32), ('ruid', ctypes.c_uint32), ('rgid', ctypes.c_uint32),
                ('svuid', ctypes.c_uint32), ('svgid', ctypes.c_uint32), ('rfu', ctypes.c_uint32),
                ('comm', ctypes.c_char * 16), ('name', ctypes.c_char * 32), ('nfiles', ctypes.c_uint32),
                ('pgid', ctypes.c_uint32), ('pjobc', ctypes.c_uint32), ('tdev', ctypes.c_uint32),
                ('tpgid', ctypes.c_uint32), ('nice', ctypes.c_int32), ('start_sec', ctypes.c_uint64),
                ('start_usec', ctypes.c_uint64)]


_LIBPROC = []


def _libproc():
    if not _LIBPROC:
        try:
            _LIBPROC.append(ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True))
        except OSError:
            _LIBPROC.append(None)
    return _LIBPROC[0]


def _info_libproc(lib, pid):
    info = _BsdInfo()
    size = lib.proc_pidinfo(ctypes.c_int(pid), PROC_PIDTBSDINFO, ctypes.c_uint64(0),
                            ctypes.byref(info), ctypes.sizeof(info))
    if size != ctypes.sizeof(info):
        return None
    return dict(pid=pid, ppid=int(info.ppid), comm=info.comm.decode('utf-8', 'replace'),
                start='%d.%06d' % (info.start_sec, info.start_usec))


def _info_ps(pid):
    try:
        run = subprocess.run(['/bin/ps', '-o', 'ppid=,lstart=,comm=', '-p', str(pid)],
                             capture_output=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    parts = run.stdout.decode('utf-8', 'replace').split()
    if run.returncode != 0 or len(parts) < 7:
        return None
    return dict(pid=pid, ppid=int(parts[0]), comm=os.path.basename(' '.join(parts[6:])),
                start=' '.join(parts[1:6]))


def info(pid):
    """{pid, ppid, comm, start} o None si no se puede leer."""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    lib = _libproc()
    if lib is not None:
        return _info_libproc(lib, pid)
    return _info_ps(pid)


def identity(pid):
    """Identidad estable de `pid`: {"pid", "start"} (start None si no se puede leer)."""
    current = info(pid)
    return dict(pid=pid, start=current['start'] if current else None)


def same_process(pid, start):
    """True si `pid` existe y, cuando se conoce, su hora de inicio coincide."""
    if not pid_alive(pid):
        return False
    if start is None:
        return True
    current = info(pid)
    if current is None:
        return pid_alive(pid)  # Identidad ilegible: solo cuenta la existencia.
    return current['start'] == start


def host_chain(parent_pid):
    """Procesos de los que depende la espera: el padre y, si es un shell, el abuelo."""
    parent = info(parent_pid)
    chain = [dict(pid=parent_pid, start=parent['start'] if parent else None)]
    if parent and parent['comm'] in SHELLS and parent['ppid'] > 1:
        grand = info(parent['ppid'])
        chain.append(dict(pid=parent['ppid'], start=grand['start'] if grand else None))
    return chain


def chain_alive(chain):
    return all(same_process(p['pid'], p['start']) for p in chain)


class StdoutWatch:
    """Detecta que el lector de `fd` (stdout del hook) se ha cerrado."""

    def __init__(self, fd=1):
        self.fd = fd
        self.kq = None
        try:
            self.kq = select.kqueue()
            self.kq.control([select.kevent(fd, select.KQ_FILTER_WRITE,
                                           select.KQ_EV_ADD | select.KQ_EV_ENABLE)], 0, 0)
        except (AttributeError, OSError, ValueError):
            if self.kq is not None:
                self.kq.close()
            self.kq = None

    def closed(self):
        try:
            if self.kq is not None:
                for event in self.kq.control(None, 4, 0):
                    if event.flags & select.KQ_EV_ERROR:
                        return False
                    if event.flags & select.KQ_EV_EOF:
                        return True
                return False
            poller = select.poll()
            poller.register(self.fd, select.POLLOUT)
            return any(mask & (select.POLLHUP | select.POLLERR) for _, mask in poller.poll(0))
        except (AttributeError, OSError, ValueError):
            return False

    def close(self):
        if self.kq is not None:
            self.kq.close()
            self.kq = None
