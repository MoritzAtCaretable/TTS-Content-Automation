"""Review workflow regressions; Sheets and provider calls stay offline."""
import copy
import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

from pydub.generators import Sine
from pydub import AudioSegment
import sheets_to_elevenlabs_qc_local as pipeline
from review_service import ReviewService, ReviewServer
from tts_identity import resolve_record
from tts_quality import CheckResult, QualityResult
from webui_api import Api


class IdentityTests(unittest.TestCase):
    def test_reordered_row_is_resolved_by_content_id(self):
        expected = {'id': 'Apple', 'text': 'Apfel', 'mode': 'word', '_row': 2}
        actual = dict(expected, _row=8, mode='Einzelwort')
        self.assertEqual(resolve_record([actual], expected)['_row'], 8)
        for rows in ([], [actual, actual], [dict(actual, text='Birne')], [dict(actual, mode='normal')]):
            with self.assertRaises(ValueError): resolve_record(rows, expected)

    def test_writeback_refreshes_row_and_columns_and_uses_raw_values(self):
        ws=Mock(); ws.get_all_values.return_value=[['text','status','id'], ['Other','','other'], ['Apfel','','apple']]
        pipeline.write_back(ws, {'status': 5}, 2, {'status':'passed'}, expected_row={'id':'apple','text':'Apfel'})
        ws.batch_update.assert_called_once_with([{'range':'B3','values':[['passed']]}], value_input_option='RAW')
        ws.get_all_values.return_value[2][0]='Birne'
        with self.assertRaises(ValueError): pipeline.write_back(ws,{},2,{'status':'passed'},expected_row={'id':'apple','text':'Apfel'})
        self.assertEqual(ws.batch_update.call_count,1)


class ReviewTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup); self.root=Path(tmp.name).resolve()
        self.target=self.root/'apple.opus'; self.source=self.root/'source.wav'
        (AudioSegment.silent(200)+Sine(440).to_audio_segment(1000).apply_gain(-15)+AudioSegment.silent(300)).export(self.source,format='wav').close()
        self.entry={'id':'apple','text':'Apfel','mode':'word','filename':self.target.name,
                    'target_path':str(self.target),'abspath':str(self.source),'raw_abspath':str(self.source),
                    'status':'review needed','model':'eleven_v3', 'sheet_id':'offline','sheet_name':'tab'}
        self.api=Api(); self.service=ReviewService(self.api,pipeline)
        patches=[patch.object(pipeline,'REVIEW_DATA_FILE',str(self.root/'review.json')),
                 patch.object(pipeline,'SPREADSHEET_ID','offline'),patch.object(pipeline,'SHEET_NAME','tab'),
                 patch.object(pipeline,'generate_review_html'),patch.object(pipeline,'write_back'),
                 patch.object(pipeline,'open_sheet',return_value=(Mock(),{},[dict(self.entry,_row=9)]))]
        self.mocks=[p.start() for p in patches]
        for p in patches:self.addCleanup(p.stop)
        pipeline._save_review_entry(self.entry,key='test')

    def perform(self,action,options=None):
        return self.service.perform('test',self.service.version(self.service.entry('test')),action,options or {})

    def test_version_rejects_stale_decision_and_file_changes(self):
        version=self.service.version(self.entry)
        self.source.write_bytes(b'changed')
        with self.assertRaises(ValueError):self.service.entry('test',version)
        current=self.service.entry('test'); current['reason']='New';pipeline._save_review_entry(current,key='test')
        with self.assertRaises(ValueError):self.service.entry('test',version)

    def test_trim_keeps_final_and_source_then_requires_fresh_approval(self):
        self.target.write_bytes(b'previous accepted file')
        original=self.source.read_bytes()
        self.perform('trim',{'start':.15,'end':1.3,'source':'original'})
        entry=self.service.entry('test')
        self.assertEqual(entry['status'],'review needed'); self.assertFalse(entry['qc']['passed'])
        self.assertEqual(entry['qc']['checks']['text']['state'],'skipped')
        self.assertEqual(self.target.read_bytes(),b'previous accepted file')
        self.assertEqual(self.source.read_bytes(),original)
        self.assertEqual(entry['raw_abspath'],str(self.source))
        self.service.verify(Path(entry['abspath']),entry)  # Real FFmpeg verifies output.
        self.perform('status',{'status':'passed','note':'Inhalt angehört'})
        approved=self.service.entry('test')
        self.assertEqual(approved['status'],'passed');self.assertEqual(approved['decision']['type'],'manual')
        self.assertTrue(self.target.read_bytes().startswith(b'OggS'))
        self.assertEqual(len(list((self.root/'.sources').rglob('previous.opus'))),1)

    def test_invalid_trim_never_changes_entry(self):
        for start,end in [(float('nan'),1),(0,float('inf')),(-1,1),(0,99),(.5,.51),(True,1)]:
            with self.assertRaises(ValueError):self.perform('trim',{'start':start,'end':end})
        self.assertEqual(self.service.entry('test'),self.entry)

    def test_sheet_failure_is_durable_and_sync_retries_without_generation(self):
        self.mocks[4].side_effect=RuntimeError('Sheet offline')
        with self.assertRaisesRegex(RuntimeError,'lokal gespeichert'):
            self.perform('status',{'status':'review needed','note':'Nachhören'})
        saved=self.service.entry('test');self.assertEqual(saved['sync_error'],'Sheet offline')
        self.assertEqual(saved['decision']['note'],'Nachhören')
        self.mocks[4].side_effect=None
        with patch.object(self.api,'run_generation') as generate:
            self.perform('sync');generate.assert_not_called()
        self.assertFalse(self.service.entry('test')['sync_error'])

    def test_regenerate_status_queues_without_generating_or_publishing(self):
        with patch.object(self.api, 'run_generation') as generate, patch.object(self.service, 'publish') as publish:
            self.perform('status', {'status': 'regenerate'})
            generate.assert_not_called()
            publish.assert_not_called()
        entry = self.service.entry('test')
        self.assertEqual(entry['status'], 'regenerate')
        self.assertEqual(entry['sync_updates']['status'], 'regenerate')
        self.assertEqual(entry['decision']['note'], '')
        self.assertTrue(pipeline.should_process(entry['status']))
        self.assertEqual(self.mocks[4].call_args.args[3]['status'], 'regenerate')

    def test_changed_sheet_blocks_paid_regeneration(self):
        self.mocks[5].return_value=(Mock(),{},[dict(self.entry,_row=9,text='Birne')])
        with patch.object(self.api,'run_generation') as generate:
            with self.assertRaises(ValueError):self.perform('regenerate',{'model':'eleven_v3'})
            generate.assert_not_called()

    def test_regenerate_passes_stable_selection_folder_and_model(self):
        with patch.object(self.api,'run_generation',return_value={'message':'done'}) as generate:
            self.perform('regenerate',{'model':'eleven_flash_v2_5'})
        generate.assert_called_once_with([{'id':'apple','text':'Apfel','mode':'word','filename':'apple.opus'}],str(self.root),'eleven_flash_v2_5')

    def test_recheck_reuses_audio_without_tts_and_publishes_only_pass(self):
        # Verification and provider QC are isolated; actual exporter is tested above.
        self.service.whisper_model=object()
        with patch.object(self.service,'verify',return_value={}),patch.object(pipeline,'ENABLE_GEMINI_CHECK',False), \
             patch.object(pipeline,'quality_check',return_value=QualityResult({'text':CheckResult('failed','Falsches Wort')})) as qc, \
             patch.object(pipeline,'text_to_speech') as tts, patch.object(self.service,'publish') as publish:
            self.perform('recheck');publish.assert_not_called();tts.assert_not_called()
            self.assertEqual(self.service.entry('test')['status'],'review needed')
            qc.return_value=QualityResult({'text':CheckResult('passed','Vollständig')})
            self.perform('recheck');publish.assert_called_once();tts.assert_not_called()
            self.assertEqual(self.service.entry('test')['status'],'passed')
            self.assertTrue(qc.call_args.kwargs['skip_silence'])

    def test_corrupt_journal_is_never_overwritten(self):
        path=self.root/'review.json';path.write_text('{broken')
        with self.assertRaises(RuntimeError):pipeline._save_review_entry(self.entry,key='test')
        self.assertEqual(path.read_text(),'{broken')

    def test_distinct_output_directories_do_not_overwrite_same_filename(self):
        pipeline._save_review_entry(self.entry)
        pipeline._save_review_entry(dict(self.entry,target_path=str(self.root/'other'/'apple.opus')))
        self.assertEqual(len(pipeline._load_review_data()),3)

    def test_http_assets_ranges_and_action_options(self):
        server=ReviewServer(self.api,pipeline,Path(__file__).resolve().parents[1]);self.addCleanup(server.close)
        parsed=urlsplit(server.url)
        def request(path,method='GET',body=None,headers=None):
            c=http.client.HTTPConnection(parsed.hostname,parsed.port,timeout=3)
            c.request(method,path,body=body,headers=headers or {});r=c.getresponse();data=r.read();status=r.status;c.close();return status,data
        base=parsed.path
        self.assertEqual(request(base)[0],200)
        self.assertEqual(request(base+'review.js')[0],200)
        self.assertEqual(request('/api/entries')[0],400)
        self.assertEqual(request(base+'../.env')[0],400)
        self.assertEqual(request(base+'audio/test',headers={'Range':'bytes=0-3'}),(206,b'RIFF'))
        self.assertEqual(request(base+'audio/test',headers={'Range':'bytes=999999-'} )[0],416)
        body=json.dumps({'key':'test','version':self.service.version(self.entry),'action':'status','options':{'status':'review needed','note':'Test'}})
        headers={'Content-Type':'application/json','Origin':'http://evil.invalid'}
        self.assertEqual(request(base+'api/action','POST',body,headers)[0],400)
        headers['Origin']=server.origin
        with patch.object(server.service,'perform',return_value={}) as perform:
            status,data=request(base+'api/action','POST',body,headers);self.assertEqual(status,202)
            deadline=time.monotonic()+2
            while self.api.job_state()['running'] and time.monotonic()<deadline:time.sleep(.01)
            self.assertEqual(perform.call_args.args[3],{'status':'review needed','note':'Test'})


