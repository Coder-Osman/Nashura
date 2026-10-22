import hashlib
import io
import json
from pathlib import Path
import tarfile


ROOT = Path(__file__).resolve().parent
WORKSPACE = ROOT.parents[2]
SERIES = "_spec_work/versions/fair-final-migration-series-v1/series_training.py"
MIGRATION = "firstlight_CR/native_runner/training/fair_migration.py"
ACTIVE = tuple("firstlight_CR/native_runner/training/fair_final/" + name + ".py"
    for name in ("adapter", "learning", "model", "policy", "search")) + (MIGRATION,)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def replace_once(source, before, after):
    if source.count(before) != 1:
        raise ValueError("active migration payload anchor differs")
    return source.replace(before, after, 1)


def build(parent):
    parent = Path(parent)
    launch = json.loads((parent / "launch.json").read_text())
    package = (parent / "payload.tar.gz").read_bytes()
    if digest(package) != launch["payload_sha256"]:
        raise ValueError("parent payload checksum differs")
    with tarfile.open(fileobj=io.BytesIO(package), mode="r:gz") as archive:
        files = {}
        for member in archive.getmembers():
            relative = Path(member.name)
            if (not member.isfile() or relative.is_absolute() or ".." in relative.parts
                    or str(relative) != member.name or member.name in files):
                raise ValueError("parent payload member differs")
            files[member.name] = archive.extractfile(member).read()
    identity = {name: digest(data) for name, data in files.items() if name != "deployment.json"}
    if identity != launch["source_sha256"] or json.loads(files["deployment.json"]) != identity:
        raise ValueError("parent deployment differs")
    original = dict(files)
    for name in ("adapter", "learning"):
        path = "firstlight_CR/native_runner/training/fair_final/" + name + ".py"
        files[path.replace(name + ".py", "reference_" + name + ".py")] = original[path]
    reference = original[MIGRATION].decode()
    reference = replace_once(reference, "from .fair_final.adapter import source_policy",
        "from .fair_final.reference_adapter import source_policy")
    reference = replace_once(reference, "from .fair_final.learning import conditional_teacher_kl",
        "from .fair_final.reference_learning import conditional_teacher_kl")
    files["reference_migration.py"] = reference.encode()
    for path in ACTIVE:
        files[path] = (WORKSPACE / path).read_bytes()
    series = original[SERIES].decode()
    before = "return semantic_digest({path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths})"
    after = ("return semantic_digest(dict(pipeline={path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}, "
        f"active={{name: hashlib.sha256((migration_series.SOURCE.parent / name).read_bytes()).hexdigest() for name in {ACTIVE!r}}}))")
    files[SERIES] = replace_once(series, before, after).encode()
    batch_path = ROOT.parent / "fair-final-migration-batch-v1/probe_native_batch.py"
    batch = batch_path.read_text()
    batch = replace_once(batch, "students[0].copy_encoder(teacher)",
        "mapping, inheritance = students[0].copy_encoder_with_audit(teacher)\n"
        "        inheritance.update(source_checkpoint_sha256=plan['rounds'][0]['manifest']['identity']['source_a_sha'], "
        "student_initial_network_sha=semantic_digest(students[0].state_dict()))\n"
        "        if frozen:\n"
        "            (args.output / 'inheritance.json').write_text(json.dumps(inheritance, indent=2))")
    files["probe_active_batch.py"] = batch.encode()
    files["probe_active_resume.py"] = (ROOT / "probe_active_resume.py").read_bytes()
    changes = {name: dict(before=digest(original[name]) if name in original else None, after=digest(data))
        for name, data in files.items() if name != "deployment.json" and original.get(name) != data}
    identity = {name: digest(data) for name, data in files.items() if name != "deployment.json"}
    files["deployment.json"] = json.dumps(identity, sort_keys=True).encode()
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name, data in sorted(files.items()):
            if name.endswith(".py"):
                compile(data, name, "exec")
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    return output.getvalue(), identity, changes
