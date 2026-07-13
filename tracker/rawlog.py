"""原始报文按天落盘:上行/下行每一帧原文,便于事后逐字节排查。

文件:data/raw/{prefix}-YYYYMMDD.log,只保留最近 KEEP_DAYS 天(跨天时自动清理)。
行格式:时间 方向(RX/TX) 对端地址 设备号 内容(jt808 为 hex,mqtt 为 topic+JSON 文本)
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import IO

logger = logging.getLogger("rawlog")

KEEP_DAYS = 7


class RawLogger:
    def __init__(self, dir_path: Path, prefix: str = "jt808", keep_days: int = KEEP_DAYS) -> None:
        self.dir = dir_path
        self.dir.mkdir(parents=True, exist_ok=True)
        self.prefix = prefix
        self.keep_days = keep_days
        self._file: IO[str] | None = None
        self._day = ""

    def _cleanup(self) -> None:
        cutoff = time.strftime(
            "%Y%m%d", time.localtime(time.time() - self.keep_days * 86400)
        )
        for f in self.dir.glob(f"{self.prefix}-*.log"):
            day = f.stem.removeprefix(f"{self.prefix}-")
            if day < cutoff:
                try:
                    f.unlink()
                    logger.info("清理过期原始日志 %s", f.name)
                except OSError:
                    pass

    def _write(self, line: str) -> None:
        now = time.time()
        lt = time.localtime(now)
        day = time.strftime("%Y%m%d", lt)
        if day != self._day:
            if self._file:
                self._file.close()
            self._file = open(self.dir / f"{self.prefix}-{day}.log", "a", encoding="utf-8")
            self._day = day
            self._cleanup()
        ts = time.strftime("%H:%M:%S", lt) + f".{int(now * 1000) % 1000:03d}"
        self._file.write(f"{ts} {line}\n")
        self._file.flush()

    def log(self, direction: str, peer: str, device: str, data: bytes) -> None:
        self._write(f"{direction} {peer} {device or '-'} {data.hex()}")

    def log_text(self, direction: str, peer: str, device: str, text: str) -> None:
        self._write(f"{direction} {peer} {device or '-'} {text}")

    def close(self) -> None:
        if self._file:
            self._file.close()
            self._file = None
