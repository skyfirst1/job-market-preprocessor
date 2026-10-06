# SPDX-License-Identifier: AGPL-3.0-only
"""Bounded browser-process capture; never changes Windows proxy settings."""

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CA_DIR = ROOT / '.wxas_ca'


def capture_command(pid):
    if pid <= 0:
        raise ValueError('An explicit positive browser network PID is required')
    return [
        sys.executable, '-u', '-c',
        'from mitmproxy.tools.main import mitmdump; mitmdump()',
        '--mode', f'local:{pid}',
        '--set', f'confdir={CA_DIR}',
        '--allow-hosts', r'^mp\.weixin\.qq\.com(?::\d+)?$',
        '--set', 'flow_detail=0',
        '-s', str(ROOT / 'wx_article_saver.py'),
    ]


def run(pid, seconds):
    from mitmproxy.certs import CertStore
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes

    CA_DIR.mkdir(exist_ok=True)
    CertStore.from_store(CA_DIR, 'mitmproxy', 2048)
    cert = CA_DIR / 'mitmproxy-ca-cert.cer'
    # Identify only this tool's CA; preserve roots that were already trusted.
    ca = x509.load_pem_x509_certificate((CA_DIR / 'mitmproxy-ca-cert.pem').read_bytes())
    fingerprint = ca.fingerprint(hashes.SHA1()).hex().upper()
    from coexist_launcher import ca_is_trusted
    existed = ca_is_trusted(fingerprint)
    proc = None
    try:
        if not existed:
            trusted = subprocess.run(['certutil', '-user', '-addstore', '-f', 'Root', str(cert)], capture_output=True)
            if trusted.returncode:
                raise RuntimeError('Certificate trust failed; no fallback attempted')
            if not ca_is_trusted(fingerprint):
                raise RuntimeError('Certificate is not trusted after confirmation')
        print(f'Browser network PID: {pid}; capture limit: {seconds}s', flush=True)
        print('SYSTEM PROXY UNCHANGED. Clash and Codex are not capture targets.', flush=True)
        print('Approve the Windows redirector prompt manually if shown. Ctrl+C stops capture.', flush=True)
        env = os.environ.copy()
        env['WXAS_EXPORT_DIR'] = str(ROOT / 'exports')
        with (ROOT / 'local_capture.log').open('a', encoding='utf-8') as log:
            proc = subprocess.Popen(capture_command(pid), cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
            deadline = time.monotonic() + seconds
            while proc.poll() is None and time.monotonic() < deadline:
                time.sleep(.5)
            if proc.poll() is not None and proc.returncode:
                raise RuntimeError(f'Capture failed ({proc.returncode}); inspect local_capture.log')
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        print('CA trust retained by user request; no automatic certificate deletion.', flush=True)
        print('Capture stopped; system proxy was never modified.', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pid', required=True, type=int, help='Exact WeChat browser network-service PID; no all-process mode')
    parser.add_argument('--seconds', type=int, default=180, help='Capture duration, 1..300 seconds; default 180')
    args = parser.parse_args()
    if args.pid <= 0 or not 1 <= args.seconds <= 300:
        parser.error('Require positive PID and seconds in 1..300')
    try:
        run(args.pid, args.seconds)
    except KeyboardInterrupt:
        print('Stopped by user.', flush=True)
    except Exception as exc:
        print(f'ERROR: {exc}', flush=True)
    input('Supervision window retained. Press Enter to close only this window.')


if __name__ == '__main__':
    main()
