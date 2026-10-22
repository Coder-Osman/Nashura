import prototype_batch as batch


def test_job_plan_matches_fleet_and_is_unique():
    config = {"fleet": {"34": 24, "35": 24, "37": 24, "39": 18, "41": 18}}
    planned = batch.jobs(config)
    assert len(planned) == 108
    assert len({job["episode_id"] for job in planned}) == 108
    assert len({(job["host"], job["port"]) for job in planned}) == 108
    assert planned[0]["host"] == "37" and planned[0]["episode_id"] == "prototype-0001"
    assert planned[-1]["host"] == "41" and planned[-1]["episode_id"] == "prototype-0108"


def test_collect_requires_complete_terminal_32_simulation_results(monkeypatch):
    config = {"fleet": {"34": 24, "35": 24, "37": 24, "39": 18, "41": 18}}
    monkeypatch.setattr(batch, "load_config", lambda: config)
    monkeypatch.setattr(batch, "inventory", lambda path: {host: ("user", "password") for host in config["fleet"]})

    def fake_status(host, host_jobs, credentials):
        job = host_jobs[0]
        return [{**job, "state": "passed", "verified": True, "passed": True,
                 "simulations": 32, "ended": True, "truncated": False}]

    monkeypatch.setattr(batch, "_status_host", fake_status)
    summary = batch.collect(None)
    assert summary["passed"] is False
    assert summary["games"] == 0
    assert summary["state_counts"]["unreachable"] == 108
    assert summary["required_games"] == 108


def test_status_is_recoverable_for_in_progress_and_finished_records(monkeypatch):
    config = {"fleet": {"34": 24, "35": 24, "37": 24, "39": 18, "41": 18}}
    monkeypatch.setattr(batch, "load_config", lambda: config)
    monkeypatch.setattr(batch, "inventory", lambda path: {host: ("user", "password") for host in config["fleet"]})

    def fake_status(host, host_jobs, credentials):
        states = []
        for job in host_jobs:
            if host == "37" and job["episode"] == 1:
                states.append({**job, "state": "running", "verified": False,
                               "simulations": 32, "ended": False, "truncated": None})
            else:
                states.append({**job, "state": "passed", "verified": True,
                               "simulations": 32, "ended": True, "truncated": False})
        return states

    monkeypatch.setattr(batch, "_status_host", fake_status)
    summary = batch.collect(None)
    assert summary["games"] == 107
    assert summary["failures"][0]["host"] == "37"
