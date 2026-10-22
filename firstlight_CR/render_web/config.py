from dataclasses import dataclass
import json
import os
from pathlib import Path

from native_runner.card_names import CARD_NAMES
from native_runner.training.mixed_decks import DECKS as MIXED_DECKS
from tools.league_decks import LAVA_HOUND_DECK, PEKKA_BRIDGE_SPAM_DECK

REPO = Path(__file__).resolve().parents[1]
DECKS = {
    "golem-beatdown": "石头人",
    "giant": "巨人",
    "hog-cycle": "野猪",
    "royal-giant": "皇家巨人",
    "recruits-hogs": "卫队猪",
    "pekka-bridge-spam": "皮卡",
    "lava-hound": "天狗",
}
MIXED_DECK_IDS = {
    "golem-beatdown": "golem", "giant": "giant",
    "hog-cycle": "hog", "royal-giant": "royal-giant",
    "recruits-hogs": "recruits-hogs",
}
DECK_CARDS = {
    **{name: MIXED_DECKS[training_id] for name, training_id in MIXED_DECK_IDS.items()},
    "pekka-bridge-spam": PEKKA_BRIDGE_SPAM_DECK,
    "lava-hound": LAVA_HOUND_DECK,
}


def deck_options():
    return [dict(id=name, label=label, training_id=MIXED_DECK_IDS.get(name),
                 cards=[dict(id=card, label=CARD_NAMES[card][0]) for card in DECK_CARDS[name]])
            for name, label in DECKS.items()]


SOURCE_RUN = "event-residual-v3-scheduler-100r-20260919"


@dataclass
class Config:
    data: Path = REPO / "artifacts/render-web"
    host: str = "127.0.0.1"
    port: int = 8765
    ssh_host: str = "172.16.48.37"
    ssh_user: str = "zhichun"
    ssh_key: str = ""
    ssh_password: str = ""
    metadata_host: str = "gujiga@172.16.48.34"
    metadata_root: str = "/home/gujiga/firstlight_CR/runs"
    archive_root: str = "/home/zhichun/firstlight_CR/runs"
    remote_root: str = "/home/zhichun/firstlight_CR/render-web"
    source_run: str = SOURCE_RUN
    remote_sdk: str = "/data/firstlight/home/android-sdk"
    remote_avd: str = "firstlight-render-web"
    remote_serial: str = "emulator-5590"
    remote_port: int = 27889
    adb: str = str(Path.home() / "Library/Android/sdk/platform-tools/adb")
    emulator: str = str(Path.home() / "Library/Android/sdk/emulator/emulator")
    avd: str = "astral-api30"
    serial: str = "emulator-5554"
    control_port: int = 26789
    # Human play and video rendering share the installed AVD, but use separate
    # emulator serials and native forwarding ports while holding worker.lock.
    human_avd: str = "astral-api30"
    human_serial: str = "emulator-5564"
    human_control_port: int = 27789
    ffmpeg: str = "/opt/homebrew/bin/ffmpeg"
    ffprobe: str = "/opt/homebrew/bin/ffprobe"
    min_free_bytes: int = 2_000_000_000

    @classmethod
    def load(cls):
        from native_runner.local_config import load_env

        load_env(REPO / ".env.render-web")
        c = cls()
        for name in cls.__dataclass_fields__:
            value = os.environ.get("CR_RENDER_" + name.upper())
            if value is not None:
                current = getattr(c, name)
                setattr(
                    c,
                    name,
                    (
                        Path(value).expanduser()
                        if isinstance(current, Path)
                        else int(value) if isinstance(current, int) else value
                    ),
                )
        c.data = c.data.resolve()
        if c.host != "127.0.0.1":
            raise ValueError("此版本仅绑定 127.0.0.1")
        return c

    def remote(self):
        return {
            k: str(getattr(self, k))
            for k in (
                "metadata_host",
                "metadata_root",
                "archive_root",
                "remote_root",
                "source_run",
                "remote_sdk",
                "remote_avd",
                "remote_serial",
                "remote_port",
            )
        }


def write_json(path, value):
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    tmp.replace(path)
