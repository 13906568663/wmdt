"""事件规则回放验证:把留存的路测 CSV 喂给 EventDetector,打印检出事件。

用法(项目根目录):
    uv run python scripts/replay_events.py docs/路测数据_20260715_2030-2130.csv
不带参数时默认回放 0715 夜测 CSV。
"""

from __future__ import annotations

import csv
import glob
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tracker.events import EventDetector  # noqa: E402
from tracker.storage import Storage  # noqa: E402


def main() -> None:
    if len(sys.argv) > 1:
        path = sys.argv[1]
    else:
        matches = glob.glob("docs/*2030-2130.csv")
        if not matches:
            raise SystemExit("未找到默认 CSV,请传入文件路径")
        path = matches[0]

    storage = Storage(":memory:")
    detector = EventDetector(storage)
    n = 0
    with open(path, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            point = {
                "gps_time": r["gps_time"],
                "speed": float(r["speed_kmh"] if "speed_kmh" in r else r["speed"]),
                "located": int(r.get("located") or 0),
                "gyro_x": int(r["gyro_x"]), "gyro_y": int(r["gyro_y"]), "gyro_z": int(r["gyro_z"]),
                "acc_x": int(r["acc_x"]), "acc_y": int(r["acc_y"]), "acc_z": int(r["acc_z"]),
            }
            detector.process("replay", point,
                             lon_bd=float(r.get("lon_bd") or 0), lat_bd=float(r.get("lat_bd") or 0))
            n += 1

    events = storage.list_events("replay", limit=1000)
    print(f"回放 {n} 点,检出事件 {len(events)} 条:")
    for e in reversed(events):
        d = e["detail"]
        span = e["start_time"][11:] + (f" ~ {e['end_time'][11:]}" if e.get("end_time") else " ~ ...")
        extra = []
        if d.get("direction"):
            extra.append(f"方向={d['direction']}")
        if d.get("tilt_max") is not None:
            extra.append(f"倾角={d['tilt_max']}")
        if d.get("from_kmh") is not None:
            extra.append(f"{d['from_kmh']}->{d['to_kmh']}km/h")
        if d.get("std_g") is not None:
            extra.append(f"std={d['std_g']}g")
        if d.get("duration_s") is not None:
            extra.append(f"时长={d['duration_s']}s")
        print(f"  [{e['type']:12s}] {span}  {' '.join(extra)}")


if __name__ == "__main__":
    main()
