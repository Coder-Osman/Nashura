"""Resume the fresh-data series only after its older coordinator reports fit completion."""

import argparse
import fcntl
import json
import os
from pathlib import Path
import shlex
import subprocess
import time


def process_argv(pid):
    result=subprocess.run(["ps","-p",str(pid),"-o","command="],capture_output=True,text=True)
    return shlex.split(result.stdout) if result.returncode==0 and result.stdout.strip() else []


def is_coordinator(argv, series_id):
    return (bool(argv) and not any(Path(token).name=="watch_tower_refresh.py" for token in argv)
        and any(Path(token).name=="run_tower_refresh.py" for token in argv)
        and "--series-id" in argv and argv[argv.index("--series-id")+1]==series_id)


def active_coordinators(series_id):
    result=subprocess.run(["ps","-axo","pid=,command="],capture_output=True,text=True,check=True)
    matches=[]
    for line in result.stdout.splitlines():
        pid,_,command=line.strip().partition(" ")
        try:
            argv=shlex.split(command)
            if is_coordinator(argv,series_id):
                matches.append((int(pid),argv))
        except (ValueError,IndexError):
            continue
    return matches


def should_resume(status):
    return status.get("status")=="complete" and status.get("stage")=="fitting_observed"


def should_stop(status):
    return status.get("status") in ("paused_failure","paused_user")


def coordinator_command(launch, restart_frozen_source=None):
    command=list(launch)
    if restart_frozen_source is None:
        return command
    restart_frozen_source=Path(restart_frozen_source)
    script_index=next((i for i,token in enumerate(command)
        if Path(token).name=="run_tower_refresh.py"),None)
    if script_index is None or "--frozen-source" not in command:
        raise ValueError("coordinator command is missing its script or frozen source")
    source_index=command.index("--frozen-source")+1
    command[script_index]=str(restart_frozen_source/"tools/run_tower_refresh.py")
    command[source_index]=str(restart_frozen_source)
    return command


def run(series_dir, initial_pid, series_id, poll_seconds, restart_frozen_source=None):
    launch=process_argv(initial_pid)
    if not is_coordinator(launch,series_id):
        found=active_coordinators(series_id)
        if len(found)!=1:
            raise RuntimeError("expected exactly one active coordinator for the series")
        initial_pid,launch=found[0]
    frozen_source=Path(launch[launch.index("--frozen-source")+1])
    probe=launch[launch.index("--probe")+1]
    restart_frozen_source=Path(restart_frozen_source) if restart_frozen_source else None
    if restart_frozen_source and not (restart_frozen_source/"tools/run_tower_refresh.py").is_file():
        raise FileNotFoundError(restart_frozen_source/"tools/run_tower_refresh.py")
    status_path=series_dir/"status.json"
    log_path=series_dir/"continuity.log"
    pid=initial_pid
    command=coordinator_command(launch,restart_frozen_source)
    with (series_dir/"continuity.lock").open("a+") as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        while True:
            if is_coordinator(process_argv(pid),series_id):
                time.sleep(poll_seconds)
                continue
            status=json.loads(status_path.read_text()) if status_path.exists() else {}
            if should_stop(status):
                with log_path.open("a") as log:
                    log.write(json.dumps({"event":"series_paused","status":status},sort_keys=True)+"\n")
                return
            if not should_resume(status):
                with log_path.open("a") as log:
                    log.write(json.dumps({"event":"coordinator_exit_without_fit_completion","status":status},sort_keys=True)+"\n")
                return
            time.sleep(2)
            found=active_coordinators(series_id)
            if found:
                if len(found)!=1:
                    raise RuntimeError("multiple coordinators appeared for the series")
                pid,active_command=found[0]
                command=coordinator_command(active_command,restart_frozen_source)
                continue
            if restart_frozen_source:
                frozen_source=restart_frozen_source
            environment=dict(os.environ,PYTHONPATH=str(frozen_source/"firstlight_CR"),CR_PROBE=probe)
            with log_path.open("ab",buffering=0) as output:
                process=subprocess.Popen(command,cwd=frozen_source,env=environment,
                    stdout=output,stderr=subprocess.STDOUT,start_new_session=True)
            pid=process.pid
            with log_path.open("a") as log:
                log.write(json.dumps({"event":"resumed_after_fit_completion","pid":pid,
                    "previous_batch":status.get("batch"),"next_batch":status.get("next_batch")},sort_keys=True)+"\n")


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--series-dir",type=Path,required=True)
    parser.add_argument("--series-id",required=True)
    parser.add_argument("--initial-pid",type=int,required=True)
    parser.add_argument("--poll-seconds",type=int,default=10)
    parser.add_argument("--restart-frozen-source",type=Path)
    args=parser.parse_args()
    run(args.series_dir,args.initial_pid,args.series_id,args.poll_seconds,args.restart_frozen_source)


if __name__=="__main__":
    main()
