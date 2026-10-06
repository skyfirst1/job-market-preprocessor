# SPDX-License-Identifier: AGPL-3.0-only
"""Bounded selective PAC trial that preserves the existing Clash proxy."""

import argparse
import ctypes
import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BACKUP = ROOT / 'coexist_proxy_backup.json'
STATUS = ROOT / 'coexist_status.json'
KEY = r'Software\Microsoft\Windows\CurrentVersion\Internet Settings'
PAC_PORT = 8918
PAC_URL = f'http://127.0.0.1:{PAC_PORT}/proxy.pac'


def pac_text():
    return r'''function FindProxyForURL(url, host) {
  host = host.toLowerCase();
  if (host === "localhost" || host === "::1" || shExpMatch(host, "127.*") ||
      shExpMatch(host, "10.*") || shExpMatch(host, "192.168.*") ||
      /^172\.(1[6-9]|2[0-9]|3[01])\./.test(host)) return "DIRECT";
  if (host === "mp.weixin.qq.com") return "PROXY 127.0.0.1:8899; PROXY 127.0.0.1:7897";
  return "PROXY 127.0.0.1:7897";
}
'''


def refresh_settings():
    for option in (39, 37):
        ctypes.windll.Wininet.InternetSetOptionW(0, option, 0, 0)


def status(phase, **details):
    STATUS.write_text(json.dumps({'phase': phase, **details}), encoding='utf-8')
    print(phase, flush=True)


class PacHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != '/proxy.pac':
            self.send_error(404)
            return
        body = pac_text().encode('ascii')
        self.send_response(200)
        self.send_header('Content-Type', 'application/x-ns-proxy-autoconfig')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def read_value(key, name):
    import winreg
    try:
        return winreg.QueryValueEx(key, name)[0]
    except FileNotFoundError:
        return None


def ca_is_trusted(fingerprint):
    if len(fingerprint) != 40 or any(c not in '0123456789ABCDEFabcdef' for c in fingerprint):
        raise ValueError('Invalid certificate fingerprint')
    path = f'Cert:\\CurrentUser\\Root\\{fingerprint}'
    check = subprocess.run(['powershell.exe', '-NoProfile', '-Command',
        f"if (Test-Path -LiteralPath '{path}') {{ exit 0 }} else {{ exit 1 }}"],
        capture_output=True, timeout=15)
    if check.returncode not in (0, 1):
        raise RuntimeError('Cannot inspect root certificate trust')
    return check.returncode == 0


def check_capture_log(log, start):
    with log.open('r', encoding='utf-8', errors='replace') as stream:
        stream.seek(start)
        text = stream.read()
    if 'Client TLS handshake failed' in text:
        raise RuntimeError('Client rejected capture certificate; stopping without retry')


def validate_proxy(state):
    if state['ProxyEnable'] != 1 or state['ProxyServer'] != '127.0.0.1:7897':
        raise RuntimeError('Expected existing enabled Clash proxy at 127.0.0.1:7897')
    if state['AutoConfigURL']:
        raise RuntimeError('Existing PAC found; refusing to overwrite it')


def restore_pac(old):
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, KEY, 0, winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE) as key:
        current = read_value(key, 'AutoConfigURL')
        if current == PAC_URL:
            if old['AutoConfigURL'] is None:
                winreg.DeleteValue(key, 'AutoConfigURL')
            else:
                winreg.SetValueEx(key, 'AutoConfigURL', 0, winreg.REG_SZ, old['AutoConfigURL'])
        elif current != old['AutoConfigURL']:
            raise RuntimeError('PAC changed externally; backup retained for inspection')
    refresh_settings()
    BACKUP.unlink(missing_ok=True)


