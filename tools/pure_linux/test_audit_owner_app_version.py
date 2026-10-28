import copy

import pytest

import audit_owner_app_version as audit


def test_bridge_audit_rejects_changed_version_and_incomplete_runs(monkeypatch):
    monkeypatch.setattr(audit, "owner_compare", lambda data: None)
    android = {"trace": {"coverage": [{"label": "app_version_wrapper_candidate", "installed": True}],
               "events": [{"kind": "app_version_wrapper", "value": "15.535.13", "owner_constructor_id": 7,
                           "thread": 5, "sequence": 1},
                          {"kind": "engine_db_init_enter", "owner_constructor": {"id": 7}, "thread": 5, "sequence": 2}]}}
    common = {"status": "BLOCKED", "timed_out": False, "inputs_before": {"libg.so": audit.ENGINE},
              "inputs_unchanged": True, "resources_unchanged": True, "auxiliary_unchanged": True}
    before = dict(common, exit_code=1)
    after = dict(common, exit_code=0, app_version_bridge_candidate=True)
    before_obs = {"error": None, "final_stop": "SIGSEGV", "events": [{"name": "jni_method_candidate",
                  "strings": {"x0": {"bytes_hex": "67657441707056657273696f6e"}}}]}
    after_obs = {"exit_code": 0, "error": None, "events": [{"name": "owner_constructor", "return_x0": 1}],
                 "targets": [{"name": name, "armed": True, "calls": int(name == "owner_constructor")}
                             for name in ("owner_constructor", "jni_method_candidate", "jni_environment_candidate")]}
    text = "\n".join(["APP_VERSION_BRIDGE_INSTALLED\tverified_package_version_candidate",
                       "APP_VERSION_BRIDGE_RETURN\tverified_package_version_candidate", "PHASE\towner_getter_return",
                       "OWNER_GETTER_RESULT\t1\t1\t1", "OWNER_VTABLE_MATCH\t1"])
    assert audit.compare(android, before, before_obs, after, after_obs, text)["linux_version_bytes_checked"]
    for damage in ("version", "thread", "jni", "exit", "return", "resources"):
        a, b, bo, c, co = map(copy.deepcopy, (android, before, before_obs, after, after_obs))
        output = text
        if damage == "version": a["trace"]["events"][0]["value"] = "unknown"
        elif damage == "thread": a["trace"]["events"][0]["thread"] = 6
        elif damage == "jni": bo["events"][0]["strings"]["x0"]["bytes_hex"] = "00"
        elif damage == "exit": co["exit_code"] = 1
        elif damage == "return": output = output.replace("OWNER_VTABLE_MATCH\t1", "OWNER_VTABLE_MATCH\t0")
        else: c["resources_unchanged"] = False
        with pytest.raises(ValueError):
            audit.compare(a, b, bo, c, co, output)
