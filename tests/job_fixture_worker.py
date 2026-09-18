"""Explicitly synthetic provider used only by tests; production CLI cannot select it."""
import json
from pathlib import Path
import subprocess
import sys
import time

from model_router import job_worker
from model_router.models import Event


class FixtureProvider:
    def run(self, **kw):
        mode = kw['task'].request
        if mode == 'sleep':
            # Child must be contained even when the provider blocks without callbacks.
            child = subprocess.Popen([sys.executable, '-B', '-c', 'import time; time.sleep(120)'])
            (kw['work'].parents[2] / 'fixture-child.pid').write_text(str(child.pid))
            time.sleep(120)
        if kw.get('review'):
            kw['emit'](Event('summary', {'text':'{"approved":true,"findings":[]}'}))
            return
        if mode == 'tamper_check':
            (kw['work'] / 'check.py').write_text("from pathlib import Path\nPath('tampered-check-ran.txt').write_text('bad')\n")
        (kw['work'] / 'value.txt').write_text('done', encoding='utf-8')
        kw['emit'](Event('summary', {'text':'fixture candidate, not a real model'}))


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    payload = json.load(sys.stdin)
    job_worker.get_provider = lambda *_: FixtureProvider()
    job_worker.execute(payload)