def run(seconds, auto_export_pilot=False):
    import winreg
    from mitmproxy.certs import CertStore
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes

    if BACKUP.exists():
        raise RuntimeError('Previous PAC backup remains; inspect and restore it first')
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, KEY) as key:
        old = {name: read_value(key, name) for name in ('ProxyEnable', 'ProxyServer', 'AutoConfigURL')}
    validate_proxy(old)
    with socket.create_connection(('127.0.0.1', 7897), timeout=2):
        pass
    ca_dir = ROOT / '.wxas_ca'
    CertStore.from_store(ca_dir, 'mitmproxy', 2048)
    ca = x509.load_pem_x509_certificate((ca_dir / 'mitmproxy-ca-cert.pem').read_bytes())
    fingerprint = ca.fingerprint(hashes.SHA1()).hex().upper()
    # Query the exact CurrentUser root key, not localized certutil output.
    pretrusted = ca_is_trusted(fingerprint)
    pac_changed = False
    server = None
    proc = None
    try:
        if not pretrusted:
            status('waiting_for_certificate_confirmation')
            result = subprocess.run(['certutil', '-user', '-addstore', '-f', 'Root', str(ca_dir / 'mitmproxy-ca-cert.cer')], capture_output=True)
            if result.returncode:
                raise RuntimeError('Certificate trust failed; no fallback')
            if not ca_is_trusted(fingerprint):
                raise RuntimeError('Certificate is not trusted after confirmation; proxy settings unchanged')
        server = ThreadingHTTPServer(('127.0.0.1', PAC_PORT), PacHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        env = os.environ.copy()
        env['WXAS_EXPORT_DIR'] = str(ROOT / 'exports')
        env['WXAS_AUTO_EXPORT_PILOT'] = '1' if auto_export_pilot else '0'
        env['WXAS_PILOT_GATE'] = str(ROOT / '.wxas_state' / 'pilot_export_gate.json')
        cmd = [sys.executable, '-u', '-c', 'from mitmproxy.tools.main import mitmdump; mitmdump()',
            '--listen-host', '127.0.0.1', '--listen-port', '8899',
            '--set', f'confdir={ca_dir}', '--set', 'flow_detail=0',
            '--allow-hosts', r'^mp\.weixin\.qq\.com(?::\d+)?$',
            '-s', str(ROOT / 'wx_article_saver.py')]
        with (ROOT / 'coexist_capture.log').open('a', encoding='utf-8') as log:
            log_start = log.tell()
            # Existing listener would falsely satisfy the readiness check.
            probe = socket.socket()
            try:
                probe.bind(('127.0.0.1', 8899))
            finally:
                probe.close()
            proc = subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
            deadline = time.monotonic() + 15
            while True:
                if proc.poll() is not None:
                    raise RuntimeError('Capture proxy exited during startup')
                try:
                    with socket.create_connection(('127.0.0.1', 8899), timeout=.5):
                        break
                except OSError:
                    if time.monotonic() > deadline:
                        raise RuntimeError('Capture proxy startup timed out')
                    time.sleep(.2)
            BACKUP.write_text(json.dumps(old), encoding='utf-8')
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, KEY, 0, winreg.KEY_SET_VALUE) as key:
                pac_changed = True
                winreg.SetValueEx(key, 'AutoConfigURL', 0, winreg.REG_SZ, PAC_URL)
            refresh_settings()
            status('running', seconds=seconds, clash='127.0.0.1:7897', pac=PAC_URL)
            deadline = time.monotonic() + seconds
            while proc.poll() is None and time.monotonic() < deadline:
                check_capture_log(ROOT / 'coexist_capture.log', log_start)
                time.sleep(.5)
            if proc.poll() is not None:
                raise RuntimeError('Capture proxy exited during trial')
    finally:
        # Restore routing before terminating either local server.
        if pac_changed:
            restore_pac(old)
            status('proxy_restored')
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        if server is not None:
            server.shutdown()
            server.server_close()
        print('CA trust retained by user request; no automatic certificate deletion.', flush=True)
        status('stopped')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds', type=int, default=180, help='Trial duration, 1..300 seconds; default 180')
    parser.add_argument('--auto-export-pilot', action='store_true', help='Auto-export only the selected CSV article, once; no automatic retry')
    args = parser.parse_args()
    if not 1 <= args.seconds <= 300:
        parser.error('seconds must be 1..300')
    try:
        run(args.seconds, args.auto_export_pilot)
    except KeyboardInterrupt:
        print('Stopped by user.', flush=True)
    except Exception as exc:
        status('error', message=str(exc))
    input('Supervision window retained. Press Enter to close only this window.')


if __name__ == '__main__':
    main()
