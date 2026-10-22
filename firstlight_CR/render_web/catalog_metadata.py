"""Index trusted, completed training artifacts using the frozen loader contract."""

import hashlib
import json
from pathlib import Path
import sys

if __package__:
    from .checkpoint_sources import checkpoint_location
else:
    from checkpoint_sources import checkpoint_location


def main():
    code, source, archive, cache = sys.argv[1:]
    sys.path.insert(0, code)
    import torch
    from native_runner.training.event_checkpoint import _resolve, _contract_hash

    torch.set_num_threads(1)
    cache = Path(cache)
    known = json.loads(cache.read_text()) if cache.exists() else {}
    archive = Path(archive)
    rows = []
    seen = set()
    for item in json.loads(Path(source).read_text()):
        sha = item["sha256"]
        if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
            continue
        identity = hashlib.sha256((item["run"] + ":" + sha).encode()).hexdigest()[:24]
        if identity in seen:
            continue
        seen.add(identity)
        expected, allowed_root = checkpoint_location(archive, item)
        path = Path(expected)
        if not path.resolve().is_relative_to(Path(allowed_root).resolve()):
            raise ValueError("checkpoint outside archive")
        available = path.is_file()
        metadata = known.get(sha)
        if available and metadata is None:
            try:
                payload = torch.load(
                    path, map_location="cpu", weights_only=False, mmap=True
                )
                backbone, model, event = _resolve(payload, None, None)
                contract = payload["event_checkpoint_contract"]
                if not backbone.snapshot_only:
                    raise ValueError("仅支持无状态 checkpoint")
                compatible = {
                    k: contract[k]
                    for k in (
                        "input_semantics",
                        "action_semantics",
                        "backbone_config",
                        "event_config",
                    )
                }
                metadata = dict(
                    supported=True,
                    contract=contract,
                    contract_sha256=_contract_hash(contract),
                    compatibility=hashlib.sha256(
                        json.dumps(compatible, sort_keys=True).encode()
                    ).hexdigest(),
                    version=contract["version"],
                    iteration=payload.get("event_ppo_training", {}).get(
                        "iterations", item["iteration"]
                    ),
                )
            except Exception as exc:
                metadata = dict(supported=False, reason=str(exc)[:240])
            known[sha] = metadata
        metadata = metadata or dict(supported=False, reason="归档文件暂不可用")
        iteration = metadata.get("iteration", item["iteration"])
        if item.get("category") and iteration != item["iteration"]:
            raise ValueError("实验权重的训练轮次与完成记录不匹配")
        label = "IL 起点" if iteration == 0 else f"RL {iteration}"
        if item.get("category"):
            prefix = item.get("label_prefix", "塔血 β=1")
            progress = (f"{item['trial_round']}/{item['trial_updates']}"
                        if item.get("trial_updates") is not None
                        else f"续训第 {item['trial_round']} 轮")
            label = f"{prefix} · RL {iteration}（{progress}）"
        if item["initial"] and "residual" in item["run"] and iteration == 171:
            label += " · 残差迁移"
        rows.append(
            dict(
                metadata,
                id=identity,
                run=item["run"],
                sha256=sha,
                path=str(path),
                iteration=iteration,
                label=label,
                available=available,
                category=item.get("category", "formal"),
                category_label=item.get("category_label", "正式训练"),
                trial_round=item.get("trial_round"),
                trial_updates=item.get("trial_updates"),
            )
        )
    tmp = cache.with_suffix(".tmp")
    tmp.write_text(json.dumps(known))
    tmp.replace(cache)
    print(
        json.dumps(sorted(rows, key=lambda x: (x["iteration"], x["run"]), reverse=True))
    )


if __name__ == "__main__":
    main()
