"""Instalador Codex de Pancho (T2) sobre un HOME temporal: nunca el ~/.codex real.

La PoC Codex de mascota se reproduce en una copia temporal con su propio instalador
(`src/codex_installation.py`), igual que el test del instalador Claude hace con la PoC
Claude. `config.toml` (con `hooks.state`) se deja ilegible para demostrar que ni la
instalación ni `status` lo leen.
"""
import hashlib
import json
import os
from pathlib import Path
import plistlib
import struct
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / 'scripts' / 'pancho_install.py'
sys.path.insert(0, str(ROOT / 'src'))
from pancho_bridge import codex_install, installer  # noqa: E402

EVENTS = ['SessionStart', 'UserPromptSubmit', 'PreToolUse', 'PostToolUse', 'Stop', 'SessionEnd',
          'SubagentStart', 'SubagentStop']
POC_OWNER = 'desktop-questions-codex-poc-v1'
FOREIGN = (b'{\n "hooks": {"PreToolUse": [{"matcher": "^Bash$", "hooks": [{"type": "command", '
           b'"command": "echo ajeno", "timeout": 5}]}],\n  "Stop": [{"hooks": [{"type": "command", '
           b'"command": "say fin"}]}]},\n  "description": "mis hooks"\n}   \n')
TOML = b'[hooks.state]\n"secreto" = "no-leer"\n'


class PanchoCodexInstallerTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.user_home = self.tmp / 'home'
        self.root = self.user_home / 'Library' / 'Application Support' / 'Pancho-test'
        self.codex_dir = self.user_home / '.codex'
        self.codex_dir.mkdir(parents=True)
        self.hooks_path = self.codex_dir / 'hooks.json'
        self.toml = self.codex_dir / 'config.toml'
        self.toml.write_bytes(TOML)
        os.chmod(str(self.toml), 0o000)  # Si alguien lo leyera, fallaría.
        self.addCleanup(os.chmod, str(self.toml), 0o600)
        self.settings = self.user_home / '.claude' / 'settings.json'
        self.poc = self.tmp / 'mascota-copia'
        self.poc.mkdir()
        self.env = dict(os.environ, HOME=str(self.user_home), PANCHO_HOME=str(self.root))
        self.pin_source = self.make_app()

    def make_app(self, interface=(b'thread-owner-discovery', b'thread-follower-steer-turn', b'thread-follower-start-turn',
                                  b'send_user_message_question_reply', b'request_user_input_async')):
        app = self.tmp / 'ChatGPT.app'
        (app / 'Contents' / 'Resources').mkdir(parents=True, exist_ok=True)
        (app / 'Contents' / 'Info.plist').write_bytes(plistlib.dumps(
            {'CFBundleIdentifier': 'com.openai.codex', 'CFBundleShortVersionString': '26.1103.41825'}))
        body = b'modulo;' + b';'.join(b'"%s"' % n for n in interface)
        header = json.dumps({'files': {'.vite': {'files': {'build': {'files': {
            'early-bootstrap-1.js': {'size': len(body), 'offset': '0'}}}}}}}).encode()
        header += b' ' * ((4 - len(header) % 4) % 4)
        pickle = struct.pack('<I', len(header)) + header
        asar = app / 'Contents' / 'Resources' / 'app.asar'
        asar.write_bytes(struct.pack('<III', 4, len(pickle) + 4, len(pickle)) + pickle + body)
        # Pin antiguo de la PoC: se sigue aceptando como origen, pero ya no fija versión ni hashes.
        source = self.tmp / 'app-pin.json'
        source.write_text(json.dumps(dict(archive_path=str(asar), data_offset=16, files=[
            dict(path='a.js', size=1, unpacked=False, offset='0', sha256=hashlib.sha256(b'x').hexdigest())])))
        self.app = app
        return source

    def cli(self, *args):
        command = [sys.executable, str(INSTALL)] + list(args) + [
            '--codex', '--home', str(self.root), '--codex-hooks', str(self.hooks_path),
            '--poc-root', str(self.poc), '--claude-settings', str(self.settings),
            '--codex-pin', str(self.pin_source), '--codex-app', str(self.app)]
        result = subprocess.run(command, capture_output=True, env=self.env, timeout=120)
        return result.returncode, json.loads(result.stdout.decode('utf-8'))

    def ours(self):
        hooks = json.loads(self.hooks_path.read_text())['hooks']
        return {e: [g for g in groups if any('--provider codex' in h['command'] for h in g['hooks'])]
                for e, groups in hooks.items()}

    def make_poc(self, foreign=True):
        if not (ROOT / 'src' / 'codex_installation.py').exists():
            self.skipTest('Sin la PoC original (no se publica en el repositorio)')
        (self.poc / 'src').mkdir()
        for name in ('codex_installation.py', 'bridge.py'):
            shutil.copyfile(str(ROOT / 'src' / name), str(self.poc / 'src' / name))
        code = 'import sys;sys.path.insert(0,sys.argv[1]);from codex_installation import install;install(sys.argv[2])'
        subprocess.run([sys.executable, '-I', '-c', code, str(self.poc / 'src'), str(self.poc)],
                       check=True, capture_output=True)
        config = self.poc / '.codex' / 'hooks.json'
        if foreign:
            data = json.loads(config.read_text())
            data['hooks']['Stop'].append({'hooks': [{'type': 'command', 'command': 'echo ajeno-local'}]})
            config.write_text(json.dumps(data))
        return config

    def toml_untouched(self):
        os.chmod(str(self.toml), 0o600)
        self.assertEqual(self.toml.read_bytes(), TOML)
        os.chmod(str(self.toml), 0o000)

    # --- pruebas ---------------------------------------------------------------------
    def test_fresh_install_records_absence_and_installs_stable_working_hooks(self):
        code, result = self.cli('install')
        self.assertEqual(code, 0, result)
        self.assertEqual(Path(result['backup']).name, 'codex-hooks-original-absent.json')
        self.assertFalse(json.loads(Path(result['backup']).read_text())['existed'])
        ours = self.ours()
        self.assertEqual(sorted(ours), sorted(EVENTS))
        expected_command = ' '.join(shlex.quote(str(p)) for p in [
            Path(sys.executable).absolute(), '-I', self.root / 'bridge' / 'pancho_hook.py', 'hook', '--provider',
            'codex', '--owner', 'pancho-v1', '--home', self.root]) + ' || true'
        self.assertIn("'%s/bridge/pancho_hook.py'" % self.root, expected_command)  # La raíz tiene espacios.
        for event in EVENTS:
            self.assertEqual(len(ours[event]), 1)
            hook = ours[event][0]['hooks'][0]
            self.assertEqual(hook['command'], expected_command)
            self.assertNotIn('bridge-versions', hook['command'])
            self.assertEqual(hook['timeout'], {'SessionEnd': 3, 'Stop': 30}.get(event, 10))
            if event in ('PreToolUse', 'PostToolUse'):
                self.assertEqual(ours[event][0]['matcher'], '^request_user_input_async$')
            else:
                self.assertNotIn('matcher', ours[event][0])
        self.assertEqual(sorted(result['review']['entries']), sorted(EVENTS))
        self.assertIn('Review hooks', result['review']['how'])
        pin = json.loads((self.root / 'install' / 'codex-app-pin.json').read_text())
        self.assertEqual((pin['mode'], pin['app_path'], pin['bundle_id']), ('interface', str(self.app), 'com.openai.codex'))
        self.assertNotIn('expected_app_version', pin)
        self.assertNotIn('files', pin)
        self.assertEqual(result['pin_check'], 'ok')
        # El comando funciona desde cualquier cwd y registra la sesión Codex.
        env = dict(self.env)
        env.pop('PANCHO_HOME')
        run = subprocess.run(['/bin/sh', '-c', expected_command], input=json.dumps(dict(
            hook_event_name='UserPromptSubmit', session_id='s-codex', turn_id='t', cwd='/p/nuevo',
            prompt='secreto')).encode(), capture_output=True, env=env, cwd=str(self.tmp), timeout=20)
        self.assertEqual((run.returncode, run.stdout), (0, b''))
        sessions = [json.loads(p.read_text()) for p in (self.root / 'v1' / 'sessions').glob('*.json')]
        self.assertEqual([(s['provider'], s['activity']) for s in sessions], [('codex', 'working')])
        self.toml_untouched()

    def test_existing_hooks_exact_backup_idempotent_and_command_stable_across_versions(self):
        self.hooks_path.write_bytes(FOREIGN)
        os.chmod(str(self.hooks_path), 0o644)
        code, first = self.cli('install')
        self.assertEqual(code, 0, first)
        self.assertEqual(Path(first['backup']).read_bytes(), FOREIGN)
        self.assertEqual(self.hooks_path.stat().st_mode & 0o777, 0o644)
        data = json.loads(self.hooks_path.read_text())
        self.assertEqual(data['description'], 'mis hooks')
        self.assertEqual(data['hooks']['PreToolUse'][0], json.loads(FOREIGN)['hooks']['PreToolUse'][0])
        self.assertEqual(data['hooks']['Stop'][0], json.loads(FOREIGN)['hooks']['Stop'][0])
        before = self.hooks_path.read_bytes()
        target = os.readlink(str(self.root / 'bridge'))
        code, again = self.cli('install')  # Actualización de Pancho: nueva versión del puente.
        self.assertEqual(code, 0, again)
        self.assertTrue(again['already_installed'])
        self.assertEqual(again['review']['entries'], [])
        self.assertEqual(self.hooks_path.read_bytes(), before)  # Mismo hash: no hay que revisar otra vez.
        self.assertNotEqual(os.readlink(str(self.root / 'bridge')), target)
        self.assertEqual({e: len(v) for e, v in self.ours().items() if v}, {e: 1 for e in EVENTS})
        code, removed = self.cli('uninstall')
        self.assertEqual(code, 0, removed)
        self.assertEqual(json.loads(self.hooks_path.read_text()), json.loads(FOREIGN))

    def test_agents_panel_upgrade_extends_codex_and_asks_review_only_for_new_entries(self):
        self.hooks_path.write_bytes(FOREIGN)
        old_events = tuple(e for e in codex_install.EVENTS if e[0] not in ('SubagentStart', 'SubagentStop'))
        with mock.patch.object(codex_install, 'EVENTS', old_events):
            codex_install.install(home=self.root, hooks_path=self.hooks_path, poc_root=self.poc,
                                  pin_source=self.pin_source, app_path=self.app)
        code, status = self.cli('status')
        self.assertEqual(code, 0, status)
        self.assertEqual(sorted(status['missing_events']), ['SubagentStart', 'SubagentStop'])
        before = self.hooks_path.read_bytes()
        code, result = self.cli('install')
        self.assertEqual(code, 0, result)
        self.assertEqual(sorted(result['extended_events']), ['SubagentStart', 'SubagentStop'])
        self.assertEqual(sorted(result['review']['entries']), ['SubagentStart', 'SubagentStop'])
        self.assertEqual(Path(result['extensions'][-1]['backup']).read_bytes(), before)
        data = json.loads(self.hooks_path.read_text())
        self.assertEqual(data['hooks']['Stop'][0], json.loads(FOREIGN)['hooks']['Stop'][0])  # Lo ajeno, intacto.
        self.assertEqual({e: len(v) for e, v in self.ours().items() if v}, {e: 1 for e in EVENTS})
        code, again = self.cli('install')
        self.assertEqual((code, again['review']['entries']), (0, []))
        self.assertEqual(self.cli('status')[1]['missing_events'], [])
        code, removed = self.cli('uninstall')
        self.assertEqual(code, 0, removed)
        self.assertEqual(json.loads(self.hooks_path.read_text()), json.loads(FOREIGN))

    def test_uninstall_keeps_changed_and_later_entries_and_deletes_only_created_file(self):
        code, result = self.cli('install')
        data = json.loads(self.hooks_path.read_text())
        data['hooks']['Stop'][0]['hooks'][0]['timeout'] = 99  # Cambio posterior de Javier.
        data['hooks']['UserPromptSubmit'].append({'hooks': [{'type': 'command', 'command': 'echo luego'}]})
        self.hooks_path.write_text(json.dumps(data))
        code, removed = self.cli('uninstall')
        self.assertEqual(code, 0, removed)
        self.assertEqual(removed['retained_modified_or_missing'], ['Stop'])
        after = json.loads(self.hooks_path.read_text())
        self.assertEqual(sorted(after['hooks']), ['Stop', 'UserPromptSubmit'])
        self.assertEqual(after['hooks']['UserPromptSubmit'], [{'hooks': [{'type': 'command', 'command': 'echo luego'}]}])
        self.assertEqual(after['hooks']['Stop'][0]['hooks'][0]['timeout'], 99)
        # Instalación limpia y retirada: el archivo creado por Pancho desaparece.
        self.hooks_path.unlink()
        code, result = self.cli('install')
        self.assertEqual(code, 0, result)
        code, removed = self.cli('uninstall')
        self.assertFalse(self.hooks_path.exists())
        self.assertTrue(Path(result['backup']).exists())
        self.toml_untouched()

    def test_poc_hooks_block_install_until_migrated_selectively(self):
        config = self.make_poc()
        poc_manifest = (self.poc / '.local-test' / 'codex' / 'install.json').read_bytes()
        backups = sorted(str(p) for p in (self.poc / '.local-test' / 'codex' / 'backups').rglob('*'))
        before = config.read_bytes()
        code, refused = self.cli('install')
        self.assertEqual(code, 3)
        self.assertIn('--migrate-poc', refused['error'])
        self.assertFalse(self.hooks_path.exists())
        self.assertEqual(config.read_bytes(), before)
        code, result = self.cli('install', '--migrate-poc')
        self.assertEqual(code, 0, result)
        local = json.loads(config.read_text())
        self.assertNotIn(POC_OWNER, json.dumps(local))
        self.assertEqual(local, {'hooks': {'Stop': [{'hooks': [{'type': 'command', 'command': 'echo ajeno-local'}]}]}})
        control = json.loads((self.poc / '.local-test' / 'codex' / 'control.json').read_text())
        self.assertFalse(control['enabled'])
        self.assertEqual((self.poc / '.local-test' / 'codex' / 'install.json').read_bytes(), poc_manifest)
        self.assertEqual(sorted(str(p) for p in (self.poc / '.local-test' / 'codex' / 'backups').rglob('*')), backups)
        migration = result['poc_migrations'][0]
        self.assertEqual(sorted(migration['removed']), ['PostToolUse', 'PreToolUse', 'Stop', 'UserPromptSubmit'])
        self.assertFalse(migration['config_deleted'])
        self.assertEqual(sorted(self.ours()), sorted(EVENTS))
        code, status = self.cli('status')
        self.assertEqual(status['poc']['entries'], [])

    def test_poc_only_config_is_deleted_because_it_did_not_exist(self):
        config = self.make_poc(foreign=False)
        code, result = self.cli('install', '--migrate-poc')
        self.assertEqual(code, 0, result)
        self.assertFalse(config.exists())
        self.assertTrue(result['poc_migrations'][0]['config_deleted'])

    def test_poc_entries_that_differ_from_their_manifest_are_not_migrated(self):
        config = self.make_poc()
        data = json.loads(config.read_text())
        data['hooks']['Stop'][0]['hooks'][0]['timeout'] = 11
        config.write_text(json.dumps(data))
        before = config.read_bytes()
        code, refused = self.cli('install', '--migrate-poc')
        self.assertEqual(code, 3)
        self.assertEqual(config.read_bytes(), before)
        self.assertTrue(json.loads((self.poc / '.local-test' / 'codex' / 'control.json').read_text())['enabled'])
        self.assertFalse(self.hooks_path.exists())

    def test_status_lists_review_without_reading_trust_and_detects_pin_mismatch(self):
        self.cli('install')
        code, status = self.cli('status')
        self.assertEqual(code, 0, status)
        self.assertTrue(status['installed'])
        self.assertEqual(set(status['entries'].values()), {'presente'})
        self.assertEqual(sorted(status['review']['entries']), sorted(EVENTS))
        self.assertFalse(status['review']['trust_state_read'])
        self.assertEqual(status['pin_check'], 'ok')
        asar = self.app / 'Contents' / 'Resources' / 'app.asar'
        info = self.app / 'Contents' / 'Info.plist'
        info.write_bytes(plistlib.dumps({'CFBundleIdentifier': 'com.openai.codex', 'CFBundleShortVersionString': '27.0.0'}))
        self.assertEqual(self.cli('status')[1]['pin_check'], 'ok')  # Actualizar Codex no rompe nada.
        asar.write_bytes(asar.read_bytes().replace(b'thread-follower-steer-turn', b'thread-follower-steer-TURN'))
        code, status = self.cli('status')
        self.assertNotEqual(status['pin_check'], 'ok')
        self.assertTrue(any('Codex' in w for w in status['warnings']))
        self.toml_untouched()

    def test_claude_installation_is_left_untouched(self):
        run = subprocess.run([sys.executable, str(INSTALL), 'install', '--home', str(self.root),
                              '--claude-settings', str(self.settings), '--poc-root', str(self.poc)],
                             capture_output=True, env=self.env, timeout=120)
        self.assertEqual(run.returncode, 0, run.stdout)
        manifest = (self.root / 'install' / 'manifest.json').read_bytes()
        settings = self.settings.read_bytes()
        code, result = self.cli('install')
        self.assertEqual(code, 0, result)
        self.assertEqual((self.root / 'install' / 'manifest.json').read_bytes(), manifest)
        self.assertEqual(self.settings.read_bytes(), settings)
        command = json.loads(settings)['hooks']['Stop'][0]['hooks'][0]['command']
        env = dict(self.env)
        env.pop('PANCHO_HOME')
        run = subprocess.run(['/bin/sh', '-c', command], input=json.dumps(dict(
            hook_event_name='Stop', session_id='s-claude', cwd='/p/x')).encode(), capture_output=True,
            env=env, timeout=20)
        self.assertEqual((run.returncode, run.stdout), (0, b''))
        providers = sorted(json.loads(p.read_text())['provider'] for p in (self.root / 'v1' / 'sessions').glob('*.json'))
        self.assertEqual(providers, ['claude'])
        code, removed = self.cli('uninstall')
        self.assertEqual(self.settings.read_bytes(), settings)

    # --- revisión r1 ---------------------------------------------------------------------
    def test_m3_reinstall_is_idempotent_with_a_preexisting_identical_entry(self):
        stop = codex_install.build_entries(Path(sys.executable).absolute(), self.root)['Stop']
        self.hooks_path.write_text(json.dumps({'hooks': {'Stop': [stop]}}))
        code, first = self.cli('install')
        self.assertEqual(code, 0, first)
        self.assertEqual(first['preexisting_identical_entries'], ['Stop'])
        before = self.hooks_path.read_bytes()
        code, again = self.cli('install')
        self.assertEqual(code, 0, again)
        self.assertTrue(again['already_installed'])
        self.assertEqual(self.hooks_path.read_bytes(), before)

    def test_m5_failed_bridge_deploy_leaves_poc_untouched(self):
        config = self.make_poc()
        before = config.read_bytes()
        control = (self.poc / '.local-test' / 'codex' / 'control.json').read_bytes()
        with mock.patch.object(installer, '_deploy_bridge', side_effect=installer.InstallError('copia rota')):
            with self.assertRaises(installer.InstallError):
                codex_install.install(self.root, self.hooks_path, self.poc, True, sys.executable,
                                      str(self.pin_source), str(self.app))
        self.assertEqual(config.read_bytes(), before)
        self.assertEqual((self.poc / '.local-test' / 'codex' / 'control.json').read_bytes(), control)
        self.assertFalse(self.hooks_path.exists())

    def test_m5_failed_global_write_restores_the_migrated_poc(self):
        config = self.make_poc()
        before = config.read_bytes()
        control = (self.poc / '.local-test' / 'codex' / 'control.json').read_bytes()
        self.hooks_path.write_text('{"hooks": {}}')
        real = codex_install._write_settings

        def write(path, data, expected, mode):
            if Path(path) == self.hooks_path:  # Cambio concurrente justo al publicar los globales.
                self.hooks_path.write_text('{"hooks": {}, "concurrente": "conservar"}')
            return real(path, data, expected, mode)
        with mock.patch.object(codex_install, '_write_settings', side_effect=write):
            with self.assertRaises(installer.InstallError) as raised:
                codex_install.install(self.root, self.hooks_path, self.poc, True, sys.executable,
                                      str(self.pin_source), str(self.app))
        self.assertIn('PoC restaurada', str(raised.exception))
        self.assertEqual(json.loads(self.hooks_path.read_text()), {'hooks': {}, 'concurrente': 'conservar'})
        self.assertEqual(json.loads(config.read_text()), json.loads(before))
        self.assertTrue(json.loads((self.poc / '.local-test' / 'codex' / 'control.json').read_text())['enabled'])
        self.assertEqual(json.loads((self.poc / '.local-test' / 'codex' / 'control.json').read_text()),
                         json.loads(control))
        self.assertFalse(json.loads((self.root / 'install' / 'codex-manifest.json').read_text())['active'])

    def test_concurrent_change_aborts_without_overwriting(self):
        self.hooks_path.write_text('{"hooks": {}}')
        real = installer._deploy_bridge

        def deploy_and_edit(*args, **kw):
            value = real(*args, **kw)
            self.hooks_path.write_text('{"hooks": {}, "concurrente": "conservar"}')
            return value
        with mock.patch.object(installer, '_deploy_bridge', side_effect=deploy_and_edit):
            with self.assertRaises(installer.ConcurrentChange):
                codex_install.install(self.root, self.hooks_path, None, False, sys.executable,
                                      str(self.pin_source), str(self.app))
        self.assertEqual(json.loads(self.hooks_path.read_text()), {'hooks': {}, 'concurrente': 'conservar'})
        manifest = json.loads((self.root / 'install' / 'codex-manifest.json').read_text())
        self.assertFalse(manifest['active'])


if __name__ == '__main__':
    unittest.main()
