"""Verify an already-built release on an isolated GitHub Windows runner."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
from urllib.request import Request, urlopen
import zipfile


REPO = 'maksk03/67'
VERSION = os.environ['VERSION67']
assert re.fullmatch(r'\d{1,3}\.\d{1,3}\.\d{1,3}', VERSION)
MODE = os.environ.get('MODE67', 'preflight')
assert MODE in ('preflight', 'upgrade', 'simple')
ROOT = Path(os.environ['RUNNER_TEMP']) / '67-release-check'
ROOT.mkdir()
REPORT = {'version': VERSION, 'mode': MODE}
APP_ENV = {key: value for key, value in os.environ.items()
           if not re.search(r'TOKEN|SECRET|PASSWORD|CREDENTIAL', key, re.I)}
APP_ENV['LOCALAPPDATA'] = str(ROOT / 'profile')
PROFILE = Path(APP_ENV['LOCALAPPDATA']) / '67_local'
PROFILE.mkdir(parents=True)
SETTINGS = PROFILE / 'settings.json'
SETTINGS.write_text(json.dumps({'port': 9225, 'mini_width': 255, 'mini_side': 'left'}), encoding='utf-8')
SETTINGS_BEFORE = SETTINGS.read_bytes()


def api(path):
    result = subprocess.run(['gh', 'api', path], capture_output=True, check=True, encoding='utf-8')
    return json.loads(result.stdout)


def download(asset, destination):
    assert 0 < asset['size'] < 512 * 1024 * 1024
    with destination.open('wb') as output:
        subprocess.run(['gh', 'api', '-H', 'Accept: application/octet-stream',
                        f'repos/{REPO}/releases/assets/{asset["id"]}'], stdout=output, check=True)
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    assert destination.stat().st_size == asset['size']
    assert asset['digest'] == 'sha256:' + digest
    return destination


def unpack(archive, directory):
    directory.mkdir()
    with zipfile.ZipFile(archive) as bundle:
        for item in bundle.infolist():
            path = directory / item.filename
            assert not item.filename.startswith(('/', '\\')) and '\\' not in item.filename
            assert ':' not in item.filename and path.resolve().is_relative_to(directory.resolve())
            assert not (item.external_attr >> 16) & 0o170000 == 0o120000
        bundle.extractall(directory)


def processes(directory):
    env = os.environ.copy()
    env['VERIFY_EXE67'] = str(directory / '67_Odbudowany.exe')
    command = ("[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
               "@(Get-Process -Name 67_Odbudowany -ErrorAction SilentlyContinue | "
               "Where-Object { $_.Path -eq $env:VERIFY_EXE67 } | "
               "Select-Object Id,MainWindowTitle) | ConvertTo-Json -Compress")
    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', command],
                            capture_output=True, encoding='utf-8', check=True, env=env)
    data = json.loads(result.stdout) if result.stdout.strip() else []
    return data if isinstance(data, list) else [data]


def stop(directory):
    for process in processes(directory):
        subprocess.run(['taskkill', '/PID', str(process['Id']), '/T', '/F'], capture_output=True)


def wait_for_license(directory, seconds=45):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        items = processes(directory)
        if any(item['MainWindowTitle'] == '67 · licencja' for item in items):
            return
        assert not any('nasłuch gry' in item['MainWindowTitle'] for item in items), 'License gate bypassed'
        time.sleep(1)
    raise RuntimeError('License activation window was not reached')


releases = api(f'repos/{REPO}/releases?per_page=100')
release = next(item for item in releases if item['tag_name'] == 'v' + VERSION)
archive_asset = next(item for item in release['assets'] if item['name'] == '67-windows-x64.zip')
manifest_asset = next(item for item in release['assets'] if item['name'] == '67-update.json')
archive = download(archive_asset, ROOT / 'new.zip')
manifest = json.loads(download(manifest_asset, ROOT / 'manifest.json').read_text(encoding='utf-8'))
assert manifest['version'] == VERSION and manifest['mandatory'] is True
assert manifest['sha256'] == hashlib.sha256(archive.read_bytes()).hexdigest()
target = ROOT / 'target'
unpack(archive, target)
exe = target / '67_Odbudowany.exe'
selftest = ROOT / 'selftest.json'
assert subprocess.run([str(exe), '--self-test', str(selftest)], cwd=target,
                      env=APP_ENV, timeout=50).returncode == 0
diagnostic = json.loads(selftest.read_text(encoding='utf-8'))
assert diagnostic['ok'] is True and diagnostic['capture_started'] is False
REPORT['frozen_selftest'] = diagnostic
REPORT['archive_sha256'] = manifest['sha256']

if MODE == 'simple':
    simple_asset = next(item for item in release['assets'] if item['name'] == '67-windows-x64-prosty.zip')
    simple_archive = download(simple_asset, ROOT / 'simple.zip')
    simple_root = ROOT / 'simple'
    unpack(simple_archive, simple_root)
    with zipfile.ZipFile(archive) as original, zipfile.ZipFile(simple_archive) as wrapped:
        assert set(wrapped.namelist()) == {'67.exe'} | {'Program/App/' + name for name in original.namelist()}
        for name in original.namelist():
            assert original.read(name) == wrapped.read('Program/App/' + name)
    launcher = simple_root / '67.exe'
    launcher_sha256 = hashlib.sha256(launcher.read_bytes()).hexdigest()
    current_app = simple_root / 'Program' / 'App'
    try:
        subprocess.Popen([str(launcher)], cwd=ROOT, env=APP_ENV)
        wait_for_license(current_app)
        assert SETTINGS.read_bytes() == SETTINGS_BEFORE
    finally:
        stop(current_app)
    current_app.rename(ROOT / 'simple-current-reference')
    REPORT['simple_package'] = {'top_level': ['67.exe', 'Program'],
                                'app_files_unchanged': True, 'launcher_opens_license_gate': True,
                                'archive_sha256': hashlib.sha256(simple_archive.read_bytes()).hexdigest()}

if MODE in ('upgrade', 'simple'):
    assert release['draft'] is False and release['prerelease'] is False
    latest = api(f'repos/{REPO}/releases/latest')
    assert latest['tag_name'] == 'v' + VERSION
    baseline_id = os.environ['BASELINE_ASSET67']
    assert baseline_id.isdecimal()
    baseline_asset = api(f'repos/{REPO}/releases/assets/{baseline_id}')
    baseline = download(baseline_asset, ROOT / 'old.zip')
    install = simple_root / 'Program' / 'App' if MODE == 'simple' else ROOT / 'installed'
    unpack(baseline, install)
    before = json.loads((install / '67-install.json').read_text(encoding='utf-8'))
    assert before['version'] != VERSION
    try:
        start_exe = launcher if MODE == 'simple' else install / '67_Odbudowany.exe'
        subprocess.Popen([str(start_exe)], cwd=ROOT, env=APP_ENV)
        deadline = time.monotonic() + 150
        completed = None
        while time.monotonic() < deadline:
            statuses = list(install.parent.glob('.67-update-*/status.json'))
            for path in statuses:
                try:
                    value = json.loads(path.read_text(encoding='utf-8'))
                except (OSError, ValueError):
                    continue
                if value['state'] in ('failed', 'rolled_back', 'rollback_failed', 'installed_restart_failed'):
                    raise RuntimeError('Installer failure: ' + value['message'])
                if value['state'] == 'completed':
                    completed = value
            if completed:
                break
            time.sleep(1)
        assert completed, 'Automatic update did not complete'
        assert json.loads((install / '67-install.json').read_text(encoding='utf-8'))['version'] == VERSION
        assert SETTINGS.read_bytes() == SETTINGS_BEFORE
        with zipfile.ZipFile(archive) as bundle:
            for name in bundle.namelist():
                if not name.endswith('/'):
                    assert hashlib.sha256((install / name).read_bytes()).digest() == hashlib.sha256(bundle.read(name)).digest()
        wait_for_license(install)
        assert not (PROFILE / 'license.json').exists()
        REPORT['automatic_update'] = {'from': before['version'], 'to': VERSION,
                                      'installer': completed['state'], 'all_files_match': True,
                                      'settings_preserved': True, 'license_required': True}
        if MODE == 'simple':
            assert hashlib.sha256(launcher.read_bytes()).hexdigest() == launcher_sha256
            assert {item.name for item in simple_root.iterdir()} == {'67.exe', 'Program'}
            REPORT['simple_package']['layout_preserved_after_update'] = True
        stop(install)
        fake_key = '67-' + '0' * 64
        (PROFILE / 'license.json').write_text(json.dumps({'schema': 1, 'license_key': fake_key, 'valid': True}), encoding='utf-8')
        subprocess.Popen([str(start_exe)], cwd=ROOT, env=APP_ENV)
        wait_for_license(install)
        time.sleep(20)
        assert all(item['MainWindowTitle'] == '67 · licencja' for item in processes(install))
        request = Request('https://67-license-api.makskielb13.workers.dev/v1/license/validate',
                          data=json.dumps({'license_key': fake_key, 'device_hash': 'f' * 64,
                                           'app_version': VERSION}).encode(),
                          headers={'Content-Type': 'application/json', 'User-Agent': '67-license-client/' + VERSION})
        from urllib.error import HTTPError
        try:
            response = urlopen(request, timeout=20)
        except HTTPError as error:
            response = error
        with response:
            assert response.status == 403 and json.load(response) == {'valid': False, 'code': 'invalid'}
        REPORT['forged_local_activation_rejected'] = True
    finally:
        stop(install)

print('VERIFICATION_REPORT=' + json.dumps(REPORT, ensure_ascii=False))
summary = os.environ.get('GITHUB_STEP_SUMMARY')
if summary:
    with open(summary, 'a', encoding='utf-8') as output:
        output.write('```json\n' + json.dumps(REPORT, ensure_ascii=False, indent=2) + '\n```\n')
