"""router.jobs/v2: the closed v2 schema, the negotiated protocol, and provenance-style evidence.

The existing `tests/test_jobs.py` is deliberately not touched by this round: it is the v1 contract
suite, and the strongest statement about legacy safety is that it passes unchanged. Everything new
lives here, including the negative controls that prove the v2 rules can fail.
"""
from dataclasses import asdict
import io
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest

from model_router import __version__
from model_router.config import Config
from model_router.job_contract import (APPROVAL_FIELD, FIELDS, FIELDS_V2, MANIFEST_VERSION,
    PROTOCOL, PROTOCOL_V2, PROTOCOLS, SCHEMAS, TERMINAL, WORK_REVISION_FIELD, canonical,
    fingerprint, request_hash, stop_acknowledgement, validate_submit)
from model_router.job_store import JobStore
from model_router.jobs import JobService, serve
from model_router.models import Task, TaskProfile


class ProtocolV2Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / 'source'
        self.repo.mkdir()
        (self.repo / 'value.txt').write_text('before')
        (self.repo / 'check.py').write_text("from pathlib import Path\nassert Path('value.txt').read_text() == 'done'\n")
        self.cfg = Config(self.root / 'state', auto_usage=False, max_total_runtime=30, max_attempts=2,
            commands={'check': [sys.executable, '-B', 'check.py']})
        self.cfg.workspaces = {'demo': {'path': str(self.repo), 'read_files': ['value.txt', 'check.py'],
            'write_files': ['value.txt'], 'checks': ['check'], 'allowed_models': ['deepseek', 'sol_medium'],
            'upload_allowed': True}}
        profile = TaskProfile(task_type='batch_edit', requirement_clarity=5, automatic_verifiability=5,
            human_verification_cost=1, failure_blast_radius=1, tool_complexity=1, repo_scope=1,
            domain_uncertainty=0, root_cause_uncertainty=0, reversibility=5)
        task = Task('write', profile, allowed_files=['value.txt'], checks=['check'],
            acceptance_criteria=['value is done'], no_escalation=True)
        self.request = dict(client_task_id='parent-1', idempotency_key='input-1', session_id='conversation-1',
            workspace_id='demo', task=asdict(task), limits=dict(max_runtime=15, max_attempts=1, max_provider_calls=2),
            allowed_models=['deepseek', 'sol_medium'], upload_allowed=True)
        self.v2_request = {**self.request, WORK_REVISION_FIELD: 'rev-7',
            APPROVAL_FIELD: {'revision': 'rev-7', 'approval_id': 'approval-1', 'granted_at': '2026-09-21T00:00:00+07:00'}}
        self.service = None

    def tearDown(self):
        if self.service:
            self.service.close()
        self.temp.cleanup()

    def start(self):
        self.service = JobService(self.cfg, 'moxiaoxi',
            worker_command=[sys.executable, '-B', '-X', 'utf8', '-m', 'tests.job_fixture_worker'])
        return self.service

    def settle(self, identifier, seconds=15):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            row = self.service.get(identifier)
            if row['state'] in TERMINAL:
                return row
            time.sleep(.03)
        self.fail('job did not settle')

    # ------------------------------------------------------------------ contract

    def test_v1_digest_is_byte_identical_to_the_fingerprint_already_stored(self):
        """The whole point of the v1 rule: a pre-upgrade retry must not look like a conflict."""
        normalized, _, _ = validate_submit(self.cfg, self.request)
        self.assertEqual(request_hash(normalized, PROTOCOL), fingerprint(normalized))
        payload = {k: v for k, v in normalized.items() if k != APPROVAL_FIELD}
        self.assertEqual(request_hash(normalized, PROTOCOL), fingerprint(payload))

    def test_both_versions_are_closed_and_reject_the_other_shape(self):
        """No optional keys in either direction: a v1 connection cannot smuggle a v2 key in."""
        validate_submit(self.cfg, self.request, protocol=PROTOCOL)
        validate_submit(self.cfg, self.v2_request, protocol=PROTOCOL_V2)
        with self.assertRaisesRegex(ValueError, 'submit_fields_must_match_v1'):
            validate_submit(self.cfg, self.v2_request, protocol=PROTOCOL)
        with self.assertRaisesRegex(ValueError, 'submit_fields_must_match_v2'):
            validate_submit(self.cfg, self.request, protocol=PROTOCOL_V2)
        with self.assertRaisesRegex(ValueError, 'submit_fields_must_match_v1'):
            validate_submit(self.cfg, {**self.request, 'approval': None}, protocol=PROTOCOL)
        self.assertEqual(SCHEMAS[PROTOCOL], FIELDS)
        self.assertEqual(SCHEMAS[PROTOCOL_V2], FIELDS_V2)
        self.assertEqual(FIELDS_V2 - FIELDS, {WORK_REVISION_FIELD, APPROVAL_FIELD})
        with self.assertRaisesRegex(ValueError, 'unsupported_protocol'):
            validate_submit(self.cfg, self.request, protocol='router.jobs/v9')

    def test_v2_approval_must_bind_the_exact_work_revision(self):
        for approval, expected in (
                ({'revision': 'rev-8', 'approval_id': 'approval-1', 'granted_at': 'now'}, 'approval_revision_mismatch'),
                ({'revision': 'rev-7', 'approval_id': 'approval-1'}, 'invalid_approval'),
                ({'revision': 'rev-7', 'approval_id': 'approval-1', 'granted_at': '', }, 'invalid_approval_granted_at'),
                ('approved', 'invalid_approval')):
            with self.assertRaisesRegex(ValueError, expected):
                validate_submit(self.cfg, {**self.v2_request, APPROVAL_FIELD: approval}, protocol=PROTOCOL_V2)

    def test_v2_digest_ignores_reapproval_metadata_and_tracks_the_revision(self):
        """Re-approving the same work package is a retry; a new revision is a different request."""
        first, _, _ = validate_submit(self.cfg, self.v2_request, protocol=PROTOCOL_V2)
        reapproved, _, _ = validate_submit(self.cfg, {**self.v2_request, APPROVAL_FIELD: {
            'revision': 'rev-7', 'approval_id': 'approval-2', 'granted_at': '2026-09-21T01:00:00+07:00'}},
            protocol=PROTOCOL_V2)
        other, _, _ = validate_submit(self.cfg, {**self.v2_request, WORK_REVISION_FIELD: 'rev-8',
            APPROVAL_FIELD: {'revision': 'rev-8', 'approval_id': 'approval-1', 'granted_at': 'now'}},
            protocol=PROTOCOL_V2)
        self.assertEqual(request_hash(first, PROTOCOL_V2), request_hash(reapproved, PROTOCOL_V2))
        self.assertNotEqual(request_hash(first, PROTOCOL_V2), request_hash(other, PROTOCOL_V2))
        v1, _, _ = validate_submit(self.cfg, self.request, protocol=PROTOCOL)
        self.assertNotEqual(request_hash(v1, PROTOCOL), request_hash(first, PROTOCOL_V2))

    def test_stop_acknowledgement_never_claims_a_remote_stop(self):
        self.assertEqual(stop_acknowledgement('cancel_requested', None, requested=True),
            {'local': 'pending', 'remote': 'unknown'})
        self.assertEqual(stop_acknowledgement('running', None),
            {'local': 'not_requested', 'remote': 'unknown'})
        self.assertEqual(stop_acknowledgement('cancelled', {'local_stopped': True}),
            {'local': 'confirmed', 'remote': 'unknown'})
        self.assertEqual(stop_acknowledgement('cancelled', {'local_stopped': False}),
            {'local': 'unconfirmed', 'remote': 'unknown'})
        self.assertEqual(stop_acknowledgement('unknown', {'local_stopped': None}),
            {'local': 'unconfirmed', 'remote': 'unknown'})

    # ------------------------------------------------------------------ declaration

    def test_describe_declares_the_version_set_and_the_new_capabilities(self):
        # Through the fixture's own service, so its store is closed in tearDown rather than
        # holding the temporary database open on Windows.
        description = self.start().describe()
        self.assertEqual(description['protocol'], PROTOCOL)
        self.assertEqual(description['protocol_versions'], list(PROTOCOLS))
        self.assertEqual(description['server_version'], __version__)
        self.assertTrue(description['features']['stop_acknowledgement'])
        self.assertTrue(description['features']['protocol_upgrade'])
        self.assertEqual(description['cancellation']['remote_provider'], 'not_observable_in_v2')
        self.assertEqual(description['manifest_version'], MANIFEST_VERSION)
        # The v1 capabilities the caller already depends on are unchanged.
        self.assertTrue(description['features']['durable_idempotency'])
        self.assertFalse(description['features']['dsh_standard_conformance'])
        self.assertEqual(description['cancellation']['local'], 'windows_job_object')

    def test_handshake_accepts_both_versions_and_upgrades_on_a_second_initialize(self):
        requests = [dict(jsonrpc='2.0', id=1, method='initialize', params={'protocol': PROTOCOL}),
            dict(jsonrpc='2.0', id=2, method='initialize', params={'protocol': PROTOCOL_V2}),
            dict(jsonrpc='2.0', id=3, method='initialize', params={'protocol': 'router.jobs/v9'}),
            dict(jsonrpc='2.0', id=4, method='initialize', params={'protocol': PROTOCOL_V2, 'extra': 1})]
        source = ('\n'.join(canonical(r) for r in requests) + '\n').encode()
        output = io.StringIO()
        serve(self.cfg, 'fixture', io.BytesIO(source), output)
        rows = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(rows[0]['result']['protocol'], PROTOCOL)
        self.assertEqual(rows[1]['result']['protocol'], PROTOCOL_V2)
        self.assertEqual(rows[1]['result']['protocol_versions'], list(PROTOCOLS))
        self.assertEqual(rows[2]['error']['message'], 'incompatible_protocol')
        self.assertEqual(rows[3]['error']['message'], 'incompatible_protocol')

    # ------------------------------------------------------------------ service

    def test_v2_submit_records_protocol_and_revision_and_replays_without_executing_twice(self):
        service = self.start()
        first = service.submit(self.v2_request, protocol=PROTOCOL_V2)
        self.assertFalse(first['duplicate'])
        row = service.store.db.execute('SELECT protocol,work_revision,approval FROM jobs WHERE id=?',
            (first['job_id'],)).fetchone()
        self.assertEqual(row['protocol'], PROTOCOL_V2)
        self.assertEqual(row['work_revision'], 'rev-7')
        self.assertEqual(json.loads(row['approval'])['approval_id'], 'approval-1')
        reapproved = service.submit({**self.v2_request, APPROVAL_FIELD: {'revision': 'rev-7',
            'approval_id': 'approval-2', 'granted_at': 'later'}}, protocol=PROTOCOL_V2)
        self.assertTrue(reapproved['duplicate'])
        self.assertEqual(reapproved['job_id'], first['job_id'])
        self.assertEqual(service.store.db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 1)
        with self.assertRaisesRegex(ValueError, 'idempotency_conflict'):
            service.submit({**self.v2_request, WORK_REVISION_FIELD: 'rev-9',
                APPROVAL_FIELD: {'revision': 'rev-9', 'approval_id': 'approval-1', 'granted_at': 'now'}},
                protocol=PROTOCOL_V2)
        self.assertEqual(service.store.db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 1)

    def test_an_existing_v1_job_is_rejected_not_duplicated_when_retried_as_v2(self):
        """The approved conservative rule: the two digest spaces cannot match, so it conflicts."""
        service = self.start()
        first = service.submit(self.request, protocol=PROTOCOL)
        with self.assertRaisesRegex(ValueError, 'idempotency_conflict'):
            service.submit({**self.v2_request, 'client_task_id': self.request['client_task_id']},
                protocol=PROTOCOL_V2)
        self.assertEqual(first['duplicate'], False)
        self.assertEqual(service.store.db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 1)

    def test_settlement_and_cancel_carry_the_acknowledgement_and_the_settled_schema_version(self):
        service = self.start()
        submitted = service.submit(self.v2_request, protocol=PROTOCOL_V2)
        pending = service.cancel(submitted['job_id'])
        self.assertEqual(pending['stop_acknowledgement']['remote'], 'unknown')
        settled = self.settle(submitted['job_id'])
        self.assertIn(settled['state'], TERMINAL, settled)
        self.assertEqual(settled['result']['stop_acknowledgement']['remote'], 'unknown')
        self.assertEqual(settled['result']['stop_acknowledgement']['local'],
            'confirmed' if settled['result']['local_stopped'] is True else 'unconfirmed')
        self.assertIsNone(settled['result']['remote_stopped'])
        schema = service.store.db.execute('SELECT settlement_schema_version FROM jobs WHERE id=?',
            (submitted['job_id'],)).fetchone()[0]
        self.assertEqual(schema, 2)
        # A v1 job settles with the v1 settlement schema version, and a second cancel stays idempotent.
        v1 = service.submit({**self.request, 'client_task_id': 'parent-2', 'idempotency_key': 'input-2'},
            protocol=PROTOCOL)
        v1_settled = self.settle(v1['job_id'])
        self.assertEqual(service.store.db.execute('SELECT settlement_schema_version FROM jobs WHERE id=?',
            (v1['job_id'],)).fetchone()[0], 1)
        self.assertEqual(v1_settled['result']['stop_acknowledgement']['remote'], 'unknown')
        again = service.cancel(v1['job_id'])
        self.assertFalse(again['cancel_requested'])

    def test_artifacts_carry_the_manifest_version_in_both_branches(self):
        service = self.start()
        submitted = service.submit({**self.request, 'client_task_id': 'parent-3', 'idempotency_key': 'input-3'},
            protocol=PROTOCOL)
        early = service.artifacts(submitted['job_id'])
        self.assertEqual(early['manifest_version'], MANIFEST_VERSION)
        self.assertEqual(early['artifacts'], [])
        settled = self.settle(submitted['job_id'])
        self.assertIn(settled['state'], TERMINAL, settled)
        final = service.artifacts(submitted['job_id'])
        self.assertEqual(final['manifest_version'], MANIFEST_VERSION)
        self.assertIn('run_root', final)
        self.assertTrue(all(set(row) == {'ref', 'relative_path', 'sha256', 'bytes'} for row in final['artifacts']))

    def test_an_old_database_is_migrated_in_place_without_touching_existing_rows(self):
        """Append-only migration: the v0.4.1 schema keeps working and gains the new columns."""
        path = self.cfg.state_dir / 'jobs.sqlite3'
        store = JobStore(path)
        store.db.execute("DROP TABLE jobs")
        store.db.executescript("""
            CREATE TABLE jobs(
                id TEXT PRIMARY KEY, client TEXT NOT NULL, task_id TEXT NOT NULL, idem TEXT NOT NULL,
                request_hash TEXT NOT NULL, request TEXT NOT NULL, state TEXT NOT NULL,
                created REAL NOT NULL, updated REAL NOT NULL, result TEXT, run_id TEXT,
                UNIQUE(client,task_id), UNIQUE(client,idem));
        """)
        store.db.execute("INSERT INTO jobs VALUES('legacy','moxiaoxi','old-task','old-idem','hash','{}','accepted',1,1,NULL,NULL)")
        store.db.commit()
        store.close()
        reopened = JobStore(path)
        columns = {row['name'] for row in reopened.db.execute('PRAGMA table_info(jobs)').fetchall()}
        self.assertTrue({'protocol', 'work_revision', 'approval', 'settlement_schema_version'} <= columns)
        row = reopened.db.execute('SELECT protocol,work_revision FROM jobs WHERE id=?', ('legacy',)).fetchone()
        self.assertEqual(row['protocol'], PROTOCOL)
        self.assertIsNone(row['work_revision'])
        self.assertEqual(reopened.db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 1)
        # Idempotent: migrating twice is a no-op rather than an error.
        reopened._migrate()
        reopened.close()
