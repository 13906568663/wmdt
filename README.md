# 外卖平台 · 车辆轨迹服务(v1)

独立项目:两类硬件接入同一平台 —— 车机按 **JT/T 808**(1 秒 1 包)、
智能刹车盒按 **MQTT + JSON**(默认 10 秒 1 包),端口独立;
后端接收入库,网页上用**百度地图**展示实时轨迹与历史轨迹。

## 功能范围(第一版)

- JT808 TCP 接入(端口 18808):0x7e 转义/异或校验,**2013 与 2019 消息头自动识别**;
  注册(0x0100→0x8100)、鉴权(0x0102)、心跳(0x0002)、位置汇报(0x0200)、其余消息回通用应答;
- 位置附加信息解析:标准里程/信号强度/卫星数 + **0xF1 陀螺仪扩展**
  (12 字节:三轴角速度 + 三轴加速度,int16 大端有符号,per 协议文档);
- MQTT 接入(端口 18883,内置轻量 broker,3.1/3.1.1,QoS0/1/2 收包):
  任意 topic,按 payload `devId` 识别设备;GPS/陀螺仪字段入轨迹库,
  雷达/刹车/油耗等其余字段原样存 `extras`;
- 坐标转换:设备上报 WGS-84 → 存库同时算好 **BD-09**(百度地图直接可画,不偏移);
- 地图页:设备列表(在线状态 + 协议标签)、实时跟踪(1 秒增量刷新、视角跟随)、
  历史轨迹按时间段查询、右下角实时遥测面板(车速/方向/陀螺仪三轴/加速度三轴);
  静止 GPS 漂移抑制(60 秒净位移判定),原始数据可用 `all=1` 全量取回;
- 两类模拟器:没有真机也能全链路联调,深圳南山虚拟路线匀速行驶。

## 快速开始

```bash
git clone https://github.com/13906568663/wmdt.git && cd wmdt
uv sync
uv run python main.py
```

- 地图页面:http://127.0.0.1:18209/
- JT808 接入:TCP `18808` 端口;MQTT 接入:TCP `18883` 端口
- 数据落在 `data/tracker.db`(SQLite,删掉即清空)
- 原始报文按天落在 `data/raw/jt808-*.log`(hex)与 `data/raw/mqtt-*.log`(topic+JSON),
  保留 7 天,排查协议问题用

另开一个终端跑模拟器:

```bash
uv run python scripts/simulator.py                 # JT808,连本机一直跑
uv run python scripts/simulator.py --host <服务器IP> --device 13900001111
uv run python scripts/mqtt_simulator.py            # MQTT,连本机一直跑
uv run python scripts/mqtt_simulator.py --host <服务器IP> --device 983104015411
```

环境变量:`TRACKER_WEB_PORT`(默认 18209)、`TRACKER_TCP_PORT`(默认 18808)、
`TRACKER_MQTT_PORT`(默认 18883)、`BAIDU_MAP_AK`(默认已内置提供的 key)。

## REST API

| 接口 | 说明 |
|---|---|
| `GET /api/devices` | 设备列表(最新位置、在线状态、轨迹点数) |
| `GET /api/devices/{id}/latest` | 最新一个轨迹点(含陀螺仪) |
| `GET /api/devices/{id}/track?since_id=&start=&end=&limit=` | 轨迹查询:增量拉取用 `since_id`,历史回放用时间段 |
| `GET /api/devices/{id}/events?start=&end=&limit=` | 事件查询:摔车(fall/fall_suspect,含方向)、急刹(hard_brake)、颠簸(bump)、停驻(stop_short/stop_long) |
| `GET /api/config` | 前端配置(百度 AK、TCP 端口) |

事件由服务端规则引擎(`tracker/events.py`)在数据入库时实时判定:六轴自标定
(自动识别加速度计字段组与刻度,兼容固件字段互换)+ 倾角/陀螺尖峰/速度序列规则,
阈值依据 `docs/事件规则分析_20260715夜测.md`,可用 `scripts/replay_events.py` 回放留存 CSV 验证。

## 0xF1 陀螺仪扩展(协议文档摘要)

位置汇报(0x0200)附加信息,ID `0xF1`,长度 12 字节,均为 int16 大端有符号:

| 字节 | 含义 |
|---|---|
| 1~2 | X 轴角速度(左右侧翻/倾斜速度) |
| 3~4 | Y 轴角速度(前后俯仰/颠簸速度) |
| 5~6 | Z 轴角速度(转向) |
| 7~8 | X 轴加速度(左右受力) |
| 9~10 | Y 轴加速度(前后惯性,急刹车) |
| 11~12 | Z 轴加速度(垂直受力,静止约 1000mG) |

## 部署到服务器(docker,两类硬件独立两套)

JT808 与 MQTT **各跑一个实例**:独立端口、独立数据库、独立页面,互不可见。

```bash
docker build -t waimai-tracker .

# 实例一:JT808(页面 18209,接入 18808)
docker run -d --name wmdt --restart unless-stopped \
  -e TRACKER_ENABLE_MQTT=0 \
  -p 18209:18209 -p 18808:18808 \
  -v wmdt_data:/app/data waimai-tracker

# 实例二:MQTT(页面 18309,接入 18883)
docker run -d --name wmdt-mqtt --restart unless-stopped \
  -e TRACKER_ENABLE_JT808=0 \
  -p 18309:18209 -p 18883:18883 \
  -v wmdt_mqtt_data:/app/data waimai-tracker
```

| 实例 | 硬件接入 | 轨迹页面 |
|---|---|---|
| JT808 | `服务器IP:18808`(TCP) | `http://服务器IP:18209/` |
| MQTT | `服务器IP:18883`(TCP) | `http://服务器IP:18309/` |

设备接入细节见 `docs/设备接入文档.md`(JT808)与 `docs/MQTT设备接入文档.md`。

## 目录结构

```text
.
├── main.py                      # 入口:JT808 + MQTT + Web 同进程三端口
├── scripts/simulator.py         # JT808 车机模拟器(注册/鉴权/1Hz上报/陀螺仪)
├── scripts/mqtt_simulator.py    # MQTT 硬件模拟器(JSON 全字段/QoS1)
└── tracker/
    ├── jt808.py            # JT808 编解码(转义/校验/双版本头/0x0200/0xF1)
    ├── mqtt_server.py      # 内置 MQTT 接入服务(独立端口,JSON→轨迹库)
    ├── geo.py              # WGS84 → GCJ02 → BD09
    ├── server.py           # asyncio TCP 接入服务(JT808)
    ├── storage.py          # SQLite 存储
    ├── api.py              # REST API
    ├── rawlog.py           # 原始报文按天落盘(7 天滚动)
    └── static/             # 百度地图页面(原生 JS)
```
