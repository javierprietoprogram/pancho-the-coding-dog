"""Instalador global de Pancho sobre un HOME temporal (nunca el real)."""
import ast
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / 'scripts' / 'pancho_install.py'
sys.path.insert(0, str(ROOT / 'src'))
from pancho_bridge import installer  # noqa: E402

EVENTS = ['SessionStart', 'UserPromptSubmit', 'PreToolUse', 'PostToolUse', 'PostToolUseFailure', 'Stop',
          'StopFailure', 'SessionEnd', 'PermissionRequest', 'SubagentStart', 'SubagentStop']
PYTHON313 = Path('/opt/homebrew/opt/python@3.13/bin/python3.13')
FOREIGN = (b'{\n    "model": "opus",\n  "permissions": {"allow": ["Bash(ls)"]},\n'
           b'  "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "echo ajeno"}]}],\n'
           b'            "Notification": [{"hooks": [{"type": "command", "command": "say hola"}]}]}\n}   \n')


class PanchoInstallerTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.user_home = self.tmp / 'home'
        self.user_home.mkdir()
        self.root = self.user_home / 'Library' / 'Application Support' / 'Pancho-test'
        self.settings = self.user_home / '.claude' / 'settings.json'
        self.poc = self.tmp / 'mascota-copia'
        self.env = dict(os.environ, HOME=str(self.user_home), PANCHO_HOME=str(self.root))

    def cli(self, *args, poc=None, home=None):
        command = [sys.executable, str(INSTALL)] + list(args) + [
            '--home', str(home or self.root), '--claude-settings', str(self.settings), '--poc-root', str(poc or self.poc)]
        result = subprocess.run(command, capture_output=True, env=self.env, timeout=60)
        return result.returncode, json.loads(result.stdout.decode('utf-8'))

    def hooks(self):
        return json.loads(self.settings.read_text())['hooks']

    def ours(self):
        return {event: [g for g in groups if any('pancho-v1' in h['command'] for h in g['hooks'])]
                for event, groups in self.hooks().items()}

    # --- pruebas -------------------------------------------------------------------
    def test_fresh_install_records_absence_and_installs_working_hooks(self):
        code, result = self.cli('install')
        self.assertEqual(code, 0, result)
        backup = Path(result['backup'])
        self.assertEqual(backup.name, 'original-absent.json')
        self.assertFalse(json.loads(backup.read_text())['existed'])
        self.assertTrue(str(backup).startswith(str(self.root / 'backups')))
        ours = self.ours()
        self.assertEqual(sorted(ours), sorted(EVENTS))
        for event in EVENTS:
            self.assertEqual(len(ours[event]), 1)
            hook = ours[event][0]['hooks'][0]
            self.assertEqual(hook['timeout'], 1860 if event in ('PreToolUse', 'PermissionRequest', 'Stop') else 10)
            self.assertIn('--owner pancho-v1', hook['command'])
            self.assertTrue(hook['command'].endswith(' || true'))
            if event == 'PreToolUse':
                self.assertEqual(ours[event][0]['matcher'], installer.PRE_MATCHER)
            else:
                self.assertNotIn('matcher', ours[event][0])
        self.assertEqual(result['entries'], {e: self.hooks()[e][0] for e in EVENTS})
        self.assertEqual(result['python']['path'], str(Path(sys.executable).absolute()))
        self.assertTrue(result['python']['version'])
        self.assertTrue((self.root / 'bridge').is_symlink())
        self.assertEqual(self.root.stat().st_mode & 0o777, 0o700)
        # El comando instalado funciona sin el proyecto: evento real por sh.
        command = self.hooks()['UserPromptSubmit'][0]['hooks'][0]['command']
        env = dict(self.env)
        env.pop('PANCHO_HOME')
        run = subprocess.run(['/bin/sh', '-c', command], input=json.dumps(dict(
            hook_event_name='UserPromptSubmit', session_id='s', cwd='/p/nuevo', prompt='x')).encode(),
            capture_output=True, env=env, cwd=str(self.tmp), timeout=20)
        self.assertEqual((run.returncode, run.stdout), (0, b''))
        self.assertEqual(len(list((self.root / 'v1' / 'sessions').glob('*.json'))), 1)
        # Aunque el código desaparezca, el comando nunca devuelve 2.
        broken = command.replace('pancho_hook.py', 'no-existe.py')
        self.assertEqual(subprocess.run(['/bin/sh', '-c', broken], input=b'{}', capture_output=True).returncode, 0)

    def test_existing_settings_exact_backup_idempotent_and_foreign_preserved(self):
        self.settings.parent.mkdir()
        self.settings.write_bytes(FOREIGN)
        os.chmod(str(self.settings), 0o644)
        code, first = self.cli('install')
        self.assertEqual(code, 0, first)
        self.assertEqual(Path(first['backup']).read_bytes(), FOREIGN)
        self.assertEqual(self.settings.stat().st_mode & 0o777, 0o644)
        data = json.loads(self.settings.read_text())
        self.assertEqual((data['model'], data['permissions']), ('opus', {'allow': ['Bash(ls)']}))
        self.assertEqual(data['hooks']['PreToolUse'][0], {'matcher': 'Bash', 'hooks': [{'type': 'command', 'command': 'echo ajeno'}]})
        self.assertEqual(data['hooks']['Notification'], json.loads(FOREIGN)['hooks']['Notification'])
        before = self.settings.read_bytes()
        backups = sorted((self.root / 'backups').iterdir())
        code, again = self.cli('install')
        self.assertEqual(code, 0, again)
        self.assertTrue(again['already_installed'])
        self.assertEqual(self.settings.read_bytes(), before)
        self.assertEqual(sorted((self.root / 'backups').iterdir()), backups)
        self.assertEqual({e: len(v) for e, v in self.ours().items() if v}, {e: 1 for e in EVENTS})
        code, status = self.cli('status')
        self.assertEqual(set(status['entries'].values()), {'presente'})
        code, removed = self.cli('uninstall')
        self.assertEqual(code, 0, removed)
        self.assertEqual(json.loads(self.settings.read_text()), json.loads(FOREIGN))
        self.assertEqual(Path(first['backup']).read_bytes(), FOREIGN)

    def test_uninstall_keeps_entries_changed_by_third_parties_and_later_changes(self):
        code, result = self.cli('install')
        data = json.loads(self.settings.read_text())
        data['hooks']['Stop'][0]['hooks'][0]['timeout'] = 99  # Modificada por terceros.
        data['hooks']['Stop'].append({'hooks': [{'type': 'command', 'command': 'echo posterior'}]})
        data['nuevo'] = 'conservar'
        self.settings.write_text(json.dumps(data))
        code, removed = self.cli('uninstall')
        self.assertEqual(code, 0, removed)
        self.assertEqual(removed['retained_modified_or_missing'], ['Stop'])
        self.assertIn('Stop', removed['message'])
        after = json.loads(self.settings.read_text())
        self.assertEqual(after['nuevo'], 'conservar')
        self.assertEqual(sorted(after['hooks']), ['Stop'])
        self.assertEqual(len(after['hooks']['Stop']), 2)
        self.assertEqual(after['hooks']['Stop'][0]['hooks'][0]['timeout'], 99)
        self.assertTrue(Path(result['backup']).exists())
        code, again = self.cli('uninstall')
        self.assertFalse(again['removed'])

    def test_fresh_install_then_uninstall_removes_created_file_only(self):
        code, result = self.cli('install')
        code, removed = self.cli('uninstall')
        self.assertEqual(code, 0, removed)
        self.assertFalse(self.settings.exists())
        self.assertTrue(Path(result['backup']).exists())
        self.assertTrue((self.root / 'bridge' / 'pancho_hook.py').exists())

    def test_reinstall_is_idempotent_with_a_preexisting_identical_entry(self):
        stop = installer.build_entries(Path(sys.executable).absolute(), self.root)['Stop']
        self.settings.parent.mkdir()
        self.settings.write_text(json.dumps({'hooks': {'Stop': [stop]}}))
        code, first = self.cli('install')
        self.assertEqual(code, 0, first)
        self.assertEqual(first['preexisting_identical_entries'], ['Stop'])
        before = self.settings.read_bytes()
        code, again = self.cli('install')
        self.assertEqual(code, 0, again)
        self.assertTrue(again['already_installed'])
        self.assertEqual(self.settings.read_bytes(), before)

    def test_concurrent_change_aborts_without_overwriting(self):
        self.settings.parent.mkdir()
        self.settings.write_text('{"theme":"dark"}')
        real_deploy = installer._deploy_bridge

        def deploy_and_concurrent_edit(*args, **kw):
            result = real_deploy(*args, **kw)
            self.settings.write_text('{"theme":"dark","concurrente":"conservar"}')
            return result
        with mock.patch.object(installer, '_deploy_bridge', side_effect=deploy_and_concurrent_edit):
            with self.assertRaises(installer.ConcurrentChange):
                installer.install(self.root, self.settings, None, False, sys.executable)
        self.assertEqual(json.loads(self.settings.read_text()), {'theme': 'dark', 'concurrente': 'conservar'})
        manifest = json.loads((self.root / 'install' / 'manifest.json').read_text())
        self.assertFalse(manifest['active'])
        self.assertEqual(manifest['failure'], 'concurrent_configuration_change')

    def test_symlinked_or_invalid_settings_are_refused(self):
        self.settings.parent.mkdir()
        real = self.tmp / 'real.json'
        real.write_text('{}')
        self.settings.symlink_to(real)
        code, result = self.cli('install')
        self.assertEqual(code, 3)
        self.assertEqual(real.read_text(), '{}')
        self.settings.unlink()
        self.settings.write_text('{"hooks": []}')
        code, result = self.cli('install')
        self.assertEqual(code, 3)
        self.assertEqual(self.settings.read_text(), '{"hooks": []}')

    def _make_poc(self, extra_foreign=True):
        if not (ROOT / 'src' / 'installation.py').exists():
            self.skipTest('Sin la PoC original (no se publica en el repositorio)')
        (self.poc / 'src').mkdir(parents=True)
        for name in ('installation.py', 'bridge.py'):
            shutil.copyfile(str(ROOT / 'src' / name), str(self.poc / 'src' / name))
        code = ('import sys;sys.path.insert(0,sys.argv[1]);from installation import install;'
                'install(sys.argv[2],sys.argv[2],sys.argv[3])')
        config = self.poc / '.claude' / 'settings.local.json'
        subprocess.run([sys.executable, '-I', '-c', code, str(self.poc / 'src'), str(self.poc), str(config)],
                       check=True, capture_output=True)
        if extra_foreign:
            data = json.loads(config.read_text())
            data['hooks']['Stop'].append({'hooks': [{'type': 'command', 'command': 'echo ajeno-local'}]})
            config.write_text(json.dumps(data))
        return config

    def test_poc_hooks_block_install_until_migrated_selectively(self):
        config = self._make_poc()
        poc_backups = sorted(p for p in (self.poc / '.local-test' / 'backups').rglob('*'))
        code, refused = self.cli('install')
        self.assertEqual(code, 3)
        self.assertIn('--migrate-poc', refused['error'])
        self.assertFalse(self.settings.exists())
        self.assertFalse((self.root / 'install' / 'manifest.json').exists())
        code, result = self.cli('install', '--migrate-poc')
        self.assertEqual(code, 0, result)
        self.assertEqual(sorted(self.ours()), sorted(EVENTS))
        local = json.loads(config.read_text())
        self.assertEqual(local, {'hooks': {'Stop': [{'hooks': [{'type': 'command', 'command': 'echo ajeno-local'}]}]}})
        self.assertNotIn('desktop-questions-poc-v1', config.read_text())
        self.assertEqual(sorted(p for p in (self.poc / '.local-test' / 'backups').rglob('*')), poc_backups)
        control = json.loads((self.poc / '.local-test' / 'claude' / 'control.json').read_text())
        self.assertFalse(control['enabled'])
        migration = result['poc_migrations'][0]
        self.assertEqual(migration['remaining_poc_entries'], [])
        self.assertTrue(migration['control_disabled'])
        manifest = json.loads((self.root / 'install' / 'manifest.json').read_text())
        self.assertEqual(manifest['poc_migrations'][0]['config'], str(config))
        self.assertEqual(manifest['poc_migrations'][0]['poc_uninstall']['retained_changed_or_missing_entries'], [])
        self.assertFalse(json.loads((self.poc / '.local-test' / 'claude' / 'install.json').read_text())['active'])

    def test_poc_hooks_not_matching_their_manifest_are_not_migrated(self):
        config = self._make_poc(extra_foreign=False)
        data = json.loads(config.read_text())
        data['hooks']['Stop'][0]['hooks'][0]['timeout'] = 11
        config.write_text(json.dumps(data))
        before = config.read_bytes()
        code, refused = self.cli('install', '--migrate-poc')
        self.assertEqual(code, 3)
        self.assertEqual(config.read_bytes(), before)
        self.assertFalse(self.settings.exists())

    # Solo si el settings.local.json real aún tiene hooks de la PoC (Claude Code puede recrearlo
    # solo con permisos, y entonces no hay nada que migrar).
    @unittest.skipUnless((ROOT / '.claude' / 'settings.local.json').exists()
                         and '"hooks"' in (ROOT / '.claude' / 'settings.local.json').read_text(errors='replace')
                         and (ROOT / '.local-test' / 'claude' / 'install.json').exists(),
                         'La PoC real ya no está instalada')
    def test_migration_on_temporary_copy_of_real_poc_settings(self):
        (self.poc / '.claude').mkdir(parents=True)
        (self.poc / '.local-test' / 'claude').mkdir(parents=True)
        (self.poc / 'src').mkdir()
        for name in ('installation.py', 'bridge.py'):
            shutil.copyfile(str(ROOT / 'src' / name), str(self.poc / 'src' / name))
        config = self.poc / '.claude' / 'settings.local.json'
        config.write_bytes((ROOT / '.claude' / 'settings.local.json').read_bytes())
        manifest = json.loads((ROOT / '.local-test' / 'claude' / 'install.json').read_text())
        manifest['config'] = str(config.resolve())
        (self.poc / '.local-test' / 'claude' / 'install.json').write_text(json.dumps(manifest))
        (self.poc / '.local-test' / 'claude' / 'control.json').write_text('{"token":"copia","enabled":true}')
        code, result = self.cli('install', '--migrate-poc')
        self.assertEqual(code, 0, result)
        self.assertEqual(result['poc_migrations'][0]['remaining_poc_entries'], [])
        if config.exists():
            self.assertNotIn('desktop-questions-poc-v1', config.read_text())

    # --- Revisión 1 ---------------------------------------------------------------------
    def test_installed_command_always_passes_home_even_for_default_root(self):
        default = self.user_home / 'Library' / 'Application Support' / 'Pancho'
        code, result = self.cli('install', home=default)
        self.assertEqual(code, 0, result)
        for event in EVENTS:
            command = self.hooks()[event][0]['hooks'][0]['command']
            self.assertIn("--home '" + str(default) + "'", command)

    def test_permission_hook_covers_every_tool_with_the_long_timeout(self):
        self.assertEqual(self.cli('install')[0], 0)
        entry = self.ours()['PermissionRequest'][0]
        self.assertNotIn('matcher', entry)
        self.assertEqual(entry['hooks'][0]['timeout'], 1860)

    def test_install_extends_an_older_installation_with_the_new_event_only(self):
        with mock.patch.object(installer, 'EVENTS', tuple(e for e in installer.EVENTS
                                                          if e[0] != 'PermissionRequest')):
            old = installer.install(home=self.root, settings=self.settings, poc_root=self.poc)
        self.assertNotIn('PermissionRequest', self.hooks())
        before = self.settings.read_bytes()
        code, result = self.cli('install')
        self.assertEqual(code, 0, result)
        self.assertEqual(result['extended_events'], ['PermissionRequest'])
        self.assertEqual({e: len(v) for e, v in self.ours().items() if v}, {e: 1 for e in EVENTS})
        extension = result['extensions'][0]
        self.assertEqual(Path(extension['backup']).read_bytes(), before)
        self.assertEqual(result['backup'], old['backup'])  # La copia original no se pierde.
        code, again = self.cli('install')
        self.assertEqual((code, again.get('extended_events')), (0, None))
        self.assertEqual(self.cli('uninstall')[0], 0)
        self.assertFalse(self.settings.exists())

    def test_agents_panel_upgrade_adds_subagent_events_and_agent_matcher(self):
        # Instalación de antes del panel de agentes: sin eventos de subagente ni Agent en PreToolUse.
        old_events = tuple(('PreToolUse', '^(AskUserQuestion|mcp__ccd_turn__wrap_up)$', 1860) if e[0] == 'PreToolUse' else e
                           for e in installer.EVENTS if e[0] not in ('SubagentStart', 'SubagentStop'))
        with mock.patch.object(installer, 'EVENTS', old_events):
            installer.install(home=self.root, settings=self.settings, poc_root=self.poc)
        code, status = self.cli('status')
        self.assertEqual(code, 0, status)
        self.assertEqual(sorted(status['missing_events']), ['PreToolUse', 'SubagentStart', 'SubagentStop'])
        before = self.settings.read_bytes()
        code, result = self.cli('install')
        self.assertEqual(code, 0, result)
        self.assertEqual(sorted(result['extended_events']), ['PreToolUse', 'SubagentStart', 'SubagentStop'])
        self.assertEqual(Path(result['extensions'][-1]['backup']).read_bytes(), before)
        pre = self.ours()['PreToolUse'][0]
        self.assertIn('Agent', pre['matcher'])
        self.assertIn('AskUserQuestion', pre['matcher'])
        self.assertEqual({e: len(v) for e, v in self.ours().items() if v}, {e: 1 for e in EVENTS})
        self.assertEqual(self.cli('status')[1]['missing_events'], [])

    def test_old_bridge_copy_is_reported_and_refreshed_without_touching_settings(self):
        # Pancho nuevo con el mismo conjunto de avisos pero código del puente cambiado: hay que volver a desplegarlo.
        installer.install(home=self.root, settings=self.settings, poc_root=self.poc)
        self.assertFalse(self.cli('status')[1]['bridge_outdated'])
        installed = self.root / 'bridge' / 'pancho_bridge' / 'sessions.py'
        installed.write_text(installed.read_text() + '\n# copia antigua\n')
        self.assertTrue(self.cli('status')[1]['bridge_outdated'])
        before = self.settings.read_bytes()
        code, result = self.cli('install')
        self.assertEqual(code, 0, result)
        self.assertEqual(self.settings.read_bytes(), before)
        self.assertFalse(self.cli('status')[1]['bridge_outdated'])

    def _old_events(self):
        # Versión anterior: PreToolUse solo para AskUserQuestion y Stop con 10 s.
        return tuple(('PreToolUse', installer.MATCHER, 1860) if e[0] == 'PreToolUse'
                     else ('Stop', None, 10) if e[0] == 'Stop' else e for e in installer.EVENTS)

    def test_install_replaces_changed_own_entries_in_place_and_keeps_foreign_ones(self):
        self.settings.parent.mkdir(parents=True)
        self.settings.write_bytes(FOREIGN)
        with mock.patch.object(installer, 'EVENTS', self._old_events()):
            old = installer.install(home=self.root, settings=self.settings, poc_root=self.poc)
        self.assertEqual(self.ours()['Stop'][0]['hooks'][0]['timeout'], 10)
        before = self.settings.read_bytes()
        code, result = self.cli('install')
        self.assertEqual(code, 0, result)
        self.assertEqual(sorted(result['extended_events']), ['PreToolUse', 'Stop'])
        ours = self.ours()
        self.assertEqual({e: len(v) for e, v in ours.items() if v}, {e: 1 for e in EVENTS})
        self.assertEqual(ours['Stop'][0]['hooks'][0]['timeout'], 1860)
        self.assertEqual(ours['PreToolUse'][0]['matcher'], installer.PRE_MATCHER)
        self.assertEqual(self.hooks()['PreToolUse'][0], {'matcher': 'Bash', 'hooks': [{'type': 'command', 'command': 'echo ajeno'}]})
        self.assertEqual(Path(result['extensions'][-1]['backup']).read_bytes(), before)
        self.assertEqual(result['backup'], old['backup'])
        code, again = self.cli('install')
        self.assertEqual((code, again.get('extended_events')), (0, None))
        self.assertEqual(self.cli('uninstall')[0], 0)
        self.assertEqual(json.loads(self.settings.read_text()), json.loads(FOREIGN))

    def test_install_does_not_upgrade_when_an_own_entry_was_edited_by_hand(self):
        with mock.patch.object(installer, 'EVENTS', self._old_events()):
            installer.install(home=self.root, settings=self.settings, poc_root=self.poc)
        data = json.loads(self.settings.read_text())
        data['hooks']['Stop'][0]['hooks'][0]['timeout'] = 99
        self.settings.write_text(json.dumps(data))
        before = self.settings.read_bytes()
        code, result = self.cli('install')
        self.assertNotEqual(code, 0, result)
        self.assertEqual(self.settings.read_bytes(), before)

    def test_stop_failure_hook_is_installed_and_runs(self):
        code, result = self.cli('install')
        self.assertEqual(code, 0, result)
        group = self.ours()['StopFailure'][0]
        self.assertNotIn('matcher', group)
        self.assertEqual(group['hooks'][0]['timeout'], 10)
        run = subprocess.run(['/bin/sh', '-c', group['hooks'][0]['command']], input=json.dumps(dict(
            hook_event_name='StopFailure', session_id='s', cwd='/p/x', error='rate_limit')).encode(),
            capture_output=True, env=self.env, timeout=20)
        self.assertEqual((run.returncode, run.stdout), (0, b''))
        record = json.loads(next((self.root / 'v1' / 'sessions').glob('*.json')).read_text())
        self.assertEqual(record['activity'], 'idle')

    def test_status_documents_interpreter_and_warns_when_missing(self):
        link = self.tmp / 'bin' / 'python-pancho'
        link.parent.mkdir()
        link.symlink_to(sys.executable)
        code, result = self.cli('install', '--python', str(link))
        self.assertEqual(code, 0, result)
        self.assertEqual(result['python']['path'], str(link))  # Sin resolver enlaces.
        self.assertEqual(result['python']['realpath'], os.path.realpath(sys.executable))
        self.assertTrue(self.hooks()['Stop'][0]['hooks'][0]['command'].startswith(str(link) + ' -I '))
        code, status = self.cli('status')
        self.assertEqual((status['python']['path'], status['python_present'], status['warnings']), (str(link), True, []))
        link.unlink()
        code, status = self.cli('status')
        self.assertFalse(status['python_present'])
        self.assertEqual(len(status['warnings']), 1)
        self.assertIn(str(link), status['warnings'][0])
        self.assertIn('--python', status['warnings'][0])
        code, refused = self.cli('install', '--python', str(self.tmp / 'no-existe' / 'python3'))
        self.assertEqual(code, 3)

    @unittest.skipUnless(PYTHON313.exists(), 'python@3.13 de Homebrew no instalado')
    def test_install_with_homebrew_python313_runs_hooks(self):
        code, result = self.cli('install', '--python', str(PYTHON313))
        self.assertEqual(code, 0, result)
        self.assertEqual(result['python']['path'], str(PYTHON313))
        self.assertTrue(result['python']['version'].startswith('3.13.'))
        command = self.hooks()['UserPromptSubmit'][0]['hooks'][0]['command']
        self.assertTrue(command.startswith(str(PYTHON313) + ' -I '))
        env = dict(self.env)
        env.pop('PANCHO_HOME')
        run = subprocess.run(['/bin/sh', '-c', command], input=json.dumps(dict(
            hook_event_name='UserPromptSubmit', session_id='s313', cwd='/p/313')).encode(),
            capture_output=True, env=env, timeout=20)
        self.assertEqual((run.returncode, run.stdout), (0, b''))
        self.assertEqual(len(list((self.root / 'v1' / 'sessions').glob('*.json'))), 1)

    def test_unmanaged_owner_entries_are_reported_and_not_duplicated(self):
        code, first = self.cli('install')
        modified = json.loads(json.dumps(self.hooks()['Stop'][0]))
        modified['hooks'][0]['timeout'] = 99
        self.cli('uninstall')
        self.settings.write_text(json.dumps({'hooks': {'Stop': [modified]}}))
        code, status = self.cli('status')
        self.assertEqual(status['unmanaged_owner_entries'], [{'event': 'Stop', 'index': 0}])
        self.assertIn('Stop[0]', status['warnings'][-1])
        before = self.settings.read_bytes()
        code, refused = self.cli('install')
        self.assertEqual(code, 3)
        self.assertIn('--force', refused['error'])
        self.assertEqual(self.settings.read_bytes(), before)
        code, forced = self.cli('install', '--force')
        self.assertEqual(code, 0, forced)
        self.assertEqual(len(self.ours()['Stop']), 2)
        self.assertEqual(forced['unmanaged_owner_entries'], [{'event': 'Stop', 'index': 0}])
        self.assertTrue(forced['warnings'])

    def test_package_is_python39_compatible_and_self_contained(self):
        for path in (ROOT / 'src' / 'pancho_bridge').glob('*.py'):
            source = path.read_text(encoding='utf-8')
            ast.parse(source, filename=str(path), feature_version=(3, 9))
            self.assertNotIn('from bridge', source)
            self.assertNotIn('import bridge', source)


if __name__ == '__main__':
    unittest.main()
