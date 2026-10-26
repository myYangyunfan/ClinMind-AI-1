"""Regression tests for persistence and shared telemetry used by the new UI."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
import config
from app.core.session_manager import SessionStore
from app.server import app


class SessionBackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path_patch = patch.object(config, "RUNTIME_DIR", Path(self.temp.name))
        self.path_patch.start()
        self.old_store = SessionStore._instance
        SessionStore._instance = None
        self.client = TestClient(app)
        self.session_id = self.client.post('/api/case/start', json={}).json()['session_id']

    def tearDown(self):
        SessionStore._instance = self.old_store
        self.path_patch.stop()
        self.temp.cleanup()

    def action(self, content='测量双侧上肢血压', action_type='physical_exam', **extra):
        return self.client.post('/api/action/execute', json={'session_id': self.session_id, 'action_type': action_type, 'content': content, **extra})

    def test_saved_session_survives_store_restart(self):
        self.assertEqual(self.action().status_code, 200)
        SessionStore._instance = SessionStore()
        data = self.client.get(f'/api/case/session/{self.session_id}/workspace').json()
        self.assertEqual(len(data['actions']), 1)
        self.assertIn('33', str(data['reports']))
        self.assertEqual(len(data['radar_scores']), 6)

    def test_unknown_session_never_selects_another_student(self):
        self.assertEqual(self.client.get('/api/case/session/not-found/workspace').status_code, 404)
        response = self.client.post('/api/action/execute', json={'session_id':'not-found','action_type':'inquiry','content':'hello'})
        self.assertEqual(response.status_code, 404)

    def test_multiple_monitors_do_not_advance_clock(self):
        url=f'/ws/vitals/{self.session_id}'
        with self.client.websocket_connect(url) as first:
            a=first.receive_json()
            with self.client.websocket_connect(url) as second:
                b=second.receive_json()
                self.assertEqual(a['virtual_seconds'], 0)
                self.assertEqual(b['virtual_seconds'], 0)
                SessionStore.get_instance().tick_all()
                self.assertEqual(SessionStore.get_instance().get_session(self.session_id).virtual_seconds, 1)

    def test_invalid_actions_return_validation_errors(self):
        self.assertEqual(self.action('   ').status_code, 422)
        self.assertEqual(self.action('艾司洛尔', 'medication', params={'dose':'invalid'}).status_code, 422)
        self.assertEqual(len(SessionStore.get_instance().get_session(self.session_id).action_history), 0)

    def test_contraindication_flag_is_returned_and_saved(self):
        response=self.action('阿司匹林', 'medication').json()
        self.assertTrue(response['is_contraindicated'])
        self.assertTrue(response['warning'])
        self.assertTrue(SessionStore.get_instance().get_session(self.session_id).action_history[0].is_contraindicated)

    def test_finished_session_is_frozen(self):
        response=self.client.post('/api/eval/submit', json={'session_id':self.session_id,'final_diagnosis':'主动脉夹层'})
        self.assertEqual(response.status_code, 200)
        SessionStore.get_instance().tick_all()
        session=SessionStore.get_instance().get_session(self.session_id)
        self.assertEqual(session.virtual_seconds, 0)
        self.assertEqual(self.action().status_code, 400)
        self.assertTrue(self.client.get(f'/api/case/session/{self.session_id}/workspace').json()['assessment'])

    def test_new_ui_and_health_are_served(self):
        self.assertEqual(self.client.get('/api/health').json()['status'], 'ok')
        for page in ['reception.html','workstation.html','js/scene.js','css/scene.css']:
            self.assertEqual(self.client.get('/static/'+page).status_code, 200)

    def test_configuration_hides_credentials_and_requires_opt_in(self):
        with patch.object(config, 'USE_ONLINE_LLM', False), patch.object(config, 'ONLINE_LLM_ENABLED', False), patch.object(config, 'DEEPSEEK_API_KEY', 'dummy-secret'):
            self.assertFalse(config.is_online_llm_active())
            data=self.client.get('/api/config/llm').json()
            self.assertEqual(data['provider'], 'offline')
            self.assertNotIn('dummy-secret', str(data))
        with patch.object(config, 'ENV_FILE', Path(self.temp.name)/'.env'):
            data=self.client.post('/api/config/llm', json={'provider':'offline'}).json()
            self.assertTrue(data['success'])
        self.assertEqual(self.client.post('/api/config/llm', json={'provider':'unknown'}).status_code, 400)


if __name__ == '__main__':
    unittest.main()
