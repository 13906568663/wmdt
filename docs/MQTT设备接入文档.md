# MQTT 设备接入文档(JSON 上报)

> 平台版本 v1 · 适用于按 MQTT + JSON 协议上报的智能刹车/ADAS 类硬件
> (与 JT808 设备共用同一平台与地图页面,端口独立)

## 一、接入地址

| 项目 | 地址 |
|---|---|
| MQTT 接入 | `186.244.238.6:18883`(TCP,不加密) |
| 轨迹地图页面 | `http://186.244.238.6:18209/` |
| 数据查询 API | `http://186.244.238.6:18209/api/devices` |

## 二、连接要求

- 协议版本:**MQTT 3.1 / 3.1.1**(5.0 基本连接也兼容),QoS 支持 0/1/2(建议 0 或 1);
- 无用户名密码校验(演示环境),client_id 任意但建议含设备号;
- keepalive 建议 60 秒,平台按 1.5 倍超时断开;
- **topic 任意**,建议 `wmdt/up/{devId}`;平台不按 topic 路由,按消息体里的 `devId` 识别设备;
- 消息体:**UTF-8 JSON**,字段见下表;`devId` 缺失的消息会被丢弃。

## 三、消息体字段

按协议文档全量支持;平台直接使用的字段:

| 字段 | 说明 | 平台用途 |
|---|---|---|
| `devId` | 设备编号(字符) | 设备唯一 ID(地图列表按它展示) |
| `ts` | 时间戳(秒,毫秒也兼容) | 定位时间 |
| `longitude` / `latitude` | GPS 经纬度,6 位小数,**WGS-84** | 轨迹坐标(百度纠偏由平台做) |
| `gpsStatus` | 0 未定位 / 1 定位 | 未定位的点存库但不画线 |
| `spdGPS` | GPS 车速 km/h | 车速显示与漂移过滤 |
| `direction` | 方向 0~359(正北 0) | 航向显示 |
| `height` | 海拔 m | 高程 |
| `acc` | ACC 状态 | 状态显示 |
| `gyroX/Y/Z` | 三轴角速度(浮点) | 遥测面板 |
| `accX/Y/Z` | 三轴加速度(浮点,静止 Z≈1000mG) | 遥测面板 |

其余字段(`dataType`、`triggerMark`、雷达距离/能量/角度、刹车事件、油耗、里程等)
**原样落库**在轨迹点的 `extras` 里,通过 API 可完整取回,后续可按需做刹车事件页面。

### 最小可用示例

```json
{
  "devId": "983104015411",
  "ts": 1783956120,
  "dataType": 2,
  "longitude": 113.950073,
  "latitude": 22.540026,
  "gpsStatus": 1,
  "spdGPS": 28,
  "direction": 69,
  "height": 15,
  "acc": 1,
  "gyroX": -2.04, "gyroY": 4.23, "gyroZ": -3.11,
  "accX": 16.9, "accY": 54.6, "accZ": 1025.5
}
```

## 四、联调验证步骤

1. 设备(或 mosquitto_pub)连 `186.244.238.6:18883`,向任意 topic 发布上表 JSON;
2. 打开 `http://186.244.238.6:18209/`,左侧设备列表出现设备号,标签显示 **MQTT**;
3. 点选设备:实时跟踪画轨迹,右下角遥测面板显示车速/方向/陀螺仪;
4. 接口核验:`GET http://186.244.238.6:18209/api/devices/{devId}/latest`,
   `extras` 里能看到雷达/刹车等全部透传字段;
5. 平台对每条上行消息(topic + JSON 原文)按天落盘 `data/raw/mqtt-日期.log`,
   联调有疑问时提供设备号和时间点即可逐条比对。

命令行快速测试(装了 mosquitto 的话):

```bash
mosquitto_pub -h 186.244.238.6 -p 18883 -t wmdt/up/983104015411 \
  -m '{"devId":"983104015411","ts":1783956120,"longitude":113.95,"latitude":22.54,"gpsStatus":1,"spdGPS":25,"direction":90}'
```

## 五、参考实现与常见问题

- `scripts/mqtt_simulator.py` 是完整的设备侧参考实现(CONNECT/PUBLISH QoS1/全字段 JSON):
  `uv run python scripts/mqtt_simulator.py --host 186.244.238.6 --device <设备号>`
- **连接立刻被断开**:检查 CONNECT 报文协议名(MQTT/MQIsdp)与 remaining length 编码;
- **列表里不出现设备**:消息不是合法 JSON 或缺 `devId`,查 `data/raw/mqtt-*.log`;
- **有设备无轨迹**:`gpsStatus` 为 0 或经纬度为 0,平台照收不画线;
- **轨迹整体偏移几百米**:上报的不是 WGS-84 原始坐标(可能已转 GCJ-02),请改回原始 GPS 坐标;
- **时间不对**:`ts` 用秒级 Unix 时间戳(毫秒也可,平台自动识别),不要发本地格式化字符串。
