"""Describe without inference by default; --execute runs the authorized demo."""
import argparse
import json
from pathlib import Path
import sys
import time
from model_router.job_client import JobClient
from model_router.job_contract import TERMINAL

root = Path(__file__).resolve().parents[1]

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    with JobClient([sys.executable, '-B', '-X', 'utf8', '-m', 'model_router', '--config', str(root/'examples/host-v1.toml'),
                    'serve', '--client-id', 'demo-host'], cwd=root) as client:
        print(json.dumps(client.description, ensure_ascii=False, indent=2))
        if not args.execute:
            return
        request = json.loads((root/'examples/host-v1-request.json').read_text(encoding='utf-8'))
        job = client.call('job.submit', request)
        cursor = 0
        end = time.monotonic() + 140
        while time.monotonic() < end:
            page = client.call('job.events', dict(job_id=job['job_id'], after=cursor, limit=50))
            cursor = page['next_cursor']
            for event in page['events']:
                print(json.dumps(event,ensure_ascii=False))
            job = client.call('job.get',dict(job_id=job['job_id']))
            if job['state'] in TERMINAL:
                print(json.dumps(job,ensure_ascii=False,indent=2))
                print(json.dumps(client.call('job.artifacts',dict(job_id=job['job_id'])),ensure_ascii=False,indent=2))
                return
            time.sleep(.2)
        client.call('job.cancel',dict(job_id=job['job_id']))
        raise TimeoutError('Demo observation deadline')

if __name__ == '__main__':
    main()
