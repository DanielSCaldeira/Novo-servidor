"""Local fallback for missed GitHub cron events. No OCI access or stored token."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess

REPO = 'DanielSCaldeira/Novo-servidor'
WORKFLOW = 'oracle-vps.yml'


def gh(*args):
    executable = shutil.which('gh')
    if not executable:
        raise RuntimeError('GitHub CLI unavailable')
    result = subprocess.run(
        [executable, *args], capture_output=True, text=True, timeout=45,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    if result.returncode:
        # Do not print API responses, environment or authentication details.
        raise RuntimeError('GitHub CLI request failed')
    return result.stdout


def reconcile(call=gh, now=None, dry_run=False):
    now = now or datetime.now(timezone.utc)
    workflow = json.loads(call('api', f'repos/{REPO}/actions/workflows/{WORKFLOW}'))
    if workflow['state'] != 'active':
        return 'Workflow inactive; no dispatch'
    variables = json.loads(call('variable', 'list', '-R', REPO, '--json', 'name,value'))
    values = {item['name']: item['value'] for item in variables}
    if any(values.get(name) != 'true' for name in ('RETRY_ENABLED', 'ZERO_COST_CONFIRMED')):
        return 'Retry flags disabled; no dispatch'
    runs = json.loads(call('run', 'list', '-R', REPO, '--workflow', WORKFLOW,
                          '--limit', '20', '--json', 'status,createdAt'))
    if any(run['status'] != 'completed' for run in runs):
        return 'Run already queued or active; no dispatch'
    if runs:
        newest = max(datetime.fromisoformat(run['createdAt'].replace('Z', '+00:00')) for run in runs)
        if (now - newest).total_seconds() < 9 * 60:
            return 'Recent run; no dispatch'
    if dry_run:
        return 'Dry run: dispatch needed'
    call('workflow', 'run', WORKFLOW, '-R', REPO,
         '-f', 'diagnostic=false', '-f', 'configure_boot_disk=false',
         '-f', 'test_notification=false', '-f', 'resume_blocked=false')
    return 'Dispatched retry workflow'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    try:
        message = reconcile(dry_run=args.dry_run)
        code = 0
    except Exception:
        message, code = 'Watchdog failed; no automatic resume or re-enable attempted', 1
    folder = Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'Novo-servidor'
    folder.mkdir(parents=True, exist_ok=True)
    log = folder / 'dispatch-watchdog.log'
    # Keep the local status log bounded, with no credentials or API bodies.
    if log.exists() and log.stat().st_size > 1024 * 1024:
        log.write_text('', encoding='utf-8')
    with log.open('a', encoding='utf-8') as stream:
        stream.write(f'{datetime.now(timezone.utc).isoformat()} {message}\n')
    print(message)
    return code


if __name__ == '__main__':
    raise SystemExit(main())
