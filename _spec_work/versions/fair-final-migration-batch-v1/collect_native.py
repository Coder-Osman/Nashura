import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parent
path = ROOT.parent / "fair-final-migration-integration-v1/collect_native.py"
spec = importlib.util.spec_from_file_location("batched_native_collector", path)
collector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collector)
collector.ARTIFACTS += ("payload.tar.gz", "benchmark.log", "benchmark/report.json",
    "benchmark/update-mismatch.json", "selected.pt", "selected.report.json", "selection.log")


if __name__ == "__main__":
    collector.main()
