import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from model_router.conversations import Conversations
from model_router.models import Event
from model_router.orchestrator import Orchestrator
from model_router.planner import Planner
from model_router.webapp import App, ROOT


class ConversationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.app = App(ROOT / 'router.example.toml', self.root / 'ui')
        self.app.config.auto_usage = False
        self.env = patch.dict(os.environ, {'DEEPSEEK_API_KEY': ''})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def wait(self, predicate):
        end = time.monotonic() + 8
        while time.monotonic() < end:
            if predicate():
                return
            time.sleep(.02)
        self.fail('background job did not complete')

    def conversation(self, message='制作一个简洁的任务清单页面', **kwargs):
        identifier = self.app.message({'message': message, **kwargs})['conversation_id']
        self.wait(lambda: not self.app.busy)
        return self.app.conversation_get(identifier)

    def test_prompt_only_produces_editable_plan_and_persists_chat(self):
        c = self.conversation()
        self.assertEqual(c['status'], 'ready')
        self.assertEqual(c['repo'], '')
        self.assertEqual(c['proposals'][0]['allowed_files'], ['*'])
        self.assertEqual(c['proposals'][0]['source'], 'template')
        self.assertFalse(list((self.app.config.state_dir / 'runs').glob('*')))
        c = self.conversation('改用蓝色，并保留之前的任务清单功能', conversation_id=c['id'])
        self.assertEqual(len(c['messages']), 4)
        self.assertEqual(len(c['proposals']), 2)
        loaded = App(ROOT / 'router.example.toml', self.app.state).conversation_get(c['id'])
        self.assertEqual(c, loaded)

    def test_two_round_research_reads_only_selected_safe_file_and_keeps_context(self):
        repo = self.root / 'repo'
        repo.mkdir()
        (repo / 'app.py').write_text('existing implementation')
        (repo / '.env').write_text('private-secret')
        c = dict(messages=[{'role':'user','content':'原始目标'}, {'role':'assistant','content':'初版方案'},
                           {'role':'user','content':'保留原始目标，并增强验收'}], repo=str(repo), proposals=[], attachments=[])
        sample = Planner(self.app.config).fallback('目标', {}, [])
        sample['recommended_model'] = 'sol_high'
        rounds = [{'read_files':['app.py','.env','../outside'], 'approaches':['A','B']}, sample]
        calls = []
        def completion(settings, model, messages, **kwargs):
            calls.append((settings, messages, kwargs))
            return {'choices':[{'message':{'content':json.dumps(rounds[len(calls)-1])}}]}
        with patch.dict(os.environ, {'DEEPSEEK_API_KEY':'fixture'}), patch('model_router.planner.chat_completion',side_effect=completion):
            result = Planner(self.app.config).generate(c, {}, lambda _:None, threading.Event())
        self.assertEqual(len(calls), 2)
        second = json.loads(calls[1][1][1]['content'])
        self.assertEqual(second['file_excerpts'], {'app.py':'existing implementation'})
        self.assertEqual(len(second['conversation']), 3)
        self.assertEqual(calls[0][0]['thinking'], 'enabled')
        self.assertEqual(calls[0][2]['timeout'], 180)
        self.assertIn('已读取：app.py', result['sources'])
        self.assertNotIn('private-secret', json.dumps([call[1] for call in calls]))

    def test_edited_prompt_model_and_scope_reach_execution_and_repeat_publish_is_idempotent(self):
        c = self.conversation()
        self.app.config.commands = {'check':[sys.executable,'-B','-c',"from pathlib import Path; assert Path('deliverable.txt').read_text() == 'done'"]}
        edited = self.app.save_proposal({'conversation_id':c['id'],'revision':1,'edits':{
            'execution_prompt':'审阅后明确要求：只生成 deliverable.txt', 'recommended_model':'sol_medium',
            'allowed_files':['deliverable.txt'],'checks':['check'], 'plugins':['无插件需求'],
            'permissions_notes':['仅写入指定交付物']}})['proposal']
        seen = []
        gate = threading.Event()
        class Executor:
            def run(self, **kw):
                seen.append(kw)
                gate.wait(3)
                (kw['work']/'deliverable.txt').write_text('done')
                kw['emit'](Event('summary',{'text':'已交付 deliverable.txt'}))
                kw['emit'](Event('session',{'thread_id':'11111111-1111-1111-1111-111111111111'}))
        def factory(cfg, **kw):
            return Orchestrator(cfg, provider_factory=lambda *_:Executor(), **kw)
        with patch('model_router.webapp.Orchestrator',side_effect=factory):
            payload = {'conversation_id':c['id'],'revision':edited['revision']}
            first = self.app.publish(payload)
            second = self.app.publish(payload)
            self.assertEqual(first['job_id'],second['job_id'])
            gate.set()
            self.wait(lambda:self.app.active is None)
        self.assertEqual(len(seen),1)
        self.assertEqual(seen[0]['model'].id,'sol_medium')
        self.assertIn('审阅后明确要求：只生成 deliverable.txt',seen[0]['prompt'])
        self.assertIn('仅写入指定交付物',seen[0]['prompt'])
        job = self.app.job(first['job_id'])
        self.assertEqual(job['status'],'verified',job)
        c = self.app.conversation_get(c['id'])
        self.assertEqual(c['publications'][0]['run_id'],job['run_id'])
        record = self.app.execution(job['run_id'])
        self.assertIn('deliverable.txt',record['outputs'])
        self.assertEqual(record['sessions'],['11111111-1111-1111-1111-111111111111'])
        self.assertEqual(len(self.app.conversations_list()['conversations']),1)
        with self.assertRaises(ValueError):
            self.app.save_proposal({'conversation_id':c['id'],'revision':1,'edits':{}})

    def test_scope_attachment_and_checks_cannot_escape_validation(self):
        with self.assertRaises(ValueError):
            self.app.message({'message':'x','attachments':[{'name':'../private.txt','content':'x'}]})
        c = self.conversation()
        for edits in ({'allowed_files':['../outside']},{'checks':['unconfigured']},{'execution_prompt':''}):
            with self.assertRaises(ValueError):
                self.app.save_proposal({'conversation_id':c['id'],'revision':1,'edits':edits})
        with self.assertRaises(ValueError):
            self.app.conversation_get('../../outside')

    def test_recovery_never_republishes_an_interrupted_conversation(self):
        c = self.conversation()
        c['status']='running'
        self.app.conversations.save(c)
        app = App(ROOT/'router.example.toml',self.app.state)
        self.assertEqual(app.conversation_get(c['id'])['status'],'interrupted')
        self.assertIsNone(app.active)

    def test_failed_research_keeps_message_and_can_be_continued(self):
        with patch('model_router.conversation_api.Planner.generate',side_effect=ValueError('fixture provider failed')):
            c = self.conversation()
        self.assertEqual(c['status'],'error')
        self.assertIn('fixture provider failed',c['messages'][-1]['content'])
        c = self.conversation('继续分析',conversation_id=c['id'])
        self.assertEqual(c['status'],'ready')
        self.assertEqual(len(c['messages']),4)

    def test_research_cancelled_without_losing_conversation(self):
        def generation(c, options, progress, cancelled):
            cancelled.wait(3)
            return Planner(self.app.config).fallback('需求',{},[])
        with patch('model_router.conversation_api.Planner.generate',side_effect=generation):
            identifier=self.app.message({'message':'需求'})['conversation_id']
            self.app.planning_cancel[identifier].set()
            self.wait(lambda:not self.app.busy)
        c=self.app.conversation_get(identifier)
        self.assertEqual(c['status'],'cancelled')
        self.assertEqual(c['proposals'],[])

    def test_prompt_only_publish_creates_workspace_and_delivers_without_manual_file_rules(self):
        c = self.conversation(attachments=[{'name':'brief.md','content':'用户主动提供的资料'}])
        class Executor:
            def run(self, **kw):
                self.content = (kw['work']/'brief.md').read_text(encoding='utf-8')
                (kw['work']/'result.md').write_text('交付内容',encoding='utf-8')
                kw['emit'](Event('summary',{'text':'已生成 result.md'}))
        executor=Executor()
        with patch('model_router.webapp.Orchestrator',side_effect=lambda cfg,**kw:Orchestrator(cfg,provider_factory=lambda *_:executor,**kw)):
            r=self.app.publish({'conversation_id':c['id'],'revision':1})
            self.wait(lambda:self.app.active is None)
        job=self.app.job(r['job_id'])
        self.assertEqual(job['status'],'needs_human',job)
        self.assertEqual(executor.content,'用户主动提供的资料')
        self.assertIn('result.md',self.app.execution(job['run_id'])['outputs'])
        self.assertEqual(self.app.conversation_get(c['id'])['repo'],'')


if __name__ == '__main__':
    unittest.main()
