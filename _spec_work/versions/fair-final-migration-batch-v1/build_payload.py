import hashlib
import io
import json
from pathlib import Path
import tarfile


ROOT = Path(__file__).resolve().parent
MODULE = "firstlight_CR/native_runner/training/fair_final/batched_migration.py"
MIGRATION = "firstlight_CR/native_runner/training/fair_migration.py"
SERIES = "_spec_work/versions/fair-final-migration-series-v1/series_training.py"


def replace_once(source, before, after):
    if source.count(before) != 1:
        raise ValueError("frozen batched migration anchor differs")
    return source.replace(before, after, 1)


def transform_migration(source):
    source = replace_once(source,
        "from .fair_final.adapter import source_policy\nfrom .fair_final.learning import conditional_teacher_kl",
        "from .fair_final.batched_migration import source_policy, conditional_teacher_kl, student_policies")
    source = replace_once(source, "student_policies, student_wdl = student(frame, legalities)",
        "student_policies, student_wdl = batched_student_policies(student, frame, legalities)")
    return replace_once(source, "conditional_teacher_kl, student_policies",
        "conditional_teacher_kl, student_policies as batched_student_policies")


def build(parent):
    parent = Path(parent)
    launch = json.loads((parent / "launch.json").read_text())
    package = (parent / "payload.tar.gz").read_bytes()
    if hashlib.sha256(package).hexdigest() != launch["payload_sha256"]:
        raise ValueError("parent payload checksum differs")
    with tarfile.open(fileobj=io.BytesIO(package), mode="r:gz") as archive:
        files = {member.name: archive.extractfile(member).read() for member in archive.getmembers() if member.isfile()}
    identity = {name: hashlib.sha256(data).hexdigest() for name, data in files.items() if name != "deployment.json"}
    if identity != launch["source_sha256"] or json.loads(files["deployment.json"]) != identity:
        raise ValueError("parent deployment differs")
    original = dict(files)
    files[MIGRATION] = transform_migration(files[MIGRATION].decode()).encode()
    files[MODULE] = (ROOT / "batched_migration.py").read_bytes()
    series = replace_once(files[SERIES].decode(),
        'migration_series.SOURCE / "native_runner/training/fair_migration_trainer.py"]',
        'migration_series.SOURCE / "native_runner/training/fair_migration_trainer.py",\n'
        '        migration_series.SOURCE / "native_runner/training/fair_final/batched_migration.py",\n'
        '        migration_series.SOURCE / "native_runner/training/fair_migration.py"]')
    files[SERIES] = series.encode()
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