class JobTests(unittest.TestCase):
    def test_only_one_job_runs_and_failed_outcome_is_visible(self):
        api=Api();release=threading.Event();started=threading.Event()
        def run():started.set();release.wait(2);raise RuntimeError('Expected failure')
        api.launch_job(run);self.assertTrue(started.wait(1))
        try:
            with self.assertRaises(ValueError):api.launch_job(lambda:None)
        finally:release.set()
        deadline=time.monotonic()+2
        while api.job_state()['running'] and time.monotonic()<deadline:time.sleep(.01)
        self.assertEqual(api.job_state()['outcome']['state'],'failed')
        self.assertIn('Expected failure',api.job_state()['outcome']['message'])

    def test_empty_sheet_does_not_load_models(self):
        with patch.object(pipeline,'open_sheet',return_value=(Mock(),{'id':1,'text':2,'status':3},[])), \
             patch.object(pipeline,'ELEVENLABS_API_KEY','offline'),patch.object(pipeline,'ENABLE_GEMINI_CHECK',False), \
             patch.object(pipeline.whisper,'load_model') as load,patch.dict('os.environ',{'TTS_SELECTION':''}), \
             tempfile.TemporaryDirectory() as tmp,patch.object(pipeline,'OUTPUT_DIR',tmp),patch.object(pipeline,'REVIEW_DIR',tmp):
            pipeline.main();load.assert_not_called()

if __name__=='__main__':unittest.main()
