# 外卖平台 · 车辆轨迹服务(v1)

独立项目:硬件车机按 **JT/T 808** 协议(1 秒 1 包)上报位置与陀螺仪数据,
后端接收入库,网页上用**百度地图**展示实时轨迹与历史轨迹。

## 功能范围(第一版)

- JT808 TCP 接入:0x7e 转义/异或校验,**2013 与 2019 消息头自动识别**;
  注册(0x0100→0x8100)、鉴权(0x0102)、心跳(0x0002)、位置汇报(0x0200)、其余消息回通用应答;
- 位置附加信息解析:标准里程/信号强度/卫星数 + **0xF1 陀螺仪扩展**
  (12 字节:三轴角速度 + 三轴加速度,int16 大端有符号,per 协议文档);
- 坐标转换:设备上报 WGS-84 → 存库同时算好 **BD-09**(百度地图直接可画,不偏移);
- 地图页:设备列表(在线状态)、实时跟踪(1 秒增量刷新、视角跟随)、
  历史轨迹按时间段查询、右下角实时遥测面板(车速/方向/陀螺仪三轴/加速度三轴);
- 车机模拟器:没有真机也能全链路联调,深圳南山虚拟路线匀速行驶。

## 快速开始

```bash
git clone https://github.com/13906568663/wmdt.git && cd wmdt
uv sync
uv run python main.py
```

- 地图页面:http://127.0.0.1:18209/
- JT808 接入:TCP `18808` 端口(硬件把服务器地址配到这里)
- 数据落在 `data/tracker.db`(SQLite,删掉即清空)

另开一个终端跑模拟器:

```bash
uv run python scripts/simulator.py                 # 连本机,一直跑
uv run python scripts/simulator.py --duration 120  # 只跑 2 分钟
uv run python scripts/simulator.py --host <服务器IP> --device 13900001111
```

环境变量:`TRACKER_WEB_PORT`(默认 18209)、`TRACKER_TCP_PORT`(默认 18808)、
`BAIDU_MAP_AK`(默认已内置提供的 key)。

## REST API

| 接口 | 说明 |
|---|---|
| `GET /api/devices` | 设备列表(最新位置、在线状态、轨迹点数) |
| `GET /api/devices/{id}/latest` | 最新一个轨迹点(含陀螺仪) |
| `GET /api/devices/{id}/track?since_id=&start=&end=&limit=` | 轨迹查询:增量拉取用 `since_id`,历史回放用时间段 |
| `GET /api/config` | 前端配置(百度 AK、TCP 端口) |

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

## 部署到服务器(docker)

```bash
docker build -t waimai-tracker .
docker run -d --name waimai-tracker --restart unless-stopped \
  -p 18209:18209 -p 18808:18808 -v waimai_tracker_data:/app/data waimai-tracker
```

硬件端把接入地址配置为 `服务器IP:18808`(TCP),打开 `http://服务器IP:18209/` 即可看轨迹。

## 目录结构

```text
.
├── main.py                 # 入口:TCP 接入 + Web 同进程双端口
├── scripts/simulator.py    # 车机模拟器(注册/鉴权/1Hz上报/陀螺仪)
└── tracker/
    ├── jt808.py            # JT808 编解码(转义/校验/双版本头/0x0200/0xF1)
    ├── geo.py              # WGS84 → GCJ02 → BD09
    ├── server.py           # asyncio TCP 接入服务
    ├── storage.py          # SQLite 存储
    ├── api.py              # REST API
    └── static/             # 百度地图页面(原生 JS)
```
