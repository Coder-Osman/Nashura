import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parent
path = ROOT.parent / "fair-final-migration-integration-v1/collect_native.py"
spec = importlib.util.spec_from_file_location("active_migration_collector", path)
collector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collector)
collector.ARTIFACTS += ("payload.tar.gz", "resources.json", "benchmark.log", "benchmark/report.json",
    "benchmark/inheritance.json", "benchmark/update-mismatch.json", "frozen.log", "trainable.log",
    "active-comparison/inheritance.json", "active-comparison/reference.json",
    "active-comparison/frozen.json", "active-comparison/trainable.json",
    "active-comparison/frozen-midpoint.pt", "active-comparison/frozen-reference.pt",
    "active-comparison/frozen-resumed.pt", "active-comparison/trainable-midpoint.pt",
    "active-comparison/trainable-reference.pt", "active-comparison/trainable-resumed.pt")


if __name__ == "__main__":
    collector.main()
