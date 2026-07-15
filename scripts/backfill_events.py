"""历史事件回填:用当前规则引擎重放库里已有的轨迹点,补生成事件。

会先删除该设备在窗口内的旧事件再重放(幂等,可反复执行)。
只回填规则引擎上线前的时段,避免与实时检测重复。

用法(容器内 /app 或本地项目根目录):
    python scripts/backfill_events.py --device 14808381029 \
        --start "2026-07-15 00:00:00" --end "2026-07-15 23:05:00"
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tracker.events import EventDetector  # noqa: E402
from tracker.storage import DB_DIR, Storage  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", required=True)
    ap.add_argument("--start", required=True, help="YYYY-MM-DD HH:MM:SS")
    ap.add_argument("--end", required=True)
    ap.add_argument("--db", default=str(DB_DIR / "tracker.db"))
    args = ap.parse_args()

    storage = Storage(args.db)
    with storage._lock:
        cur = storage._conn.execute(
            "DELETE FROM events WHERE device_id = ? AND start_time >= ? AND start_time <= ?",
            (args.device, args.start, args.end),
        )
        storage._conn.commit()
    print(f"清除窗口内旧事件 {cur.rowcount} 条")

    points = storage.track(args.device, start=args.start, end=args.end,
                           limit=50000, only_located=False)
    print(f"重放 {len(points)} 个轨迹点 ...")
    detector = EventDetector(storage)
    for p in points:
        detector.process(args.device, p, lon_bd=p.get("lon_bd"), lat_bd=p.get("lat_bd"))

    events = storage.list_events(args.device, start=args.start, end=args.end, limit=1000)
    print(f"回填事件 {len(events)} 条:")
    for e in reversed(events):
        d = e["detail"]
        info = d.get("direction") or (f"{d.get('duration_s')}s" if d.get("duration_s") else "") or \
               (f"{d.get('from_kmh')}->{d.get('to_kmh')}km/h" if d.get("from_kmh") is not None else "")
        print(f"  [{e['type']:12s}] {e['start_time']} ~ {e.get('end_time') or '...'}  {info}")


if __name__ == "__main__":
    main()
