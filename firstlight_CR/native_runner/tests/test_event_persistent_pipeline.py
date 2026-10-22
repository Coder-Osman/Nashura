"""The handoff must retain the total budget and delay evaluation until completion."""
import json
import sys

import pytest

from tools import event_rl_persistent_pipeline as pipeline


@pytest.mark.parametrize('last_iteration', [551, 304])
def test_resumed_pipeline_preserves_target_and_evaluation_gate(tmp_path, monkeypatch, last_iteration):
    root = tmp_path
    audit = root/'learner-persistent-audit'
    audit.mkdir()
    (audit/'validation.json').write_text(json.dumps({'passed':True}))
    (audit/'takeover.json').write_text(json.dumps({'completed_iteration':303}))
    (root/'fleet.json').write_text(json.dumps(dict(
        start_iteration=251, additional_iterations=300, learner_mode='persistent',
        evaluation=dict(matchups=[[551,251],[551,351],[551,451],[551,501]]))))
    calls=[]
    def run(argv, **kwargs):
        calls.append(argv)
        if len(calls)==1:
            assert argv[-1]=='--resume'
            status=dict(event='completed', iterations=last_iteration,
                        additional_iterations=last_iteration-251, checkpoint='final.pt')
            (root/'status.json').write_text(json.dumps(status))
        else:
            assert argv==['python3',str(root/'evaluate.py'),str(root)]
            (root/'post-training-eval').mkdir()
            (root/'post-training-eval/status.json').write_text(json.dumps({'event':'completed'}))
    monkeypatch.setattr(pipeline.subprocess,'run',run)
    monkeypatch.setattr(sys,'argv',['pipeline','--run-root',str(root)])
    if last_iteration==551:
        pipeline.main()
        assert len(calls)==2
        assert json.loads((root/'pipeline-status.json').read_text())['event']=='completed'
    else:
        with pytest.raises(AssertionError):
            pipeline.main()
        assert len(calls)==1
        assert json.loads((root/'pipeline-status.json').read_text())['event']=='failed'
