"""Experimental private Desktop IPC contract, pinned to app 26.1103.41825.

Only owner discovery and canonical async question replies are exposed. An IPC
acknowledgement is not evidence that the question was accepted or continued.
See docs/research/CODEX_IPC.md for the inspected ASAR contract and its limits.
"""
import ctypes
import json
import math
import os
from pathlib import Path
import re
import socket
import stat
import struct
import sys
import time
import uuid

MAX_INBOUND_BYTES = 64 * 1024
MAX_OUTBOUND_BYTES = 1024 * 1024


class IPCError(RuntimeError):
    """No response body or conversation content is included in errors."""
    outcome_unknown = False


class IPCSecurityError(IPCError, PermissionError):
    pass


class IPCProtocolError(IPCError):
    pass


class IPCRemoteError(IPCError):
    def __init__(self, code):
        known = {'no-client-found', 'request-timeout', 'client-disconnected',
            'request-version-mismatch', 'no-handler-for-request', 'server-closed',
            'thread-role-timeout', 'thread-follower-response-method-mismatch'}
        self.remote_code = code if isinstance(code, str) and code in known else 'remote-error'
        super().__init__('IPC remote error: ' + self.remote_code)


class IPCTimeout(IPCError, TimeoutError):
    def __init__(self, outcome_unknown):
        self.outcome_unknown = outcome_unknown
        super().__init__('IPC deadline exceeded; outcome unknown' if outcome_unknown else 'IPC deadline exceeded')


def _uuid(value, label):
    if not isinstance(value, str):
        raise ValueError('Invalid ' + label)
    try:
        valid = str(uuid.UUID(value)) == value
    except ValueError:
        valid = False
    if not valid:
        raise ValueError('Invalid ' + label)
    return value


