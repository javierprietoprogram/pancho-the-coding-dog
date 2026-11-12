"""Origen de la conversación (para abrirla desde Pancho): id de la sesión de escritorio y terminal, nada más del entorno."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / 'scripts' / 'pancho_hook.py'


class SessionOriginTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name) / 'Pancho'
        base = {k: v for k, v in os.environ.items() if not k.startswith(('CLAUDE', 'TERM_PROGRAM'))}
        self.env = dict(base, PANCHO_HOME=str(self.home), HOME=str(Path(tmp.name) / 'home'))

    def hook(self, provider, env, **payload):
        payload = dict(dict(hook_event_name='UserPromptSubmit', session_id='S1', cwd='/Users/x/mascota', prompt='hola'), **payload)
        run = subprocess.run([sys.executable, '-I', str(HOOK), 'hook', '--provider', provider, '--owner', 'pancho-v1'],
                             input=json.dumps(payload).encode(), capture_output=True, env=dict(self.env, **env), timeout=20)
        self.assertEqual(run.returncode, 0, run.stderr)
        files = list((self.home / 'v1' / 'sessions').glob('*.json'))
        self.assertEqual(len(files), 1)
        return json.loads(files[0].read_text())

    def test_claude_desktop_session_is_recorded(self):
        r = self.hook('claude', dict(CLAUDE_CODE_HOST_SESSION_ID='local_dd312fe7-76e2-4825-bfe5-09aa18cf847d',
                                     CLAUDE_CODE_ENTRYPOINT='claude-desktop', CLAUDE_CODE_OAUTH_TOKEN='SECRETO',
                                     CLAUDE_CODE_USER_EMAIL='a@b.c'))
        self.assertEqual((r['host_session'], r['entrypoint']), ('local_dd312fe7-76e2-4825-bfe5-09aa18cf847d', 'claude-desktop'))
        text = json.dumps(r)
        self.assertNotIn('SECRETO', text)
        self.assertNotIn('a@b.c', text)

    def test_claude_terminal_records_terminal_app(self):
        r = self.hook('claude', dict(CLAUDE_CODE_ENTRYPOINT='cli', TERM_PROGRAM='iTerm.app'))
        self.assertEqual((r.get('host_session'), r['entrypoint'], r['term_program']), (None, 'cli', 'iTerm.app'))

    def test_bad_values_are_dropped(self):
        r = self.hook('claude', dict(CLAUDE_CODE_HOST_SESSION_ID='local_../../x;rm', CLAUDE_CODE_ENTRYPOINT='x' * 300,
                                     TERM_PROGRAM='Programa Raro'))
        self.assertIsNone(r.get('host_session'))
        self.assertIsNone(r.get('entrypoint'))
        self.assertIsNone(r.get('term_program'))

    def test_origin_is_kept_on_later_events_without_env(self):
        self.hook('claude', dict(CLAUDE_CODE_HOST_SESSION_ID='local_abc', CLAUDE_CODE_ENTRYPOINT='claude-desktop'))
        r = self.hook('claude', {}, hook_event_name='Stop')
        self.assertEqual(r['host_session'], 'local_abc')

    def test_nested_cli_does_not_inherit_desktop_session_and_resume_updates_origin(self):
        self.hook('claude', dict(CLAUDE_CODE_HOST_SESSION_ID='local_abc', CLAUDE_CODE_ENTRYPOINT='claude-desktop'))
        r = self.hook('claude', dict(CLAUDE_CODE_HOST_SESSION_ID='local_abc', CLAUDE_CODE_ENTRYPOINT='cli',
                                     TERM_PROGRAM='Apple_Terminal'))
        self.assertEqual((r.get('host_session'), r['entrypoint'], r['term_program']), (None, 'cli', 'Apple_Terminal'))

    def test_codex_records_no_claude_origin(self):
        r = self.hook('codex', dict(CLAUDE_CODE_HOST_SESSION_ID='local_abc', TERM_PROGRAM='Apple_Terminal'),
                      turn_id='t', model='m', permission_mode='default', transcript_path=None)
        self.assertIsNone(r.get('host_session'))


if __name__ == '__main__':
    unittest.main()
