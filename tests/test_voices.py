"""Voice selection, persistence and provider wiring without network or TTS calls."""
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests
import sheets_to_elevenlabs_qc_local as pipeline
from review_service import ReviewService
from tts_voices import VoiceStore, fetch_voice, validate_voice_id
from webui_api import Api


class VoiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'voices.json'
        self.default = 'legacyVoice123'
        self.second = 'femaleVoice456'
        self.patch = patch.object(pipeline, 'VOICE_ID', self.default)
        self.patch.start(); self.addCleanup(self.patch.stop)
        self.api = Api(voice_config_path=self.path)

    def test_existing_voice_import_and_restart(self):
        self.assertEqual(self.api.get_state()['voice_id'], self.default)
        self.assertFalse(self.path.exists())
        self.api.voices.add({'id': self.second, 'name': 'Anna'})
        restarted = Api(voice_config_path=self.path)
        self.assertEqual(restarted.get_state()['voice_id'], self.second)
        self.assertEqual(len(restarted.get_state()['voices']), 2)
        restarted.set_voice(self.default)
        self.assertEqual(Api(voice_config_path=self.path).get_state()['voice_id'], self.default)
        self.assertNotIn('api_key', self.path.read_text())

    def test_adding_voice_fetches_name_selects_it_and_avoids_duplicates(self):
        with patch('webui_api.fetch_voice', return_value={'id': self.second, 'name': 'Anna'}) as fetch:
            self.assertTrue(self.api.add_voice(self.second)['ok'])
            self.assertEqual(self.api.get_state()['voice_id'], self.second)
            self.api.add_voice(self.second, 'Weibliche Stimme')
            fetch.assert_called_once()
        self.assertEqual(len(self.api.get_state()['voices']), 2)
        self.assertEqual(self.api.voices.get()['name'], 'Weibliche Stimme')

    def test_failed_lookup_and_disk_write_preserve_old_selection(self):
        self.api.set_voice(self.default)
        before = self.path.read_bytes()
        with patch('webui_api.fetch_voice', side_effect=ValueError('Stimme nicht gefunden')):
            self.assertFalse(self.api.add_voice(self.second)['ok'])
        with patch('tts_voices.os.replace', side_effect=OSError('disk full')):
            with self.assertRaises(OSError): self.api.voices.add({'id': self.second, 'name': 'Anna'})
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.api.voices.get()['id'], self.default)

    def test_busy_job_prevents_voice_changes(self):
        self.api._busy = True
        with patch('webui_api.fetch_voice') as fetch:
            self.assertFalse(self.api.set_voice(self.default)['ok'])
            self.assertFalse(self.api.add_voice(self.second)['ok'])
            fetch.assert_not_called()
        self.assertFalse(self.path.exists())

    def test_selected_voice_reaches_subprocess_with_shared_output_folder(self):
        self.api.voices.add({'id': self.second, 'name': 'Anna'})
        process = Mock(stdout=io.StringIO('@@PROGRESS 1/1\n'))
        process.wait.return_value = 0
        with patch('webui_api.subprocess.Popen', return_value=process) as launch:
            self.api.run_generation(folder='/tmp/shared-output')
        env = launch.call_args.kwargs['env']
        self.assertEqual(env['ELEVENLABS_VOICE_ID'], self.second)
        self.assertEqual(env['TTS_VOICE_NAME'], 'Anna')
        self.assertEqual(env['OUTPUT_DIR'], '/tmp/shared-output')

    def test_job_captures_voice_before_background_execution(self):
        self.api.voices.add({'id': self.second, 'name': 'Anna'})
        with patch.object(self.api, 'launch_job', return_value={}) as launch, patch.object(self.api, 'run_generation') as generate:
            self.api.start([], all_open=True)
            self.api.voices.select(self.default)
            launch.call_args.args[0]()
            self.assertEqual(generate.call_args.args[3], self.second)

    def test_review_regeneration_keeps_recorded_voice_after_app_switch(self):
        self.api.voices.add({'id': self.second, 'name': 'Anna'})
        service = ReviewService(self.api, pipeline)
        entry = {'id':'item','text':'Apfel','mode':'word','filename':'apple.opus','target_path':str(Path(self.tmp.name)/'apple.opus'), 'model':'eleven_v3'}
        with patch.object(service, 'entry', return_value=entry), patch.object(service, 'current_sheet'), patch.object(self.api, 'run_generation', return_value={}) as generate:
            service.perform('key', 'version', 'regenerate', {})
            self.assertEqual(generate.call_args.args[3], self.default)  # Legacy entry never adopts the new default.
            entry['voice_id'] = self.second
            self.api.voices.select(self.default)
            service.perform('key', 'version', 'regenerate', {})
            self.assertEqual(generate.call_args.args[3], self.second)

    def test_review_and_history_keep_voice_metadata(self):
        with patch.object(pipeline, 'REVIEW_DATA_FILE', str(Path(self.tmp.name)/'review.json')), patch.object(pipeline, 'VOICE_NAME', 'Bisherige Stimme'):
            row = {'id':'item', 'filename':'apple.opus', '_target_path':str(Path(self.tmp.name)/'apple.opus')}
            pipeline._record_review(row)
            with patch.object(pipeline, 'VOICE_ID', self.second), patch.object(pipeline, 'VOICE_NAME', 'Anna'):
                pipeline._record_review(row)
            entry = next(iter(pipeline._load_review_data().values()))
            self.assertEqual(entry['voice_id'], self.second)
            self.assertEqual(entry['voice_name'], 'Anna')
            self.assertEqual(entry['history'][0]['voice_id'], self.default)
            public = ReviewService(self.api, pipeline).public_entries()[0]
            self.assertEqual(public['voice_name'], 'Anna')

    def test_corrupt_configuration_is_not_replaced(self):
        self.path.write_text('{broken')
        with self.assertRaisesRegex(ValueError, 'unverändert'): VoiceStore(self.path, self.default)
        self.assertEqual(self.path.read_text(), '{broken')


class VoiceLookupTests(unittest.TestCase):
    def test_id_and_provider_response_validation(self):
        for value in ('', '../secret', 'https://example.org/id', 'id?query=1', None):
            with self.assertRaises(ValueError): validate_voice_id(value)
        response = Mock(status_code=200)
        response.json.return_value = {'voice_id':'voice123','name':'Anna'}
        with patch('tts_voices.requests.get', return_value=response) as get:
            self.assertEqual(fetch_voice(' voice123 ', 'offline-key'), {'id':'voice123','name':'Anna'})
            self.assertEqual(get.call_args.args[0], 'https://api.elevenlabs.io/v1/voices/voice123')
            self.assertFalse(get.call_args.kwargs['allow_redirects'])
            response.json.return_value = {'voice_id':'wrong','name':'Anna'}
            with self.assertRaises(ValueError): fetch_voice('voice123', 'offline-key')
            for code in (401,403,404,429,500):
                response.status_code = code
                with self.assertRaises(ValueError): fetch_voice('voice123', 'offline-key')
        with patch('tts_voices.requests.get', side_effect=requests.Timeout('secret details')):
            with self.assertRaisesRegex(ValueError,'nicht erreichbar'): fetch_voice('voice123','offline-key')

if __name__ == '__main__': unittest.main()
