"""Project isolation and worksheet creation; all Google/provider calls are offline."""
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import sheets_to_elevenlabs_qc_local as pipeline
from review_service import ReviewService
from tts_projects import PROJECT_HEADERS, ProjectStore, create_project_sheet, validate_project_name
from webui_api import Api


def worksheet(gid, name):
    return Mock(id=gid, title=name, _properties={'sheetType': 'GRID'})


class ProjectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.sheets = [worksheet(0, 'Bestand'), worksheet(22, 'Training')]
        self.book = Mock()
        self.book.worksheets.side_effect = lambda: self.sheets[:]
        self.book.get_worksheet_by_id.side_effect = lambda gid: next(w for w in self.sheets if w.id == gid)
        for patcher in (patch.object(pipeline, 'SPREADSHEET_ID', 'offline'),
                        patch.object(pipeline, 'SHEET_NAME', 'Bestand'),
                        patch.object(pipeline, 'REVIEW_DATA_FILE', 'review_data.json'),
                        patch.object(pipeline, 'open_spreadsheet', return_value=self.book)):
            patcher.start(); self.addCleanup(patcher.stop)
        self.api = self.new_api()

    def new_api(self):
        api = Api(voice_config_path=self.root/'voices.json', project_config_path=self.root/'projects.json')
        api.root = self.root
        api.output_base = str(self.root/'output')
        return api

    def select(self, gid):
        result = self.api.set_project(str(gid))
        self.assertTrue(result['ok'], result)
        return result

    def test_existing_tabs_selection_and_preferences_survive_restart_and_rename(self):
        result = self.api.list_projects()
        self.assertEqual(result['project_id'], '0')
        self.assertEqual(len(result['projects']), 2)
        original_folder = result['folder']
        self.api.voices.add({'id':'otherVoice', 'name':'Anna'})
        self.api.set_voice('otherVoice', '0')
        self.api.set_model('eleven_v3', '0')
        self.select(22)
        self.assertEqual(self.api.model, 'eleven_v3')
        self.assertNotEqual(self.api.folder, original_folder)
        self.assertEqual(self.api.project['review_data'].split('/')[-2], '22')
        self.select(0)
        self.sheets[0].title = 'Bestand umbenannt'
        restarted = self.new_api()
        result = restarted.list_projects()
        self.assertEqual(result['project_id'], '0')
        self.assertEqual(result['sheet_name'], 'Bestand umbenannt')
        self.assertEqual(result['folder'], original_folder)
        self.assertEqual(result['voice_id'], 'otherVoice')
        self.assertEqual(result['model'], 'eleven_v3')

    def test_global_model_migrates_from_last_project_and_ignores_old_overrides(self):
        self.api.project_store.save_project('0', {'name':'Bestand','model':'eleven_flash_v2_5'}, select=True)
        self.api.project_store.save_project('22', {'name':'Training','model':'eleven_v3'})
        self.api = self.new_api()
        self.assertEqual(self.api.model, 'eleven_flash_v2_5')
        self.select(22)
        self.assertEqual(self.api.model, 'eleven_flash_v2_5')
        self.assertIn('eleven_v4', self.api.get_state()['models'])
        self.assertTrue(self.api.set_model('eleven_v4', '22')['ok'])
        self.select(0)
        self.assertEqual(self.api.model, 'eleven_v4')
        self.assertEqual(self.new_api().model, 'eleven_v4')
        self.assertEqual(self.api.project_store.data['model'], 'eleven_v4')
        self.assertTrue(all('model' not in p for p in self.api.project_store.book()['projects'].values()))

    def test_failed_global_model_save_keeps_previous_selection(self):
        self.select(0)
        before = self.api.model
        with patch('tts_projects.os.replace', side_effect=OSError('disk full')):
            self.assertFalse(self.api.set_model('eleven_v3')['ok'])
        self.assertEqual(self.api.model, before)
        self.assertEqual(self.new_api().model, before)

    def test_empty_new_project_accepts_first_automatic_ids_and_scopes_appends(self):
        def batch(body):
            props = body['requests'][0]['addSheet']['properties']
            self.sheets.append(worksheet(props['sheetId'], props['title']))
            return {}
        self.book.batch_update.side_effect = batch
        result = self.api.create_project('Neues Projekt')
        self.assertTrue(result['ok'], result)
        gid = result['project_id']
        self.assertEqual(self.api.project['name'], 'Neues Projekt')
        with patch.object(pipeline, 'load_rows', return_value=[]) as load:
            self.assertTrue(self.api.load_rows(gid)['ok'])
            load.assert_called_once_with(worksheet_id=int(gid))
        plan = self.api.plan_rows('Test', 'Einzelwort', 'Apfel\nBirne', gid)
        self.assertTrue(plan['ok'], plan)
        self.assertEqual([e['id'] for e in plan['entries']], ['Test_001', 'Test_002'])
        with patch.object(pipeline, 'append_rows', return_value=(2,2)) as append:
            self.assertTrue(self.api.commit_rows(plan['entries'], gid)['ok'])
            append.assert_called_once_with(plan['entries'], worksheet_id=int(gid))

    def test_old_selection_and_planned_rows_cannot_leak_into_new_project(self):
        self.select(0)
        with patch.object(pipeline, 'load_rows', return_value=[{'_row':2,'id':'old','text':'Apfel'}]):
            self.api.load_rows('0')
        plan = self.api.plan_rows('New', 'Normal', 'Hallo', '0')
        self.select(22)
        self.assertEqual(self.api.rows, [])
        self.assertFalse(self.api.rows_loaded)
        with patch.object(pipeline, 'append_rows') as append, patch.object(self.api, 'launch_job') as launch:
            self.assertFalse(self.api.commit_rows(plan['entries'], plan['project_id'])['ok'])
            self.assertFalse(self.api.start([2], False, '0')['ok'])
            self.assertFalse(self.api.start([2], False, '22')['ok'])
            append.assert_not_called(); launch.assert_not_called()
        self.assertFalse(self.api.set_voice(pipeline.VOICE_ID, '0')['ok'])
        self.assertFalse(self.api.set_model('eleven_v3', '0')['ok'])

    def test_switching_and_creating_are_blocked_during_job(self):
        self.select(0)
        self.api._busy = True
        for result in (self.api.set_project('22'), self.api.create_project('Other'), self.api.list_projects()):
            self.assertFalse(result['ok'])
        self.book.batch_update.assert_not_called()
        self.assertEqual(self.api.project['id'], '0')

    def test_running_job_captures_project_and_child_environment(self):
        self.select(0)
        with patch.object(self.api, 'launch_job', return_value={}) as launch, patch.object(self.api, 'run_generation') as generate:
            self.assertTrue(self.api.start([], True, '0')['ok'])
            callback = launch.call_args.args[0]
            self.select(22)
            callback()
            captured = generate.call_args.args[4]
            self.assertEqual(captured['id'], '0')
        process = Mock(stdout=io.StringIO(''))
        process.wait.return_value = 0
        with patch('webui_api.subprocess.Popen', return_value=process) as popen:
            self.api.run_generation(project=captured)
        env = popen.call_args.kwargs['env']
        self.assertEqual(env['TTS_WORKSHEET_ID'], '0')
        self.assertEqual(env['SHEET_NAME'], 'Bestand')
        self.assertEqual(env['SPREADSHEET_ID'], 'offline')
        self.assertEqual(env['OUTPUT_DIR'], captured['folder'])
        self.assertEqual(env['TTS_REVIEW_DATA_FILE'], captured['review_data'])

    def test_deleted_selected_tab_does_not_silently_switch_to_another(self):
        self.select(22)
        self.sheets.pop()
        result = self.api.list_projects()
        self.assertTrue(result['ok'])
        self.assertIsNone(result['project_id'])
        self.assertFalse(self.api.start([], True)['ok'])

    def test_failed_creation_keeps_selection_and_never_retries_mutation(self):
        self.select(0)
        self.book.batch_update.side_effect = TimeoutError('offline')
        result = self.api.create_project('New')
        self.assertFalse(result['ok'])
        self.assertIn('neu laden', result['error'])
        self.book.batch_update.assert_called_once()
        self.assertEqual(self.api.project['id'], '0')

    def test_legacy_review_import_once_and_project_reset_isolated(self):
        audio = self.root/'legacy.opus'; audio.write_bytes(b'original')
        legacy = {'id':'old', 'sheet_id':'offline', 'sheet_name':'Bestand', 'abspath':str(audio)}
        original = {'old':legacy, 'unrelated':dict(legacy, sheet_name='Training')}
        source = self.root/'review_data.json'
        source.write_text(json.dumps(original))
        self.api.list_projects()
        project_a = copy.deepcopy(self.api.project)
        service_a = ReviewService(self.api, pipeline, project_a)
        self.assertEqual(list(service_a.load_entries()), ['old'])
        self.assertEqual(service_a.entry('old')['worksheet_id'], 0)
        self.select(22)
        project_b = copy.deepcopy(self.api.project)
        entry_b = dict(legacy, sheet_name='Training', worksheet_id=22)
        pipeline._save_review_entry(entry_b, key='new', data_file=project_b['review_data'])
        service_b = ReviewService(self.api, pipeline, project_b)
        with self.assertRaises(ValueError): service_a.entry('new')
        with self.assertRaises(ValueError): service_b.entry('old')
        self.select(0)
        self.assertFalse(self.api.reset_review('22')['ok'])
        self.assertTrue(self.api.reset_review('0')['ok'])
        self.assertEqual(service_a.load_entries(), {})
        self.assertEqual(list(service_b.load_entries()), ['new'])
        self.api.list_projects()
        self.assertEqual(service_a.load_entries(), {})  # Reset must not re-import old reviews.
        self.assertEqual(json.loads(source.read_text()), original)
        self.assertEqual(audio.read_bytes(), b'original')

    def test_open_review_retains_its_project_after_app_switch(self):
        self.select(0)
        project = copy.deepcopy(self.api.project)
        entry = {'id':'a','text':'Apfel','mode':'word','filename':'a.opus', 'target_path':str(self.root/'a.opus'),
                 'sheet_id':'offline','sheet_name':'old title','worksheet_id':0,'model':'eleven_v3'}
        pipeline._save_review_entry(entry, key='a', data_file=project['review_data'])
        service = ReviewService(self.api, pipeline, project)
        self.select(22)
        with patch.object(pipeline, 'open_sheet', return_value=(Mock(), {}, [dict(entry, _row=2)])) as open_sheet, \
             patch.object(self.api, 'run_generation', return_value={}) as generate:
            service.perform('a', service.version(entry), 'regenerate', {})
            open_sheet.assert_called_once_with(worksheet_id=0)
            self.assertEqual(generate.call_args.kwargs['project']['id'], '0')
        # Same text/filename in another project still must not pass validation.
        with self.assertRaises(ValueError): service.current_sheet(dict(entry, worksheet_id=22))

    def test_sheet_button_opens_selected_tab(self):
        self.select(22)
        with patch('webui_api.webbrowser.open') as browser:
            self.assertTrue(self.api.open_sheet()['ok'])
        self.assertTrue(browser.call_args.args[0].endswith('#gid=22'))

    def test_review_status_saves_only_its_project_after_switch(self):
        self.select(0)
        project_a = copy.deepcopy(self.api.project)
        entry = {'id':'a','text':'Apfel','mode':'word','filename':'a.opus', 'target_path':str(self.root/'a.opus'),
                 'sheet_id':'offline','sheet_name':'Bestand','worksheet_id':0,'model':'eleven_v3'}
        pipeline._save_review_entry(entry, key='a', data_file=project_a['review_data'])
        service = ReviewService(self.api, pipeline, project_a)
        self.select(22)
        path_b = self.api.project['review_data']
        pipeline._save_review_entry(dict(entry, worksheet_id=22), key='b', data_file=path_b)
        before = Path(path_b).read_bytes()
        ws = Mock()
        with patch.object(pipeline, 'open_sheet', return_value=(ws, {}, [dict(entry, _row=2)])) as open_sheet, \
             patch.object(pipeline, 'write_back') as write_back:
            service.perform('a', service.version(entry), 'status', {'status':'regenerate'})
        self.assertTrue(all(call.kwargs == {'worksheet_id':0} for call in open_sheet.call_args_list))
        self.assertIs(write_back.call_args.args[0], ws)
        self.assertEqual(service.entry('a')['status'], 'regenerate')
        self.assertEqual(Path(path_b).read_bytes(), before)

    def test_review_identity_uses_id_instead_of_renamed_title(self):
        self.select(0)
        path = self.api.project['review_data']
        entry = {'sheet_id':'offline','worksheet_id':0,'sheet_name':'Old','id':'a','target_path':'a.opus'}
        pipeline._save_review_entry(entry, data_file=path)
        pipeline._save_review_entry(dict(entry, sheet_name='Renamed', status='passed'), data_file=path)
        saved = pipeline._load_review_data(path)
        self.assertEqual(len(saved), 1)
        self.assertEqual(next(iter(saved.values()))['status'], 'passed')


