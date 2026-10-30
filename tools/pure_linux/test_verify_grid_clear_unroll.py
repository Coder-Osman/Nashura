"""Independent grid experiment gates, paired batch accounting and tamper checks."""
from copy import deepcopy
import gzip
import json
from pathlib import Path
import pytest

import run_headless_match as runner
import verify_grid_clear_unroll as grid
from test_run_headless_match import inputs
from test_combined_followup import combined_evidence


def batches():
    return [{"index":i,"variant":v,"summary":{"matches":216,"measured_matches":200,
        "median_match_ms":100 if v=="combined" else 98,"tick":17,
        "timed_seconds":20 if v=="combined" else 19.6,
        "step_percentiles_us":{"p50":5,"p95":10,"p99":20}}}
        for i,v in enumerate(grid.ORDER,1)]


def test_statistics_keep_batch_units():
    result=grid.aggregate(batches())
    assert result["duration_change_percent"]==pytest.approx(-2)
    assert result["block_duration_change_percent"]==pytest.approx([-2,-2])
    assert result["throughput_change_percent"]==pytest.approx((20/19.6-1)*100)
    assert result["variants"]["candidate"]["batches"]==4


@pytest.mark.parametrize("mutation",["missing","order","count","warmup"])
def test_missing_reordered_or_wrong_match_batches_rejected(mutation):
    rows=batches()
    if mutation=="missing":rows.pop()
    elif mutation=="order":rows.reverse()
    elif mutation=="count":rows[0]["summary"]["matches"]=215
    else:rows[0]["summary"]["measured_matches"]=199
    with pytest.raises(ValueError):grid.aggregate(rows)


