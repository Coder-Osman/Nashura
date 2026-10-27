"""Run a private, identity-bound E2 attempt on an explicitly owned AVD."""

import argparse
import hashlib
import json
from pathlib import Path
import socket
import subprocess
import sys
import time
import traceback


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def traced_probe(operation, oracle, collect, save):
    if oracle is None:
        return operation()
    oracle.start()
    try:
        return operation()
    finally:
        original_failure = sys.exc_info()[1]
        try:
            oracle.stop()
            collect()
        except BaseException as failure:
            save('java-trace-cleanup-failure.json', dict(error=str(failure),
                 original_error=str(original_failure) if original_failure else None))
            if original_failure is None:
                raise


def run(config_path, output):
    import frida

    config = json.loads(config_path.read_text())
    output.mkdir(parents=True, exist_ok=False)
    scripts = {}
    receipts = []
    messages = []
    session = None
    lifecycle_completed = False
    return_observer_installed = False
    pn_bridge_installed = False
    started = time.time()
    repository = Path(__file__).resolve().parent

    def save(name, value):
        (output / name).write_text(json.dumps(value, indent=2) + '\n')

    def command(*arguments):
        result = subprocess.run(config['adb'] + list(arguments), capture_output=True, text=True, timeout=180)
        with (output / 'commands.jsonl').open('a') as target:
            target.write(json.dumps(dict(argv=config['adb'] + list(arguments), code=result.returncode,
                                         stdout=result.stdout, stderr=result.stderr)) + '\n')
        result.check_returncode()
        return result.stdout.strip()

    def identity():
        files = command('shell', 'sha256sum', *config['guest_identity'].keys())
        actual = {line.split(maxsplit=1)[1].strip(): line.split()[0] for line in files.splitlines()}
        if actual != config['guest_identity']:
            raise RuntimeError('guest identity mismatch')
        updates = command('shell', config['update_identity_command'])
        if hashlib.sha256(updates.encode()).hexdigest() != config['update_identity_sha256']:
            raise RuntimeError('update identity mismatch')
        return dict(files=actual, update_manifest_sha256=hashlib.sha256(updates.encode()).hexdigest())

    def thread_inventory(pid, phase):
        raw = command('shell', 'for task in /proc/' + str(pid) + '/task/*; do '
                      'printf "%s\t" "${task##*/}"; cat "$task/comm" 2>/dev/null || true; done')
        rows = []
        for line in raw.splitlines():
            parts = line.split('\t', 1)
            if len(parts) == 2 and parts[0].isdigit():
                rows.append(dict(tid=int(parts[0]), comm=parts[1]))
        save('threads-' + phase + '.json', dict(phase=phase, threads=rows,
                                              scope='proc task comm snapshot; exited or renamed threads remain unknown'))

    def control(text, phase):
        for name in ('e0', 'e2', 'fmod', 'replacement', 'return-observer'):
            if name in scripts:
                scripts[name].exports_sync.phase(phase)
        beginning = time.monotonic()
        with socket.create_connection(('127.0.0.1', config['control_port']), timeout=300) as connection:
            connection.sendall((text + '\n').encode())
            response = json.loads(connection.makefile('rb').readline())
        receipts.append(dict(command=text, phase=phase, response=response, seconds=time.monotonic() - beginning))
        save('control-receipts.json', receipts)
        if not response.get('ok'):
            raise RuntimeError('native command rejected: ' + text + ': ' + str(response))
        return response

    def create(name, filename, settings):
        source = (repository / filename).read_text()
        if name == 'return-observer' and config.get('pn_bridge'):
            source = (repository / 'e2_1_pn_bridge.js').read_text() + '\n' + source
        script = session.create_script(source)
        def message(payload, data):
            messages.append(dict(script=name, payload=payload))
            with (output / 'messages.jsonl').open('a') as target:
                target.write(json.dumps(messages[-1]) + '\n')
        script.on('message', message)
        script.load()
        scripts[name] = script
        configured = script.exports_sync.configure(settings)
        print('configured ' + name, flush=True)
        return configured

    save('run-start.json', dict(started=started, config_sha256=digest(config_path),
                                mode=config.get('mode', 'original'), runner_sha256=digest(__file__)))
    save('private-config.json', config)
    save('source-identity.json', {path.name: digest(path) for path in repository.glob('e[012]_*.js')})
    source_directory = output / 'sources'
    source_directory.mkdir()
    for path in list(repository.glob('e[012]_*.js')) + [Path(__file__), repository / 'e2_config_store.c']:
        (source_directory / path.name).write_bytes(path.read_bytes())
    if config.get('return_observer'):
        path = repository / 'E21PreferenceObserver.java'
        (source_directory / path.name).write_bytes(path.read_bytes())
    if config.get('pn_candidate') or config.get('pn_matrix'):
        for filename in ('e2_1_pn_store.c', 'e2_1_pn_store.h'):
            path = repository / filename
            (source_directory / path.name).write_bytes(path.read_bytes())
    if config.get('pn_codec'):
        for filename in ('e2_1_pn_codec.c', 'e2_1_pn_codec.h', 'e2_1_pn_codec.py', 'e2_1_pn_codec_matrix.py'):
            path = repository / filename
            (source_directory / path.name).write_bytes(path.read_bytes())
    if config.get('pn_bridge'):
        install_at = config.get('return_observer', {}).get('install_at')
        if config.get('replacement') or install_at not in {'post-cold-ready', 'first-native-bridge'}:
            raise ValueError('PN bridge experiment requires independent explicit installation')
        if config['pn_bridge'].get('install_at', 'post-cold-ready') != install_at:
            raise ValueError('PN bridge and backend installation phases differ')
        for filename in ('e2_1_pn_bridge.c', 'e2_1_pn_bridge.h'):
            path = repository / filename
            (source_directory / path.name).write_bytes(path.read_bytes())
    try:
        if command('emu', 'avd', 'name').splitlines()[0] != config['avd']:
            raise RuntimeError('AVD ownership mismatch')
        if command('shell', 'pidof ' + config['package'] + ':engine0 || true'):
            raise RuntimeError('existing engine must be separately reviewed before this attempt')
        save('identity-before.json', identity())
        for path, expected in config['local_identity'].items():
            if digest(path) != expected:
                raise RuntimeError('local identity mismatch: ' + path)
        device = frida.get_device_manager().add_remote_device('127.0.0.1:' + str(config['frida_port']))
        pid = device.spawn(config['package'], activity=config['activity'])
        save('owned-process.json', dict(pid=pid, avd=config['avd'],
                                        stat=command('shell', 'cat', '/proc/' + str(pid) + '/stat')))
        session = device.attach(pid)
        observer = json.loads(Path(config['observer_config']).read_text())
        observer.update(capture_backtraces=False, enumerate_threads=False)
        if config.get('replacement') or config.get('pn_bridge'):
            observer['functions'] = [entry for entry in observer['functions']
                                     if entry['name'] not in {'PreferenceNativeReadBridge', 'PreferenceNativeWriteBridge'}]
        create('e0', 'e0_dependency_observer.js', observer)
        if config.get('enable_e2_observer', True):
            create('e2', 'e2_jni_config_observer.js', observer)
        resource = dict(config['resource_config'])
        resource['audit_path'] = config['audit_prefix'] + output.name + '.tsv'
        create('resource', 'e1_resource_redirect.js', resource)
        create('fmod', 'e1_fmod_binding.js', config['fmod_config'])
        if config.get('replacement'):
            create('replacement', 'e2_config_replace.js', config['replacement'])
        early_observer = config.get('return_observer', {}).get('install_at') == 'first-native-bridge'
        if early_observer:
            settings = dict(config['return_observer'])
            if config.get('pn_bridge'):
                settings['pn_bridge'] = config['pn_bridge']
            create('return-observer', 'e2_1_return_observer.js', settings)
        device.resume(pid)
        print('resumed ' + str(pid), flush=True)
        for attempt in range(90):
            try:
                status = control('status', 'cold_ready_wait')
                if status.get('coldReady'):
                    break
            except (OSError, ValueError):
                pass
            time.sleep(1)
        else:
            raise RuntimeError('cold ready not reached')
        save('cold-ready.json', status)
        thread_inventory(pid, 'cold-ready')
        if config.get('java_oracle'):
            create('java-oracle', 'e2_1_java_oracle.js', config['java_oracle'])
        if config.get('semantics_probe'):
            create('semantics', 'e2_1_config_probe.js', config['semantics_probe'])
            save('semantics-cold.json', scripts['semantics'].exports_sync.batch([
                dict(case='after_cold_ready', operation='read', key='PN_POPUP')]))
        if early_observer:
            observed = scripts['return-observer'].exports_sync.snapshot()
            save('return-observer-cold.json', observed)
            return_observer_installed = observed['cold']['installed']
            if not return_observer_installed or observed['cold']['errors']:
                raise RuntimeError('cold observer installation failed; original engine outcome retained separately')
        elif config.get('return_observer'):
            create('return-observer', 'e2_1_return_observer.js', config['return_observer'])
            installed = scripts['return-observer'].exports_sync.command('install')
            save('return-observer-install.json', dict(result=installed, phase='after_cold_ready_before_state_create'))
            return_observer_installed = True
        if config.get('pn_bridge'):
            bridge = scripts['return-observer'].exports_sync
            save('pn-bridge-install.json', bridge.bridge_snapshot() if early_observer
                 else bridge.bridge_install(config['pn_bridge']))
            pn_bridge_installed = True
        match = json.loads(Path(config['match']).read_text())
        control('configure ' + json.dumps(match, separators=(',', ':')), 'state_create_load')
        control('observe', 'initial_state')
        initial = control('snapshot-create', 'snapshot_create')
        control('step 21', 'snapshot_branch')
        first = control('snapshot-create', 'snapshot_create')
        control('restore ' + str(initial['handle']), 'restore')
        control('step 21', 'snapshot_branch_replay')
        second = control('snapshot-create', 'snapshot_create')
        if (first['digest'], first['bytes']) != (second['digest'], second['bytes']):
            raise RuntimeError('snapshot branch replay mismatch')
        for snapshot in (initial, first, second):
            control('release-snapshot ' + str(snapshot['handle']), 'snapshot_release')
        control('reset', 'reset')
        control('observe', 'after_reset')
        if 'semantics' in scripts:
            save('semantics-reset.json', scripts['semantics'].exports_sync.batch([
                dict(case='after_reset', operation='read', key='PN_POPUP')]))
        thread_inventory(pid, 'before-trace')
        result = control('standalone-trace 10000 ' + config['actions_name'], 'full_trace')
        save('trace.json', result)
        if not result['finalized'] or result['injected'] != 28 or result['queue_count'] != 0:
            raise RuntimeError('incomplete native trace')
        command('pull', result['snapshot_directory'], str(output / 'snapshots'))
        control('observe', 'terminal')
        control('reset', 'terminal_reset_destroy')
        control('observe', 'final_reset_state')
        thread_inventory(pid, 'after-trace')
        if 'semantics' in scripts:
            save('semantics-terminal-reset.json', scripts['semantics'].exports_sync.batch([
                dict(case='after_terminal_reset', operation='read', key='PN_POPUP')]))
            if config.get('pn_bridge_matrix'):
                from e2_1_pn_bridge_matrix import run_bridge_matrix

                path = repository / 'e2_1_pn_bridge_matrix.py'
                (source_directory / path.name).write_bytes(path.read_bytes())
                for name in ('e0', 'e2', 'fmod', 'return-observer'):
                    if name in scripts:
                        scripts[name].exports_sync.phase('explicit_post_trajectory_pn_matrix')
                oracle = scripts['java-oracle'].exports_sync if 'java-oracle' in scripts else None

                def capture_bridge_xml(label, files):
                    directory = output / ('bridge-xml-' + label)
                    directory.mkdir(exist_ok=False)
                    records = []
                    for index, filename in enumerate(files):
                        local = directory / ('store-' + str(index) + '.xml')
                        command('pull', filename, str(local))
                        records.append(dict(sha256=digest(local), bytes_hex=local.read_bytes().hex()))
                    return records

                traced_probe(
                    lambda: run_bridge_matrix(scripts['return-observer'].exports_sync,
                                             scripts['semantics'].exports_sync, bool(config.get('pn_bridge')),
                                             save, capture_bridge_xml),
                    oracle,
                    lambda: command('pull', config['java_oracle']['trace_path'], str(output / 'java-methods.trace')),
                    save)
            if config.get('pn_codec'):
                from e2_1_pn_codec_matrix import run_codec_matrix

                def capture_xml(label, files):
                    directory = output / ('codec-xml-' + label)
                    directory.mkdir(exist_ok=False)
                    records = []
                    for index, filename in enumerate(files):
                        local = directory / ('store-' + str(index) + '.xml')
                        command('pull', filename, str(local))
                        records.append(dict(sha256=digest(local), bytes_hex=local.read_bytes().hex()))
                    return records

                save('pn-codec-configure.json', create('pn-codec', 'e2_1_pn_codec_probe.js', config['pn_codec']))
                run_codec_matrix(scripts['return-observer'].exports_sync, scripts['pn-codec'].exports_sync,
                                 config['pn_codec'], save, capture_xml)
            if config.get('pn_matrix'):
                from e2_1_pn_matrix import run_matrix

                path = repository / 'e2_1_pn_matrix.py'
                (source_directory / path.name).write_bytes(path.read_bytes())
                create('pn-matrix-candidate', 'e2_1_pn_matrix_candidate.js', config['pn_matrix'])
                run_matrix(scripts['return-observer'].exports_sync, scripts['semantics'].exports_sync,
                           scripts['pn-matrix-candidate'].exports_sync, save)
            if config.get('semantics_cases'):
                oracle = scripts['java-oracle'].exports_sync if 'java-oracle' in scripts else None
                traced_probe(
                    lambda: save('semantics-cases.json', scripts['semantics'].exports_sync.batch(config['semantics_cases'])),
                    oracle,
                    lambda: command('pull', config['java_oracle']['trace_path'], str(output / 'java-methods.trace')),
                    save)
                if config.get('pn_candidate'):
                    create('pn-candidate', 'e2_1_pn_candidate.js', config['pn_candidate'])
                    original = json.loads((output / 'semantics-cases.json').read_text())
                    save('pn-candidate-replay.json', scripts['pn-candidate'].exports_sync.replay(original))
        if config.get('string_lifecycle_probe'):
            create('string-lifecycle', 'e2_1_string_lifecycle.js', config['string_lifecycle_probe'])
            save('string-lifecycle.json', scripts['string-lifecycle'].exports_sync.run())
        (output / 'threads-final.txt').write_text(command('shell', 'ps', '-T', '-p', str(pid), '-o', 'TID,NAME,WCHAN'))
        save('identity-after.json', identity())
        command('pull', resource['audit_path'], str(output / 'provider-audit.tsv'))
        if any(message['payload'].get('type') == 'error' for message in messages):
            raise RuntimeError('Frida script error; inspect retained messages')
        lifecycle_completed = True
        print('native lifecycle completed', flush=True)
    except BaseException as failure:
        save('run-finished.json', dict(status='failed', exit_code=1, elapsed=time.time() - started,
                                      error=str(failure), traceback=traceback.format_exc()))
        raise
    finally:
        snapshot_errors = []
        if config.get('pn_bridge') and config.get('return_observer', {}).get('install_at') == 'first-native-bridge' and 'return-observer' in scripts:
            try:
                pn_bridge_installed = scripts['return-observer'].exports_sync.bridge_snapshot()['installed']
            except BaseException as failure:
                save('pn-bridge-cold-status-failure.json', dict(error=str(failure)))
                snapshot_errors.append('pn-bridge-cold-status')
        if pn_bridge_installed:
            try:
                observer = scripts['return-observer'].exports_sync
                observer.bridge_retire()
                final = observer.bridge_snapshot()
                save('pn-bridge-final.json', final)
                if final['errors'] or not final['retired']:
                    snapshot_errors.append('pn-bridge')
            except BaseException as failure:
                save('pn-bridge-cleanup-failure.json', dict(error=str(failure)))
                snapshot_errors.append('pn-bridge')
        if 'return-observer' in scripts and config.get('return_observer', {}).get('install_at') == 'first-native-bridge':
            try:
                return_observer_installed = scripts['return-observer'].exports_sync.snapshot()['cold']['installed']
            except BaseException as failure:
                save('return-observer-cold-status-failure.json', dict(error=str(failure)))
                snapshot_errors.append('return-observer-cold-status')
        if return_observer_installed:
            try:
                observer = scripts['return-observer'].exports_sync
                (output / 'return-observer-events.tsv').write_text(observer.command('snapshot'))
                save('return-observer-uninstall.json', dict(result=observer.command('uninstall')))
                return_observer_installed = False
                if config['return_observer'].get('release_after_uninstall'):
                    save('return-observer-release.json', dict(result=observer.command('release')))
            except BaseException as failure:
                save('return-observer-cleanup-failure.json', dict(error=str(failure)))
                snapshot_errors.append('return-observer-cleanup')
        for name, script in scripts.items():
            try:
                final = script.exports_sync.snapshot()
                save(name + '-final.json', final)
                if name == 'return-observer' and config.get('return_observer', {}).get('install_at') == 'first-native-bridge':
                    cold = final['cold']
                    if not cold['installed'] or cold['errors'] or any('after' not in window for window in cold['windows']):
                        snapshot_errors.append('return-observer-cold-windows')
            except BaseException as failure:
                save(name + '-snapshot-failure.json', dict(error=str(failure)))
                snapshot_errors.append(name)
        if session:
            session.detach()
        if snapshot_errors:
            save('evidence-incomplete.json', dict(missing_final_snapshots=snapshot_errors))
            save('run-finished.json', dict(status='failed', exit_code=1, elapsed=time.time() - started,
                                          error='final evidence snapshots missing', snapshots=snapshot_errors))
            raise RuntimeError('final evidence snapshots missing: ' + ', '.join(snapshot_errors))
        if lifecycle_completed:
            save('run-finished.json', dict(status='completed', exit_code=0, elapsed=time.time() - started))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    run(args.config, args.output)
