"""Registro de subagentes (panel de agentes): hook como proceso real, almacén temporal.

Comprueba que Claude y Codex dejan en v1/agents/ solo lo acordado (tipo, título corto,
proyecto, horas y estado) y nunca el encargo, la respuesta ni rutas de transcripción.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / 'scripts' / 'pancho_hook.py'
sys.path.insert(0, str(ROOT / 'src'))
from pancho_bridge import agents  # noqa: E402
from pancho_bridge.store import Store  # noqa: E402

SECRET_PROMPT = 'ENCARGO-SECRETO: lee todo el repositorio y resume'
SECRET_ANSWER = 'RESPUESTA-SECRETA del subagente'
SECRET_PATH = '/Users/x/.claude/projects/p/sesion.jsonl'


class AgentsHookTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.home = self.tmp / 'Pancho'
        self.env = dict(os.environ, PANCHO_HOME=str(self.home), HOME=str(self.tmp / 'home'))

    # --- utilidades ---------------------------------------------------------------
    def hook(self, provider, payload):
        run = subprocess.run([sys.executable, '-I', str(HOOK), 'hook', '--provider', provider, '--owner', 'pancho-v1'],
                             input=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
                             capture_output=True, env=self.env, timeout=20)
        self.assertEqual((run.returncode, run.stdout), (0, b''), run.stderr)
        return run

    def claude(self, event, session='S1', **extra):
        self.hook('claude', dict(hook_event_name=event, session_id=session, cwd='/Users/x/Proyectos/mascota',
                                 transcript_path=SECRET_PATH, **extra))

    def launch(self, call, title, kind='Explore', session='S1'):
        self.claude('PreToolUse', session, tool_name='Agent', tool_use_id=call,
                    tool_input=dict(description=title, subagent_type=kind, prompt=SECRET_PROMPT))

    def start(self, agent, kind='Explore', session='S1'):
        self.claude('SubagentStart', session, agent_id=agent, agent_type=kind)

    def stop(self, agent, kind='Explore', session='S1'):
        self.claude('SubagentStop', session, agent_id=agent, agent_type=kind, stop_hook_active=False,
                    agent_transcript_path=SECRET_PATH, last_assistant_message=SECRET_ANSWER)

    def confirm(self, call, agent, title, kind='Explore', session='S1', status='completed'):
        self.claude('PostToolUse', session, tool_name='Agent', tool_use_id=call,
                    tool_input=dict(description=title, subagent_type=kind, prompt=SECRET_PROMPT),
                    tool_response=dict(status=status, agentId=agent, description=title, prompt=SECRET_PROMPT,
                                       content=[dict(type='text', text=SECRET_ANSWER)]))

    def records(self):
        directory = self.home / 'v1' / 'agents'
        out = {}
        for path in sorted(directory.glob('*.json')) if directory.exists() else []:
            data = json.loads(path.read_text())
            if isinstance(data, dict) and data.get('agent_id'):
                out[data['agent_id']] = data
        return out

    def all_text(self):
        root = self.home / 'v1'
        return '\n'.join(p.read_text(errors='replace') for p in root.rglob('*') if p.is_file())

    # --- Claude ---------------------------------------------------------------------
    def test_claude_subagent_lifecycle_with_title(self):
        self.launch('call-1', 'Buscar dónde se valida el login')
        self.start('ag-1')
        r = self.records()['ag-1']
        self.assertEqual((r['provider'], r['session_id'], r['agent_type'], r['status'], r['project']),
                         ('claude', 'S1', 'Explore', 'running', 'mascota'))
        self.assertEqual(r['title'], 'Buscar dónde se valida el login')
        self.assertIsNone(r.get('ended_at'))
        self.stop('ag-1')
        r = self.records()['ag-1']
        self.assertEqual(r['status'], 'done')
        self.assertIsInstance(r['ended_at'], float)

    def test_never_stores_prompt_answer_or_paths(self):
        self.launch('call-1', 'Título')
        self.start('ag-1')
        self.confirm('call-1', 'ag-1', 'Título')
        self.stop('ag-1')
        text = self.all_text()
        for secret in (SECRET_PROMPT, SECRET_ANSWER, SECRET_PATH, 'transcript'):
            self.assertNotIn(secret, text)

    def test_title_is_single_line_and_short(self):
        self.launch('call-1', '  Uno\n\tdos   tres ' + 'x' * 200)
        self.start('ag-1')
        title = self.records()['ag-1']['title']
        self.assertTrue(title.startswith('Uno dos tres x'))
        self.assertLessEqual(len(title), 80)
        self.assertNotIn('\n', title)

    def test_parallel_launch_pairs_by_type_and_is_corrected_by_agent_id(self):
        self.launch('call-a', 'Revisar estilos', kind='general-purpose')
        self.launch('call-b', 'Buscar pruebas', kind='Explore')
        self.start('ag-b', kind='Explore')
        self.start('ag-a', kind='general-purpose')
        r = self.records()
        self.assertEqual((r['ag-a']['title'], r['ag-b']['title']), ('Revisar estilos', 'Buscar pruebas'))
        # Mismo tipo y orden cruzado: se corrige al confirmar.
        self.launch('call-c', 'Primero', kind='Explore')
        self.launch('call-d', 'Segundo', kind='Explore')
        self.start('ag-d')
        self.start('ag-c')
        self.assertEqual(self.records()['ag-d']['title'], 'Primero')  # provisional, cruzado
        self.confirm('call-c', 'ag-c', 'Primero', status='async_launched')
        r = self.records()
        self.assertEqual((r['ag-c']['title'], r['ag-d']['title']), ('Primero', 'Segundo'))
        self.assertTrue(r['ag-c']['title_confirmed'])

    def test_session_end_closes_its_running_subagents(self):
        # Interrumpido (Esc, cierre): no llega SubagentStop. Al terminar la conversación no debe quedar «en marcha».
        self.start('ag-1')
        self.start('ag-2')
        self.stop('ag-2')
        self.start('ag-x', session='OTRA')
        self.claude('SessionEnd', reason='prompt_input_exit')
        r = self.records()
        self.assertEqual((r['ag-1']['status'], r['ag-2']['status'], r['ag-x']['status']), ('done', 'done', 'running'))
        self.assertIsInstance(r['ag-1']['ended_at'], float)

    def test_stop_without_start_is_recorded_as_done(self):
        self.stop('ag-x')
        r = self.records()['ag-x']
        self.assertEqual((r['status'], r['started_at']), ('done', None))

    def test_task_tool_name_also_counts(self):
        self.claude('PreToolUse', tool_name='Task', tool_use_id='c1',
                    tool_input=dict(description='Antiguo nombre', subagent_type='Explore'))
        self.start('ag-1')
        self.assertEqual(self.records()['ag-1']['title'], 'Antiguo nombre')

    def test_agent_pre_tool_use_does_not_create_question(self):
        self.launch('call-1', 'Título')
        self.assertFalse((self.home / 'v1' / 'requests').exists()
                         and any((self.home / 'v1' / 'requests').iterdir()))

    def test_bad_payloads_are_ignored(self):
        self.claude('SubagentStart', agent_id=123, agent_type='Explore')
        self.claude('SubagentStart', agent_id='', agent_type='Explore')
        self.hook('claude', dict(hook_event_name='SubagentStart', agent_id='a'))  # sin sesión
        self.assertEqual(self.records(), {})

    # --- Codex ----------------------------------------------------------------------
    def test_codex_subagent_has_type_and_no_title(self):
        base = dict(session_id='C1', turn_id='t1', cwd='/Users/x/Proyectos/web', model='gpt',
                    permission_mode='default', transcript_path=SECRET_PATH)
        self.hook('codex', dict(base, hook_event_name='SubagentStart', agent_id='cx-1', agent_type='worker'))
        r = self.records()['cx-1']
        self.assertEqual((r['provider'], r['agent_type'], r['title'], r['project'], r['status']),
                         ('codex', 'worker', None, 'web', 'running'))
        self.hook('codex', dict(base, hook_event_name='SubagentStop', agent_id='cx-1', agent_type='worker',
                                stop_hook_active=False, agent_transcript_path=SECRET_PATH,
                                last_assistant_message=SECRET_ANSWER))
        self.assertEqual(self.records()['cx-1']['status'], 'done')
        self.assertNotIn(SECRET_ANSWER, self.all_text())

    def test_codex_events_inside_subagent_do_not_create_conversations(self):
        base = dict(session_id='CHILD', turn_id='t1', cwd='/Users/x/web', model='gpt', permission_mode='default',
                    transcript_path=None, agent_id='cx-1', agent_type='worker')
        self.hook('codex', dict(base, hook_event_name='UserPromptSubmit', prompt='hola'))
        sessions = self.home / 'v1' / 'sessions'
        self.assertFalse(sessions.exists() and any(sessions.iterdir()))


class AgentsSweepTest(unittest.TestCase):
    def test_sweep_removes_old_done_and_stale_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp))
            now = time.time()
            base = dict(session_id='S', cwd='/p/q', hook_event_name='SubagentStart')
            agents.start(store, 'claude', dict(base, agent_id='old-done', agent_type='E'), now - 4000)
            agents.stop(store, 'claude', dict(base, agent_id='old-done', agent_type='E'), now - 3600)
            agents.start(store, 'claude', dict(base, agent_id='recent-done', agent_type='E'), now - 100)
            agents.stop(store, 'claude', dict(base, agent_id='recent-done', agent_type='E'), now - 60)
            agents.start(store, 'claude', dict(base, agent_id='stale', agent_type='E'), now - 13 * 3600)
            agents.start(store, 'claude', dict(base, agent_id='live', agent_type='E'), now - 5)
            with store.lock():
                agents._sweep(store, now)
            ids = sorted(json.loads(p.read_text()).get('agent_id', '') for p in (Path(tmp) / 'v1' / 'agents').glob('*.json'))
            self.assertEqual([i for i in ids if i], ['live', 'recent-done'])


if __name__ == '__main__':
    unittest.main()