def _json(value):
    def unique(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise ValueError('Duplicate JSON key')
            result[key] = item
        return result

    def invalid_constant(_):
        raise ValueError('Invalid JSON constant')

    return json.loads(value, object_pairs_hook=unique, parse_constant=invalid_constant)


def _native_reply(envelope):
    prefix = '<send_user_message_question_reply>\n'
    suffix = '\n</send_user_message_question_reply>'
    try:
        if not isinstance(envelope, str) or not envelope.startswith(prefix) or not envelope.endswith(suffix):
            raise ValueError
        replies = _json(envelope[len(prefix):-len(suffix)])
        if not isinstance(replies, list) or not replies:
            raise ValueError
        seen = set()
        for reply in replies:
            if (not isinstance(reply, dict) or set(reply) != {'questionItemId', 'question', 'answer'}
                    or any(not isinstance(value, str) or not value.strip() for value in reply.values())):
                raise ValueError
            question_id = reply['questionItemId']
            key = _json(question_id)
            if (not isinstance(key, list) or len(key) != 3 or key[0] != 'request_user_input_async'
                    or not isinstance(key[1], str) or not key[1].strip()
                    or type(key[2]) is not int or key[2] < 0 or question_id in seen):
                raise ValueError
            seen.add(question_id)
        envelope.encode('utf-8')
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ValueError('Invalid canonical question reply') from None


class DesktopIPC:
    """One fresh unregistered connection per operation; no retries or fallback.

    The caller owns consent, question/session correlation, version checks and
    the active/finished-turn decision. This module never claims acceptance.
    """
    def __init__(self, socket_path=None, timeout=5, client_id=None):
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or timeout <= 0):
            raise ValueError('Invalid IPC timeout')
        if client_id is None:
            client_id = 'desktop-questions-ipc-' + str(uuid.uuid4())
        if (not isinstance(client_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', client_id)
                or client_id.lower().startswith(('codex', 'vscode', 'cursor'))
                or client_id.lower() in {'desktop', 'initializing-client'}):
            raise ValueError('Use an independent IPC client identity')
        self.socket_path = Path(socket_path) if socket_path is not None else (
            Path.home() / '.codex' / 'ipc' / 'ipc.sock')
        self.timeout = timeout
        self.client_id = client_id

    def discover_owner(self, thread_id):
        """Return the owner UUID for exactly one already-known local thread."""
        _uuid(thread_id, 'thread ID')
        reply = self._exchange('thread-owner-discovery', 1,
            {'hostId': 'local', 'conversationId': thread_id})
        if (not isinstance(reply.get('result'), dict)
                or reply['result'].get('supportsUntrustedAppInput') is not True):
            raise IPCProtocolError('Invalid owner discovery capability')
        return reply['handledByClientId']

    def send_reply(self, thread_id, cwd, envelope, message_id, owner_id, mode='steer'):
        """Send a canonical async reply once and return its correlated raw ack.

        `mode='start'` must only be selected after the caller observed the
        original turn Stop. Any failure after attempted sending can have
        `outcome_unknown=True`; callers must preserve the reply and not resend.
        """
        _uuid(thread_id, 'thread ID')
        _uuid(message_id, 'message ID')
        _uuid(owner_id, 'owner ID')
        if not isinstance(cwd, str) or not Path(cwd).is_absolute() or '\0' in cwd:
            raise ValueError('Invalid absolute working directory')
        if mode not in ('steer', 'start'):
            raise ValueError('Choose steer or start explicitly')
        _native_reply(envelope)
        restore = dict(id=message_id, text=envelope, cwd=cwd,
            createdAt=int(time.time() * 1000), responsesapiClientMetadata={'source': 'codex'},
            context=dict(prompt=envelope, turnTrigger='send_user_message_async_question',
                addedFiles=[], fileAttachments=[], ideContext=None, imageAttachments=[],
                workspaceRoots=[cwd]))
        params = dict(conversationId=thread_id,
            input=[dict(type='text', text=envelope+'\n', text_elements=[])],
            clientUserMessageId=message_id, attachments=[], restoreMessage=restore)
        if mode == 'start':
            params = dict(conversationId=thread_id, turnStart=dict(
                request=dict(threadId=thread_id, turnTrigger='send_user_message_async_question',
                    input=params['input'], clientUserMessageId=message_id,
                    responsesapiClientMetadata={'source': 'codex'}, model=None, effort=None),
                context=dict(restoreMessage=restore, attachments=[], commentAttachments=[],
                    localTurnMetadata={'fileAttachmentCount': 0}, useAppServerPermissionDefault=True,
                    inheritThreadSettings=True, writingBlockContextPrepared=False, responseItems=[])))
            return self._exchange('thread-follower-start-turn', 2, params, owner_id)
        return self._exchange('thread-follower-steer-turn', 1, params, owner_id)

    def _exchange(self, method, version, params, owner_id=None):
        request = dict(type='request', requestId=str(uuid.uuid4()),
            sourceClientId=self.client_id, version=version, method=method,
            params=params, timeoutMs=max(1, int(self.timeout * 1000)))
        if owner_id is not None:
            request['targetClientId'] = owner_id
        data = json.dumps(request, ensure_ascii=False, allow_nan=False).encode('utf-8')
        if len(data) > MAX_OUTBOUND_BYTES:
            raise IPCProtocolError('IPC outbound frame exceeds limit')
        attempted = False
        deadline = time.monotonic() + self.timeout
        try:
            metadata = self.socket_path.lstat()
            if (not stat.S_ISSOCK(metadata.st_mode) or metadata.st_uid != os.getuid()
                    or stat.S_IMODE(metadata.st_mode) != 0o600):
                raise IPCSecurityError('Unsafe IPC socket')
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
                self._set_deadline(peer, deadline)
                peer.connect(str(self.socket_path))
                self._check_peer(peer)
                current = self.socket_path.lstat()
                if (current.st_dev, current.st_ino, current.st_uid, current.st_mode) != (
                        metadata.st_dev, metadata.st_ino, metadata.st_uid, metadata.st_mode):
                    raise IPCSecurityError('IPC socket changed during connection')
                self._set_deadline(peer, deadline)
                attempted = owner_id is not None
                peer.sendall(struct.pack('<I', len(data)) + data)
                size = struct.unpack('<I', self._read_exact(peer, 4, deadline))[0]
                if not 0 < size <= MAX_INBOUND_BYTES:
                    raise IPCProtocolError('IPC inbound frame exceeds limit or is empty')
                try:
                    reply = _json(self._read_exact(peer, size, deadline).decode('utf-8'))
                except (ValueError, UnicodeError, RecursionError):
                    raise IPCProtocolError('Malformed IPC response') from None
                if (not isinstance(reply, dict) or reply.get('type') != 'response'
                        or reply.get('requestId') != request['requestId']):
                    raise IPCProtocolError('Unexpected IPC response')
                # Router errors omit method/handledByClientId. If present they
                # must still agree; no unrelated response is ever accepted.
                if 'method' in reply and reply['method'] != method:
                    raise IPCProtocolError('IPC method mismatch')
                if owner_id is not None and 'handledByClientId' in reply and reply['handledByClientId'] != owner_id:
                    raise IPCProtocolError('IPC owner mismatch')
                if reply.get('resultType') == 'error':
                    raise IPCRemoteError(reply.get('error'))
                if (reply.get('method') != method or reply.get('resultType') != 'success'
                        or not isinstance(reply.get('result'), dict)):
                    raise IPCProtocolError('Unexpected IPC response')
                try:
                    _uuid(reply.get('handledByClientId'), 'owner ID')
                except ValueError:
                    raise IPCProtocolError('Invalid IPC owner') from None
                peer.setblocking(False)
                try:
                    if peer.recv(1, socket.MSG_PEEK):
                        raise IPCProtocolError('Unexpected extra IPC data')
                except BlockingIOError:
                    pass
                return reply
        except TimeoutError:
            raise IPCTimeout(attempted) from None
        except IPCError as error:
            error.outcome_unknown = attempted
            raise
        except OSError:
            error = IPCError('IPC connection failed')
            error.outcome_unknown = attempted
            raise error from None

    @staticmethod
    def _check_peer(peer):
        if hasattr(peer, 'getpeereid'):
            uid, _ = peer.getpeereid()
        elif sys.platform == 'darwin':
            uid_value, gid_value = ctypes.c_uint(), ctypes.c_uint()
            try:
                check = ctypes.CDLL(None, use_errno=True).getpeereid
                check.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_uint)]
                check.restype = ctypes.c_int
                if check(peer.fileno(), ctypes.byref(uid_value), ctypes.byref(gid_value)) != 0:
                    raise IPCSecurityError('Unable to verify IPC peer')
            except (OSError, AttributeError):
                raise IPCSecurityError('Unable to verify IPC peer') from None
            uid = uid_value.value
        elif hasattr(socket, 'SO_PEERCRED'):
            _, uid, _ = struct.unpack('3i', peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        else:
            raise IPCSecurityError('IPC peer identity is unavailable')
        if uid != os.getuid():
            raise IPCSecurityError('IPC peer belongs to another user')

    @staticmethod
    def _set_deadline(peer, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        peer.settimeout(remaining)

    @classmethod
    def _read_exact(cls, peer, size, deadline):
        data = bytearray()
        while len(data) < size:
            cls._set_deadline(peer, deadline)
            chunk = peer.recv(size - len(data))
            if not chunk:
                raise IPCProtocolError('IPC connection closed before response completed')
            data.extend(chunk)
        return bytes(data)
