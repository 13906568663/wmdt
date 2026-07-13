"""原始报文按天落盘:上行/下行每一帧的 hex 原文,便于事后逐字节排查。

文件:data/raw/jt808-YYYYMMDD.log,只保留最近 KEEP_DAYS 天(跨天时自动清理)。
行格式:时间 方向(RX/TX) 对端地址 设备号 hex原文(含0x7e定界符)
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import IO

logger = logging.getLogger("jt808.rawlog")

KEEP_DAYS = 7


class RawLogger:
    def __init__(self, dir_path: Path, keep_days: int = KEEP_DAYS) -> None:
        self.dir = dir_path
        self.dir.mkdir(parents=True, exist_ok=True)
        self.keep_days = keep_days
        self._file: IO[str] | None = None
        self._day = ""

    def _cleanup(self, today: str) -> None:
        cutoff = time.strftime(
            "%Y%m%d", time.localtime(time.time() - self.keep_days * 86400)
        )
        for f in self.dir.glob("jt808-*.log"):
            day = f.stem.removeprefix("jt808-")
            if day < cutoff:
                try:
                    f.unlink()
                    logger.info("清理过期原始日志 %s", f.name)
                except OSError:
                    pass

    def log(self, direction: str, peer: str, device: str, data: bytes) -> None:
        now = time.time()
        lt = time.localtime(now)
        day = time.strftime("%Y%m%d", lt)
        if day != self._day:
            if self._file:
                self._file.close()
            self._file = open(self.dir / f"jt808-{day}.log", "a", encoding="ascii")
            self._day = day
            self._cleanup(day)
        ts = time.strftime("%H:%M:%S", lt) + f".{int(now * 1000) % 1000:03d}"
        self._file.write(f"{ts} {direction} {peer} {device or '-'} {data.hex()}\n")
        self._file.flush()

    def close(self) -> None:
        if self._file:
            self._file.close()
            self._file = None