class ProjectSheetTests(unittest.TestCase):
    def test_create_uses_one_atomic_request_with_headers_and_dropdowns(self):
        book = Mock(); book.worksheets.return_value = []
        result = create_project_sheet(book, '  Wörter & Sätze  ')
        book.batch_update.assert_called_once()
        requests = book.batch_update.call_args.args[0]['requests']
        gid = int(result['id'])
        self.assertEqual(requests[0]['addSheet']['properties']['title'], 'Wörter & Sätze')
        self.assertEqual(requests[0]['addSheet']['properties']['gridProperties']['frozenRowCount'], 1)
        self.assertEqual([v['userEnteredValue']['stringValue'] for v in requests[1]['updateCells']['rows'][0]['values']], PROJECT_HEADERS)
        for request in requests[2:4]:
            self.assertEqual(request['setDataValidation']['range']['sheetId'], gid)
        self.assertIn({'userEnteredValue':'regenerate'}, requests[3]['setDataValidation']['rule']['condition']['values'])

    def test_invalid_or_duplicate_names_never_create_a_sheet(self):
        for name in ('', 'a/b', 'a:b', 'a\\b', 'a?b', 'a*b', '[a]', 'a\nb', 'x'*101, None):
            with self.assertRaises(ValueError): validate_project_name(name)
        book = Mock(); book.worksheets.return_value = [worksheet(1,'Existing')]
        with self.assertRaises(ValueError): create_project_sheet(book, 'existing')
        book.batch_update.assert_not_called()

    def test_pipeline_resolves_stable_id_zero_even_when_name_changed(self):
        book = Mock(); ws = book.get_worksheet_by_id.return_value
        ws.get_all_values.return_value = [PROJECT_HEADERS]
        with patch.object(pipeline, 'open_spreadsheet', return_value=book):
            pipeline.open_sheet('old title', 0)
        book.get_worksheet_by_id.assert_called_once_with(0)
        book.worksheet.assert_not_called()

    def test_unprepared_existing_tab_is_never_modified(self):
        ws = Mock()
        with patch.object(pipeline, 'open_sheet', return_value=(ws, {'unrelated':1}, [])):
            with self.assertRaisesRegex(ValueError, 'Kopfspalten'): pipeline.load_rows(worksheet_id=9)
            with self.assertRaisesRegex(RuntimeError, 'keine Zeilen'): pipeline.append_rows([{'id':'a','text':'Apfel'}], worksheet_id=9)
        ws.append_rows.assert_not_called()

    def test_configuration_corruption_and_failed_save_preserve_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'projects.json'
            path.write_text('{broken')
            with self.assertRaises(ValueError): ProjectStore(path, 'offline')
            self.assertEqual(path.read_text(), '{broken')
            path.unlink()
            store = ProjectStore(path, 'offline')
            store.save_project('0', {'name':'Old'}, select=True)
            before = path.read_bytes()
            with patch('tts_projects.os.replace', side_effect=OSError('disk full')):
                with self.assertRaises(OSError): store.save_project('22', {'name':'New'}, select=True)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(store.book()['selected'], '0')


if __name__ == '__main__':
    unittest.main()
