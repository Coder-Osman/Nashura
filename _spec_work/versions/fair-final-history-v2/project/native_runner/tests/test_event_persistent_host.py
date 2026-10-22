import json
from pathlib import Path
import subprocess
import sys

from tools.event_rl_host import Host, stop_process


def test_worker_is_reused_and_retries_keep_task_seed(tmp_path, monkeypatch):
    (tmp_path / "manifest.json").write_text("{}")
    host = Host(dict(run=str(tmp_path), code=str(tmp_path), manifest=str(tmp_path / "manifest.json"), ports=[1]))
    script = '''import json,sys,os
from pathlib import Path
for line in sys.stdin:
 t=json.loads(line)
 if t.get("fail_once") and "attempt-0" in t["out"]:
  print(json.dumps({"success":False,"error":"injected failure"}),flush=True)
  break
 print(json.dumps({"success":True,"port":t["port"],"seed":t["seed"],"worker_pid":os.getpid(),"source_checkpoint_sha256":t["weights"]["source_checkpoint_sha256"]}),flush=True)
'''
    def start(port):
        p = subprocess.Popen([sys.executable, "-u", "-c", script], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1)
        host.processes[port] = p
        return p
    monkeypatch.setattr(host, "start", start)
    task = dict(port=1, seed=123, iteration_dir=str(tmp_path), weights={"source_checkpoint_sha256":"a"*64})
    try:
        first = host.worker(task)
        second = host.worker({**task, "seed":456})
        assert first["worker_pid"] == second["worker_pid"]
        retry = host.worker({**task, "fail_once":True})
        assert retry["success"] and retry["seed"] == retry["attempts"][0]["seed"] == 123
        assert retry["worker_pid"] != first["worker_pid"]
        assert len(retry["attempts"]) == 1
    finally:
        host.close()
    assert not host.processes


def test_closed_window_updates_tracker_without_model_inference(monkeypatch):
    from types import SimpleNamespace
    import torch
    from tools import event_rl_rollout as rollout
    from native_runner.training.event_config import EventPolicyConfigV1

    observations = lambda tick: {o: SimpleNamespace(tick=tick, players=[
        SimpleNamespace(owner=i, elixir_exact=10.) for i in (0, 1)]) for o in (0, 1)}
    class Environment:
        def action_outcomes(self):
            return ()

        episode_config = None
        steps = 0
        def reset(self, **kwargs):
            return observations(300), {}
        def step(self, *args, **kwargs):
            self.steps += 1
            return observations(300+self.steps), {}, {"__all__": self.steps==2}, {"__all__": False}, {}
        def deployment_timing_events(self, owner):
            return [{"nativeEventId": "1", "kind": "card_play"}] if self.steps==1 else []
    seen = []
    def tensorizer(*args, actor_owner):
        return SimpleNamespace(start_episode=lambda *a, **k: None,
                               tensorize=lambda obs, **k: seen.append((actor_owner, obs.tick)))
    monkeypatch.setattr(rollout, "build_episode_tensorizer_v4", tensorizer)
    monkeypatch.setattr(rollout, "canonicalize_event_observation", lambda x: x)
    monkeypatch.setattr(rollout, "legal_event_actions", lambda *a, **k: SimpleNamespace(
        decision_ready=False, act_allowed=False, delay_masks=torch.zeros(8,61,dtype=torch.bool)))
    coordinator = SimpleNamespace(**{name: lambda *a, **k: None for name in
        ("start_session", "begin_round", "finish_round", "close_session")}, profile=lambda: {})
    # No encode/initial_state methods: reaching inference would fail this test.
    model = SimpleNamespace(card_count=8, cell_count=576)
    _, counters = rollout.play_match(environment=Environment(), coordinator=coordinator, model=model,
        event_config=EventPolicyConfigV1(), catalog=None, device=torch.device("cpu"),
        decks={0:tuple(range(1,9)),1:tuple(range(1,9))}, match_id="closed", policy_version="test",
        seed=1, env_id=0, temperatures=(1.,1.,1.,1.), max_ticks=7200, max_seconds=10,
        max_advance_ticks=30, max_wait_ticks=300)
    assert seen == [(0,301),(1,301)]
    assert counters["terminal"] and counters["not_ready"] == 2
    assert counters["inference_calls"] == counters["decisions"] == 0
