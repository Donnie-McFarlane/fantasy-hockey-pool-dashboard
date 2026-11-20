"""Refresh the scoring checkpoint using only aliases."""

import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from player_board import build

ROOT = Path(__file__).resolve().parents[1]


def main(checkpoint):
    with tempfile.TemporaryDirectory(prefix="batspool-scores-") as directory:
        folder = Path(directory)
        shutil.copyfile(checkpoint, folder / "pool_data.json")
        shutil.copyfile(ROOT / "update_pool_day.py", folder / "update_pool_day.py")
        subprocess.run([sys.executable, folder / "update_pool_day.py"], check=True, timeout=1000)
        updated = json.loads((folder / "pool_data.json").read_text(encoding="utf-8"))
        updated["playerBoard"] = build(updated, folder)
        staging = checkpoint.with_suffix(".next")
        staging.write_text(json.dumps(updated, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        staging.replace(checkpoint)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
