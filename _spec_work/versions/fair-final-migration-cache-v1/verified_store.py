from copy import deepcopy
import hashlib

from migration_series import ReadOnlyMigrationStore
from native_runner.training.fair_search.contracts import require_sha


class VerifiedEpisodeStore(ReadOnlyMigrationStore):
    def __init__(self, root, *, manifest):
        super().__init__(root, manifest=manifest)
        self._verified_key = None
        self._verified_episode = None

    def _read(self, digest, expected_sha):
        require_sha(digest)
        require_sha(expected_sha)
        key = (self.dataset_sha, digest, expected_sha)
        if key == self._verified_key:
            data = (self.root / "episodes" / f"{digest}.pt.gz").read_bytes()
            if hashlib.sha256(data).hexdigest() != expected_sha:
                raise ValueError("migration file checksum differs")
            return self._verified_episode
        episode = super()._read(digest, expected_sha)
        self._verified_key = key
        self._verified_episode = episode
        return episode

    def load(self, match_id):
        return deepcopy(super().load(match_id))

    def load_rows(self, match_id, offsets):
        episode, receipt = super().load(match_id)
        offsets = list(offsets)
        if (not offsets or len(set(offsets)) != len(offsets)
                or any(type(offset) is not int or not 0 <= offset < len(episode["rows"]) for offset in offsets)):
            raise ValueError("distinct valid migration row offsets required")
        return deepcopy([episode["rows"][offset] for offset in offsets]), receipt
