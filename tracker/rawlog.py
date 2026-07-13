"""原始报文按天落盘:上行/下行每一帧的 hex 原文,便于事后逐字节排查。

文件:data/raw/jt808-YYYYMMDD.log
行格式:时间 方向(RX/TX) 对端地址 设备号 hex原文(含0x7e定界符)
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import IO


class RawLogger:
    def __init__(self, dir_path: Path) -> None:
        self.dir = dir_path
        self.dir.mkdir(parents=True, exist_ok=True)
        self._file: IO[str] | None = None
        self._day = ""

    def log(self, direction: str, peer: str, device: str, data: bytes) -> None:
        now = time.time()
        lt = time.localtime(now)
        day = time.strftime("%Y%m%d", lt)
        if day != self._day:
            if self._file:
                self._file.close()
            self._file = open(self.dir / f"jt808-{day}.log", "a", encoding="ascii")
            self._day = day
        ts = time.strftime("%H:%M:%S", lt) + f".{int(now * 1000) % 1000:03d}"
        self._file.write(f"{ts} {direction} {peer} {device or '-'} {data.hex()}\n")
        self._file.flush()

    def close(self) -> None:
        if self._file:
            self._file.close()
            self._file = None
