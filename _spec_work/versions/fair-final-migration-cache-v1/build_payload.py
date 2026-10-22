import hashlib
import io
import json
from pathlib import Path
import tarfile


ROOT = Path(__file__).resolve().parent
SERIES = "_spec_work/versions/fair-final-migration-series-v1/"
SELECTION = "_spec_work/versions/fair-final-migration-selection-v1/"
PROBE = "_spec_work/versions/fair-final-migration-integration-v1/probe_native_resume.py"


def replace_once(source, before, after):
    if source.count(before) != 1:
        raise ValueError("frozen integration anchor differs")
    return source.replace(before, after, 1)


def build(parent, *, deterministic=False):
    parent = Path(parent)
    launch = json.loads((parent / "launch.json").read_text())
    package = (parent / "payload.tar.gz").read_bytes()
    if hashlib.sha256(package).hexdigest() != launch["payload_sha256"]:
        raise ValueError("parent payload checksum differs")
    with tarfile.open(fileobj=io.BytesIO(package), mode="r:gz") as archive:
        files = {member.name: archive.extractfile(member).read() for member in archive.getmembers() if member.isfile()}
    for name, digest in launch["source_sha256"].items():
        if hashlib.sha256(files[name]).hexdigest() != digest:
            raise ValueError("parent deployment differs")
    original = dict(files)
    source = files[SERIES + "series_training.py"].decode()
    source = replace_once(source, "from migration_series import ReadOnlyMigrationStore, audit_completed_series",
        "from migration_series import audit_completed_series\nfrom verified_store import VerifiedEpisodeStore as ReadOnlyMigrationStore")
    source = replace_once(source, "loaded, frames, legalities = {}, [], []", "loaded, frames, legalities = {}, [], []\n        groups = {}\n        for row_id in ids:\n            number, match_id, offset, _ = self.index[row_id]\n            groups.setdefault((number, match_id), []).append(offset)")
    source = replace_once(source, "episode, receipt = self.stores[number].load(match_id)",
        "offsets = groups[(number, match_id)]\n                rows, receipt = self.stores[number].load_rows(match_id, offsets)")
    source = replace_once(source, "loaded[match_id] = episode\n            row = loaded[match_id][\"rows\"][offset]",
        "loaded[match_id] = dict(zip(offsets, rows))\n            row = loaded[match_id][offset]")
    source = replace_once(source, "paths = [Path(__file__), Path(migration_series.__file__),",
        "paths = [Path(__file__), Path(__file__).with_name('verified_store.py'), Path(migration_series.__file__),")
    files[SERIES + "series_training.py"] = source.encode()
    files[SERIES + "verified_store.py"] = (ROOT / "verified_store.py").read_bytes()
    entry = files[SELECTION + "train_round.py"].decode()
    entry = replace_once(entry, "    dataset = SeriesMigrationDataset(plan, args.data_roots, teacher=teacher)",
        "    print(json.dumps(dict(event='dataset_index_started')), flush=True)\n    dataset = SeriesMigrationDataset(plan, args.data_roots, teacher=teacher)\n    print(json.dumps(dict(event='dataset_index_completed')), flush=True)")
    entry = replace_once(entry, "        run = MigrationRound(trainer, dataset, config=config)",
        "        print(json.dumps(dict(event='initial_validation_started')), flush=True)\n        run = MigrationRound(trainer, dataset, config=config)\n        print(json.dumps(dict(event='initial_validation_completed')), flush=True)")
    entry = replace_once(entry, "        metrics = run.advance(dataset)",
        "        print(json.dumps(dict(event='update_started', step=run.trainer.optimizer_steps + 1)), flush=True)\n        metrics = run.advance(dataset)")
    files[SELECTION + "train_round.py"] = entry.encode()
    selection = files[SELECTION + "round_selection.py"].decode()
    selection = replace_once(selection, "import io\n", "import io\nimport json\nimport time\n")
    selection = replace_once(selection, "                for row_id in row_ids:",
        "                started = time.monotonic()\n                for row_number, row_id in enumerate(row_ids, 1):")
    selection = replace_once(selection, "                        values[key].append(float(metrics[key]))",
        "                        values[key].append(float(metrics[key]))\n                    if row_number % 32 == 0 or row_number == len(row_ids):\n                        print(json.dumps(dict(event='validation_progress', rows=row_number, total=len(row_ids),\n                            seconds=time.monotonic()-started)), flush=True)")
    files[SELECTION + "round_selection.py"] = selection.encode()
    if deterministic:
        files[SERIES + "deterministic_runtime.py"] = (ROOT / "deterministic_runtime.py").read_bytes()
        source = files[SERIES + "series_training.py"].decode()
        source = replace_once(source, "Path(__file__).with_name('verified_store.py'),",
            "Path(__file__).with_name('verified_store.py'), Path(__file__).with_name('deterministic_runtime.py'),")
        files[SERIES + "series_training.py"] = source.encode()
        entry = files[SELECTION + "train_round.py"].decode()
        entry = replace_once(entry, "    torch.set_num_threads(1)",
            "    from deterministic_runtime import configure\n    print(json.dumps(configure()), flush=True)\n    torch.set_num_threads(1)")
        files[SELECTION + "train_round.py"] = entry.encode()
        probe = (ROOT.parent / "fair-final-migration-integration-v1/probe_native_resume.py").read_text()
        probe = replace_once(probe, "    torch.set_num_threads(1)",
            "    from deterministic_runtime import configure\n    print(json.dumps(configure()), flush=True)\n    torch.set_num_threads(1)")
        files[PROBE] = probe.encode()
    changed = {name: dict(before=hashlib.sha256(original[name]).hexdigest() if name in original else None,
        after=hashlib.sha256(data).hexdigest()) for name, data in files.items()
        if name != "deployment.json" and original.get(name) != data}
    identity = {name: hashlib.sha256(data).hexdigest() for name, data in files.items() if name != "deployment.json"}
    files["deployment.json"] = json.dumps(identity, sort_keys=True).encode()
    for name, data in files.items():
        if name.endswith(".py"):
            compile(data, name, "exec")
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, data in sorted(files.items()):
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    return buffer.getvalue(), identity, changed
