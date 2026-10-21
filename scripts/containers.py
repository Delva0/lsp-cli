"""Install a wheel into Docker containers or rebuild named images with that wheel.

Rebuilds preserve each original image under a backup tag before advancing its
existing tag. The output manifest records original/new IDs and installed source
hashes. Run this script from the lsp-cli checkout through uv run --frozen.
"""

import argparse
import hashlib
import json
import subprocess
import zipfile
from datetime import UTC, datetime
from pathlib import Path


def run(*argv, **kwargs):
    return subprocess.check_output(argv, text=True, **kwargs).strip()


def wheel_identity(wheel):
    with zipfile.ZipFile(wheel) as archive:
        metadata, = (name for name in archive.namelist() if name.endswith('.dist-info/METADATA'))
        version, = (line.removeprefix('Version: ') for line in archive.read(metadata).decode().splitlines()
                    if line.startswith('Version: '))
        files = {name.removeprefix('lsp_cli/'): hashlib.sha256(archive.read(name)).hexdigest()
                 for name in archive.namelist() if name.startswith('lsp_cli/') and name.endswith('.py')}
    return {'version': version, 'files': files}


def install(container, wheel, expected, *, offline=False):
    prefix = ['docker', 'exec', container]
    remote = run(*prefix, 'mktemp', '-d', '/tmp/lsp-install-XXXXXXXX')
    target = remote + '/' + wheel.name
    try:
        run('docker', 'cp', str(wheel), container + ':' + target)
        run(*prefix, 'python3', '-m', 'venv', '/opt/lsp-cli')
        pip = [*prefix, '/opt/lsp-cli/bin/pip', '--disable-pip-version-check']
        run(*pip, 'uninstall', '-y', 'lsp-cli')
        run(*pip, 'install', *(['--no-index'] if offline else []), target)
        run(*prefix, 'ln', '-sfn', '/opt/lsp-cli/bin/lsp', '/usr/local/bin/lsp')
        actual = json.loads(run(*prefix, '/opt/lsp-cli/bin/python', '-I', '-c',
                                'import json; from lsp_cli.identity import source_identity; '
                                'print(json.dumps(source_identity()))'))
        if actual != expected:
            raise ValueError('Installed sources differ from the selected wheel')
        version = run(*prefix, 'lsp', '--version')
        if version != 'lsp ' + expected['version']:
            raise ValueError('The container PATH selects another lsp installation: ' + version)
        shown = json.loads(run('docker', 'exec', '-i', container, '/opt/lsp-cli/bin/python', '-I', '-c',
                               'from lsp_cli.commands import main; '
                               'raise SystemExit(main(["show", "sample", "--json"]))',
                               input='def sample(): return 42\n'))
        if not any('return 42' in row['code'] for row in shown['results']):
            raise ValueError('Installed CLI source smoke test failed')
        return actual
    finally:
        run(*prefix, 'rm', '-rf', remote)


def rebuild(image, wheel, expected, *, offline=False):
    before = run('docker', 'image', 'inspect', '--format={{.Id}}', image)
    suffix = before.removeprefix('sha256:')[:12]
    backup = image + '-before-lsp-' + suffix
    run('docker', 'tag', before, backup)
    container = run('docker', 'run', '-d', '--init', '--network', 'none' if offline else 'bridge',
                    '--entrypoint', 'sleep', '--label', 'lsp-cli.operation=rebuild', before, 'infinity')
    try:
        installed = install(container, wheel, expected, offline=offline)
        # Preserve the original startup command after the temporary sleep used
        # to keep the build container alive.
        config = json.loads(run('docker', 'image', 'inspect', before))[0]['Config']
        labels = {**(config.get('Labels') or {}), 'org.lsp-cli.version': expected['version'],
                  'org.lsp-cli.wheel-sha256': hashlib.sha256(wheel.read_bytes()).hexdigest()}
        changes = ['--change', 'ENTRYPOINT ' + json.dumps(config.get('Entrypoint') or []),
                   '--change', 'CMD ' + json.dumps(config.get('Cmd') or [])]
        for key, value in labels.items():
            changes.extend(['--change', 'LABEL ' + key + '=' + json.dumps(value)])
        after = run('docker', 'commit', *changes, container, image)
        return {'image': image, 'before': before, 'backup': backup, 'after': after,
                'implementation': installed, 'status': 'rebuilt'}
    finally:
        run('docker', 'rm', '-f', container)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wheel', required=True, type=Path)
    parser.add_argument('--offline', action='store_true', help='Require all dependencies to be preinstalled')
    modes = parser.add_subparsers(dest='mode', required=True)
    modes.add_parser('install').add_argument('container')
    images = modes.add_parser('rebuild')
    images.add_argument('images', nargs='+')
    images.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    wheel = args.wheel.resolve(strict=True)
    expected = wheel_identity(wheel)
    if args.mode == 'install':
        install(args.container, wheel, expected, offline=args.offline)
        print('Installed lsp ' + expected['version'] + ' in ' + args.container)
        return
    report = {'created_at': datetime.now(UTC).isoformat(), 'wheel': str(wheel),
              'wheel_sha256': hashlib.sha256(wheel.read_bytes()).hexdigest(), 'images': []}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    for image in args.images:
        try:
            result = rebuild(image, wheel, expected, offline=args.offline)
        except Exception as error:
            report['images'].append({'image': image, 'status': 'failed', 'error': str(error)})
            raise
        else:
            report['images'].append(result)
            print(image + ' -> ' + result['after'], flush=True)
        finally:
            args.output.write_text(json.dumps(report, indent=2) + '\n')


if __name__ == '__main__':
    main()
