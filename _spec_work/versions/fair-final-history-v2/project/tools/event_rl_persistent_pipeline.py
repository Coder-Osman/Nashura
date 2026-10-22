"""Resume the existing budget, then run its existing frozen evaluation plan."""
from pathlib import Path
import argparse
import json
import os
import subprocess
import time
import traceback


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-root', required=True, type=Path)
    args = parser.parse_args()
    root = args.run_root.resolve()
    cfg = json.loads((root/'fleet.json').read_text())
    target = cfg['start_iteration'] + cfg['additional_iterations']

    def state(event, **kwargs):
        temporary = root/'pipeline-status.tmp'
        temporary.write_text(json.dumps(dict(event=event, time=time.time(), **kwargs)))
        temporary.replace(root/'pipeline-status.json')

    try:
        assert json.loads((root/'learner-persistent-audit/validation.json').read_text())['passed']
        assert cfg['learner_mode'] == 'persistent'
        boundary = json.loads((root/'learner-persistent-audit/takeover.json').read_text())
        (root/'pipeline.pid').write_text(str(os.getpid()))
        state('training', start_iteration=cfg['start_iteration'], target_iteration=target,
              additional_iterations=cfg['additional_iterations'], learner='persistent.v1',
              resumed_after_iteration=boundary['completed_iteration'])
        with (root/'training-learner-persistent.log').open('x') as log:
            subprocess.run(['python3', str(root/'continue-persistent-learner.py'),
                            '--run-root', str(root), '--resume'], cwd=root,
                           stdout=log, stderr=subprocess.STDOUT, check=True)
        training = json.loads((root/'status.json').read_text())
        assert training['event'] == 'completed' and training['iterations'] == target
        assert training['additional_iterations'] == cfg['additional_iterations']
        state('evaluating', checkpoint=training['checkpoint'], matchups=cfg['evaluation']['matchups'])
        with (root/'evaluation.log').open('x') as log:
            subprocess.run(['python3', str(root/'evaluate.py'), str(root)], cwd=root,
                           stdout=log, stderr=subprocess.STDOUT, check=True)
        result = json.loads((root/'post-training-eval/status.json').read_text())
        assert result['event'] == 'completed'
        state('completed', checkpoint=training['checkpoint'], evaluation=result)
    except BaseException:
        state('failed', error=traceback.format_exc())
        raise


if __name__ == '__main__':
    main()