def synthetic(inputs,monkeypatch):
    previous,_,_=combined_evidence(inputs,monkeypatch)
    root=previous.parent/"grid";root.mkdir()
    monkeypatch.setattr(grid,"GUEST_ROOT",root)
    observer=root.parent/"grid-observer.so"
    observer.write_bytes(b"synthetic grid observer")
    observer=str(observer)
    monkeypatch.setattr(grid,"OBSERVER",observer)
    def save(p,v):p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(v))
    sha=grid.sha256(observer)
    env={"cpu_count":2,"affinity":[0,1],"memory_mib_expected":6144,"epoch_ns":1}
    save(root/"stage-plan.json",{"variants":grid.VARIANTS,"cases":list(grid.CASES),
        "order":list(grid.ORDER),"benchmark_repeats":216,"warmup_matches":16,
        "profiling":False,"concurrency":False,"engineering_checks_finished_before_launch":True,
        "environment":env,"observer_sha256":sha})
    save(root/"done.json",{"runs":32,"successes":32,"environment":env})
    save(root/"correctness-done.json",{"runs":12,"successes":12})
    save(root/"instruction-check.json",{"exit_code":0,"stderr":"","stdout":
        "GRID_CLEAR_UNROLL_CHECK cases=24107 original_and_candidate=equal guard_pages=passed duplicate_cells=covered signed_modular_product=covered"})
    (root/"controller.exit").write_text("0")
    gates=[];timings=[]
    for name in grid.CASES:
        old_settings=grid.read(previous/name/"settings.json");settings=deepcopy(old_settings)
        settings["observer"]=observer;settings["identities"][observer]=sha
        save(root/name/"settings.json",settings)
        for variant in grid.VARIANTS:
            source=previous/name/("full-original" if variant=="original" else "full-combined")
            directory=root/name/("full-"+variant)
            plan=grid.read(source/"plan.json");old=plan["output"]
            plan["command"]=[x.replace(old,str(directory)).replace(old_settings["observer"],observer) for x in plan["command"]]
            plan["environment"]={k:v.replace(old,str(directory)) for k,v in plan["environment"].items()}
            if grid.VARIANTS[variant]:plan["environment"]["NASHURA_NATIVE_OPTIMIZATION"]=grid.VARIANTS[variant]
            plan.update(output=str(directory),identities=settings["identities"],native_optimization=grid.VARIANTS[variant])
            save(directory/"plan.json",plan)
            text=gzip.decompress((source/"stdout.log.gz").read_bytes()).decode()
            lines=[x for x in text.splitlines() if not x.startswith("HEADLESS_NATIVE_OPTIMIZATION ")]
            # Fourway synthetic full logs lack action metrics; add the exact replay count.
            replay=grid.read(settings["replay"])
            lines.append(f'HEADLESS_BATTLE_METRICS loaded_commands={len(replay["cmd"])} injected_commands={len(replay["inject"])}')
            if grid.VARIANTS[variant]:lines=runner.optimization_installation_markers(grid.VARIANTS[variant])+lines
            (directory/"stdout.log.gz").write_bytes(gzip.compress(chr(10).join(lines).encode()))
            archive=(source/"snapshots.bin.gz").read_bytes();(directory/"snapshots.bin.gz").write_bytes(archive)
            (directory/"exit.txt").write_text("0")
            pair={"records":18,"right":{"bytes":len(gzip.decompress(archive)),"sha256":grid.hashlib.sha256(gzip.decompress(archive)).hexdigest()}}
            result=grid.result_expected(variant,17,pair=pair);save(directory/"result.json",result)
            gates.append({"case":name,"variant":variant,"result":result,"frames":18,"observed_lines":1,
                "full_frames_equal":True,"observed_equal":True,"snapshot_gzip_sha256":grid.sha256(directory/"snapshots.bin.gz"),
                "stdout_gzip_sha256":grid.sha256(directory/"stdout.log.gz")})
        source=previous/name/"timing/02-combined"
        template=gzip.decompress((source/"stdout.log.gz").read_bytes()).decode()
        lines=[x for x in template.splitlines() if not x.startswith("HEADLESS_NATIVE_OPTIMIZATION ")]
        for index,variant in enumerate(grid.ORDER,1):
            directory=root/name/"timing"/(str(index).zfill(2)+"-"+variant)
            plan=runner.run(settings,directory,benchmark_repeats=216,timeout_seconds=120,
                native_optimization=grid.VARIANTS[variant],compress_stdout=True)
            save(directory/"plan.json",plan)
            text=chr(10).join(runner.optimization_installation_markers(grid.VARIANTS[variant])+lines)
            (directory/"stdout.log.gz").write_bytes(gzip.compress(text.encode()));(directory/"exit.txt").write_text("0")
            result=grid.result_expected(variant,17);save(directory/"result.json",result)
            summary=grid.summarize(text,216,expected_tick=17,expected_digest="e16801510db89efd",expected_bytes=3,warmup_matches=16)
            summary={k:v for k,v in summary.items() if k not in grid.COMPACT_OMIT}
            save(directory.parent/(directory.name+"-summary.json"),summary)
            save(directory.parent/(directory.name+"-environment.json"),env)
            timings.append({"case":name,"index":index,"variant":variant,"summary":summary,"result":result})
    save(root/"correctness-results.json",gates);save(root/"timing-results.json",timings)
    return root,previous


def test_complete_graph_and_tampered_evidence_rejected(inputs,monkeypatch):
    root,reference=synthetic(inputs,monkeypatch)
    result=grid.verify(root,reference)
    assert len(result["correctness"])==12 and result["timing_batches"]==32
    assert result["whole_goal_complete"] is False
    targets=[("done.json",lambda v:v.update(successes=31)),
        ("instruction-check.json",lambda v:v.update(exit_code=1)),
        ("stage-plan.json",lambda v:v.update(profiling=True)),
        (grid.CASES[0]+"/timing/01-combined/result.json",lambda v:v.update(matches=215)),
        (grid.CASES[0]+"/full-candidate/result.json",lambda v:v.update(archive_sha256="0"*64)),
        (grid.CASES[0]+"/timing/01-combined/plan.json",lambda v:v["environment"].update(NASHURA_NATIVE_PHASES="1"))]
    for rel,mutate in targets:
        p=root/rel;saved=p.read_text();value=json.loads(saved);mutate(value);p.write_text(json.dumps(value))
        with pytest.raises(ValueError):grid.verify(root,reference)
        p.write_text(saved)
