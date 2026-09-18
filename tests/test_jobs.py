from dataclasses import asdict
import copy
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest

from model_router.config import Config
from model_router.job_client import JobClient
from model_router.job_contract import canonical, fingerprint, validate_submit, TERMINAL
from model_router.job_store import JobStore
from model_router.jobs import JobService, serve
from model_router.models import Task, TaskProfile


ROOT = Path(__file__).resolve().parents[1]


class JobTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / 'source'
        self.repo.mkdir()
        (self.repo / 'value.txt').write_text('before')
        (self.repo / 'private.txt').write_text('never export this')
        (self.repo / 'check.py').write_text("from pathlib import Path\nassert Path('value.txt').read_text() == 'done'\n")
        self.cfg = Config(self.root / 'state', auto_usage=False, max_total_runtime=30, max_attempts=2,
            commands={'check':[sys.executable,'-B','check.py']})
        self.cfg.workspaces = {'demo': {'path':str(self.repo), 'read_files':['value.txt','check.py'],
            'write_files':['value.txt'], 'checks':['check'], 'allowed_models':['deepseek','sol_medium'], 'upload_allowed':True}}
        profile = TaskProfile(task_type='batch_edit', requirement_clarity=5, automatic_verifiability=5, human_verification_cost=1,
            failure_blast_radius=1, tool_complexity=1, repo_scope=1, domain_uncertainty=0, root_cause_uncertainty=0,reversibility=5)
        task = Task('write',profile,allowed_files=['value.txt'],checks=['check'],acceptance_criteria=['value is done'],no_escalation=True)
        self.request = dict(client_task_id='parent-1', idempotency_key='input-1',session_id='conversation-1',workspace_id='demo',
            task=asdict(task),limits=dict(max_runtime=15,max_attempts=1,max_provider_calls=2),allowed_models=['deepseek','sol_medium'],upload_allowed=True)
        self.service = None

    def tearDown(self):
        if self.service:
            self.service.close()
        self.temp.cleanup()

    def start(self):
        self.service = JobService(self.cfg,'moxiaoxi',worker_command=[sys.executable,'-B','-X','utf8','-m','tests.job_fixture_worker'])
        return self.service

    def finish(self, identifier, seconds=10):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            result = self.service.get(identifier)
            if result['state'] in TERMINAL:
                return result
            time.sleep(.03)
        self.fail('job did not settle')

    def test_contract_rejects_scope_budget_and_model_expansion(self):
        for field, value in [('upload_allowed',False),('allowed_models',['astra']),('limits',{'tokens':6000}),('workspace_id','../demo')]:
            request = copy.deepcopy(self.request)
            request[field] = value
            with self.assertRaises(ValueError):
                validate_submit(self.cfg,request)
        for allowed in (['*'],['private.txt'],['../outside']):
            request = copy.deepcopy(self.request)
            request['task']['allowed_files'] = allowed
            with self.assertRaises(ValueError):
                validate_submit(self.cfg,request)
        request = copy.deepcopy(self.request)
        request['limits']['max_provider_calls'] = True
        with self.assertRaises(ValueError):
            validate_submit(self.cfg,request)

    @unittest.skipUnless(os.name == 'nt','Windows v1 containment')
    def test_real_subprocess_idempotency_package_and_candidate_hashes(self):
        service = self.start()
        first = service.submit(self.request)
        second = service.submit(self.request)
        self.assertEqual(first['job_id'],second['job_id'])
        self.assertTrue(second['duplicate'])
        result = self.finish(first['job_id'])
        self.assertEqual(result['state'],'verified',result)
        self.assertEqual((self.repo/'value.txt').read_text(),'before')
        package = self.cfg.state_dir/'packages'/first['job_id']/'input'
        self.assertFalse((package/'private.txt').exists())
        events = service.store.events('moxiaoxi', first['job_id'],0,100)['events']
        self.assertEqual(sum(e['kind']=='provider_call' for e in events),1)
        self.assertEqual([e['sequence'] for e in events],list(range(1,len(events)+1)))
        self.assertTrue(result['result']['local_stopped'])
        self.assertIsNone(result['result']['remote_stopped'])
        artifacts = service.artifacts(first['job_id'])
        self.assertFalse(artifacts['applied'])
        self.assertIn('work/value.txt',[a['relative_path'] for a in artifacts['artifacts']])
        (Path(artifacts['run_root'])/'work/value.txt').write_text('changed later')
        with self.assertRaisesRegex(ValueError,'artifact_changed'):
            service.artifacts(first['job_id'])

    @unittest.skipUnless(os.name == 'nt','Windows v1 containment')
    def test_conflict_and_client_isolation(self):
        service = self.start()
        accepted = service.submit(self.request)
        request = copy.deepcopy(self.request)
        request['task']['request'] = 'different'
        with self.assertRaisesRegex(ValueError,'idempotency_conflict'):
            service.submit(request)
        with self.assertRaisesRegex(ValueError,'job_not_found'):
            service.store.get('another-client',accepted['job_id'])
        with self.assertRaisesRegex(ValueError,'already_running'):
            JobService(self.cfg,'another-client')

    @unittest.skipUnless(os.name == 'nt','Windows v1 containment')
    def test_timeout_covers_blocking_provider_and_descendant(self):
        service = self.start()
        request = copy.deepcopy(self.request)
        request['task']['request'] = 'sleep'
        request['limits']['max_runtime'] = 2
        identifier = service.submit(request)['job_id']
        result = self.finish(identifier)
        self.assertEqual(result['state'],'timed_out',result)
        self.assertTrue(result['result']['local_stopped'])
        self.assertLess(result['result']['runtime'],8)
        pid_file = self.cfg.state_dir/'execution/fixture-child.pid'
        self.assertTrue(pid_file.exists())
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        handle = kernel.OpenProcess(0x100000,False,int(pid_file.read_text()))
        if handle:
            kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE,wintypes.DWORD]
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            self.assertEqual(kernel.WaitForSingleObject(handle,1000),0)
            kernel.CloseHandle(handle)

    @unittest.skipUnless(os.name == 'nt','Windows v1 containment')
    def test_cancel_is_request_then_local_fact_and_no_replay(self):
        service = self.start()
        request = copy.deepcopy(self.request)
        request['task']['request'] = 'sleep'
        identifier = service.submit(request)['job_id']
        service.cancel(identifier)
        result = self.finish(identifier)
        self.assertEqual(result['state'],'cancelled',result)
        self.assertIsNone(result['result']['remote_stopped'])
        self.assertEqual(service.submit(request)['job_id'],identifier)
        self.assertEqual(service.cancel(identifier)['state'],'cancelled')

    @unittest.skipUnless(os.name == 'nt','Windows v1 containment')
    def test_verifier_script_cannot_be_rewritten_into_pass(self):
        self.cfg.workspaces['demo']['write_files'].append('check.py')
        self.request['task']['allowed_files'].append('check.py')
        self.request['task']['request'] = 'tamper_check'
        self.start()
        result = self.finish(self.service.submit(self.request)['job_id'])
        self.assertNotEqual(result['state'],'verified',result)
        self.assertFalse(list(self.cfg.state_dir.rglob('tampered-check-ran.txt')))

    def test_restart_marks_unknown_and_blocks_new_dispatch(self):
        store = JobStore(self.cfg.state_dir/'jobs.sqlite3')
        normalized,_,_ = validate_submit(self.cfg,self.request)
        identifier,_ = store.admit('moxiaoxi',normalized,fingerprint(normalized))
        store.close()
        self.start()
        self.assertEqual(self.service.get(identifier)['state'],'unknown')
        self.assertEqual(self.service.submit(self.request)['job_id'],identifier)
        request = copy.deepcopy(self.request)
        request.update(client_task_id='parent-2',idempotency_key='input-2')
        with self.assertRaisesRegex(ValueError,'quarantined'):
            self.service.submit(request)

    @unittest.skipUnless(os.name == 'nt','Windows v1 containment')
    def test_required_review_consumes_same_provider_budget(self):
        self.request['task']['profile'].update(production=True,failure_blast_radius=3)
        self.request['limits']['max_provider_calls'] = 1
        service = self.start()
        identifier = service.submit(self.request)['job_id']
        result = self.finish(identifier)
        self.assertNotEqual(result['state'],'verified',result)
        self.assertEqual(result['result']['provider_calls'],1)
        events = service.store.events('moxiaoxi',identifier,0,2)
        self.assertTrue(events['has_more'])
        later = service.store.events('moxiaoxi',identifier,events['next_cursor'],100)
        self.assertGreater(later['events'][0]['sequence'],events['next_cursor'])
        with self.assertRaisesRegex(ValueError,'cursor_ahead'):
            service.store.events('moxiaoxi',identifier,100000,100)

    @unittest.skipUnless(os.name == 'nt','Windows v1 containment')
    def test_total_deadline_also_stops_verifier(self):
        self.cfg.commands['check'] = [sys.executable,'-B','-c','import time; time.sleep(120)']
        self.request['limits']['max_runtime'] = 2
        service = self.start()
        identifier = service.submit(self.request)['job_id']
        result = self.finish(identifier)
        self.assertEqual(result['state'],'timed_out',result)
        self.assertTrue(result['result']['local_stopped'])
        self.assertLess(result['result']['runtime'],8)

    def test_wire_version_schema_and_duplicate_key_validation(self):
        requests = [dict(jsonrpc='2.0',id=1,method='job.get',params={'job_id':'x'}),
            dict(jsonrpc='2.0',id=2,method='initialize',params={'protocol':'other/v1'}),
            dict(jsonrpc='2.0',id=3,method='initialize',params={'protocol':'router.jobs/v1'}),
            dict(jsonrpc='2.0',id=4,method='job.cancel',params={'job_id':'x','principal':'admin'})]
        source = ('\n'.join(canonical(r) for r in requests)+'\n{"id":1,"id":2}\n').encode()
        output = io.StringIO()
        serve(self.cfg,'fixture',io.BytesIO(source),output)
        rows = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(rows[0]['error']['message'],'initialize_required')
        self.assertEqual(rows[1]['error']['message'],'incompatible_protocol')
        self.assertEqual(rows[2]['result']['protocol'],'router.jobs/v1')
        self.assertEqual(rows[3]['error']['message'],'method_fields_must_match_v1')
        self.assertEqual(rows[4]['error']['message'],'duplicate_json_key')

    def test_real_stdio_client_can_describe_without_models(self):
        with JobClient([sys.executable,'-B','-X','utf8','-m','model_router','--state-dir',str(self.cfg.state_dir),
                        'serve','--client-id','moxiaoxi'],cwd=ROOT) as client:
            self.assertEqual(client.description['protocol'],'router.jobs/v1')
            self.assertFalse(client.description['features']['dsh_standard_conformance'])
            self.assertTrue(client.call('catalog.list')['models'])
            self.assertTrue(client.description['features']['lookup_by_parent'])
            self.assertEqual(client.call('job.lookup',{'client_task_id':'absent'}),{'found':False,'job':None})

    def test_parent_lookup_is_scoped_read_only_and_never_dispatches(self):
        normalized,_,_=validate_submit(self.cfg,self.request)
        store=JobStore(self.cfg.state_dir/'jobs.sqlite3')
        identifier,_=store.admit('moxiaoxi',normalized,fingerprint(normalized))
        store.close()
        service=self.start()
        found=service.lookup(self.request['client_task_id'])
        self.assertEqual(found['job']['job_id'],identifier)
        self.assertEqual(found['job']['state'],'unknown')
        self.assertIsNone(service.thread)
        service.client='another-host'
        self.assertFalse(service.lookup(self.request['client_task_id'])['found'])
        self.assertEqual(service.store.db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0],1)
