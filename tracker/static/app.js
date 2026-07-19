/* 车辆轨迹监控前端:百度地图 GL + 轮询式实时跟踪/历史轨迹 + 派单订单图层 */

const $ = (s) => document.querySelector(s);

let map = null;
let marker = null;
let polyline = null;
let trackPath = [];          // BMapGL.Point 数组
let currentDevice = null;
let mode = "live";           // live | history
let lastPointId = 0;
let liveTimer = null;
let deviceTimer = null;
let evtTimer = null;
let dispatchApi = null;      // 派单系统地址(空 = 不显示订单图层)
let focusDevice = null;      // 演示聚焦设备(空 = 多设备)

async function api(path) {
  const res = await fetch(`/api${path}`);
  if (!res.ok) throw new Error(`${path} -> ${res.status}`);
  return res.json();
}

/* ── 地图图标(SVG 内联) ── */

function svgIcon(svg, w, h, anchorX, anchorY) {
  return new BMapGL.Icon(
    "data:image/svg+xml;charset=utf-8," + encodeURIComponent(svg),
    new BMapGL.Size(w, h),
    { imageSize: new BMapGL.Size(w, h), anchor: new BMapGL.Size(anchorX ?? w / 2, anchorY ?? h / 2) }
  );
}

// 外卖骑手:橙色圆底 + 电动车骑手 + 外卖箱
const RIDER_SVG = `<svg xmlns="http://www.w3.org/2000/svg" width="48" height="48" viewBox="0 0 48 48">
  <circle cx="24" cy="24" r="21.5" fill="#ff5000" stroke="#fff" stroke-width="3"/>
  <circle cx="15" cy="31" r="4.6" fill="none" stroke="#fff" stroke-width="2.6"/>
  <circle cx="34" cy="31" r="4.6" fill="none" stroke="#fff" stroke-width="2.6"/>
  <path d="M15 31 L21 23 L29 23 L34 31" fill="none" stroke="#fff" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"/>
  <path d="M23 23 L25.5 15.5 L31 13.5" fill="none" stroke="#fff" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"/>
  <circle cx="32.5" cy="10.5" r="3.4" fill="#fff"/>
  <rect x="10.5" y="16.5" width="8" height="6.6" rx="1.4" fill="#fff"/>
</svg>`;

const SHOP_SVG = `<svg xmlns="http://www.w3.org/2000/svg" width="34" height="34" viewBox="0 0 34 34">
  <circle cx="17" cy="17" r="15" fill="#2563eb" stroke="#fff" stroke-width="2.5"/>
  <text x="17" y="22.5" text-anchor="middle" font-size="15" font-weight="700" fill="#fff" font-family="PingFang SC,Microsoft YaHei,sans-serif">店</text>
</svg>`;

const BUYER_SVG = `<svg xmlns="http://www.w3.org/2000/svg" width="34" height="34" viewBox="0 0 34 34">
  <circle cx="17" cy="17" r="15" fill="#16a34a" stroke="#fff" stroke-width="2.5"/>
  <text x="17" y="22.5" text-anchor="middle" font-size="15" font-weight="700" fill="#fff" font-family="PingFang SC,Microsoft YaHei,sans-serif">客</text>
</svg>`;

const EVENT_DOT_SVG = (color) => `<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 16 16">
  <circle cx="8" cy="8" r="6" fill="${color}" stroke="#fff" stroke-width="2"/>
</svg>`;

let RIDER_ICON, SHOP_ICON, BUYER_ICON, FALL_ICON, BRAKE_ICON;

function initIcons() {
  RIDER_ICON = svgIcon(RIDER_SVG, 48, 48);
  SHOP_ICON = svgIcon(SHOP_SVG, 34, 34);
  BUYER_ICON = svgIcon(BUYER_SVG, 34, 34);
  FALL_ICON = svgIcon(EVENT_DOT_SVG("#e02020"), 16, 16);
  BRAKE_ICON = svgIcon(EVENT_DOT_SVG("#d97706"), 16, 16);
}

function riderMarker(pos) {
  const m = new BMapGL.Marker(pos, { icon: RIDER_ICON });
  map.addOverlay(m);
  return m;
}

/* ── 初始化:拿配置,动态加载百度地图 JS ── */

(async function boot() {
  const cfg = await api("/config");
  const lines = [];
  if (cfg.tcp_port) lines.push(`JT808 接入: TCP ${cfg.tcp_port}(2013/2019,0xF1 陀螺仪)`);
  if (cfg.mqtt_port) lines.push(`MQTT 接入: TCP ${cfg.mqtt_port}(JSON,按 devId 识别)`);
  $("#connInfo").textContent = lines.join("\n");
  dispatchApi = cfg.dispatch_api || null;
  focusDevice = cfg.focus_device || null;
  window.__initMap = initMap;
  const script = document.createElement("script");
  script.src = `https://api.map.baidu.com/api?type=webgl&v=1.0&ak=${cfg.baidu_ak}&callback=__initMap`;
  document.body.appendChild(script);
})();

function initMap() {
  initIcons();
  map = new BMapGL.Map("map");
  map.centerAndZoom(new BMapGL.Point(120.005, 30.29), 14); // 默认杭州仓前(梦想小镇一带)
  map.enableScrollWheelZoom(true);
  map.addControl(new BMapGL.ZoomControl());
  map.addControl(new BMapGL.ScaleControl());

  refreshDevices();
  deviceTimer = setInterval(refreshDevices, 5000);
  if (dispatchApi) startOrders();
}

/* ── 设备列表 ── */

async function refreshDevices() {
  let data;
  try {
    data = await api("/devices");
  } catch (e) {
    return;
  }
  let devices = data.devices || [];
  // 演示聚焦:只显示指定设备
  if (focusDevice) devices = devices.filter((d) => d.device_id === focusDevice);
  $("#devCount").textContent = devices.length ? `共 ${devices.length} 台` : "";
  const list = $("#deviceList");
  if (!devices.length) {
    list.innerHTML = '<div class="empty">等待设备接入…<br/><span class="muted">让硬件连到下方 TCP 端口即可</span></div>';
    return;
  }
  list.innerHTML = devices
    .map((d) => {
      const active = d.device_id === currentDevice ? " active" : "";
      const time = d.gps_time ? d.gps_time.slice(5) : "无轨迹";
      const proto = (d.protocol || "jt808").toUpperCase();
      return `<div class="device-card${active}" onclick="selectDevice('${d.device_id}')">
        <div class="device-id"><span class="dot ${d.online ? "on" : "off"}"></span>${d.device_id}
          <span class="proto-tag">${proto}</span></div>
        <div class="device-meta">
          <span>${d.speed != null ? d.speed + " km/h" : "-"}</span>
          <span>${time}</span>
          <span>${d.point_count} 点</span>
        </div>
      </div>`;
    })
    .join("");
  if (!currentDevice && devices.length) {
    selectDevice(devices[0].device_id);
  }
}

window.selectDevice = function (deviceId) {
  if (currentDevice === deviceId) return;
  currentDevice = deviceId;
  $("#teleDevice").textContent = deviceId;
  resetTrack();
  renderEvents([]);
  refreshDevices();
  if (mode === "live") startLive();
};

/* ── 轨迹绘制 ── */

function resetTrack() {
  if (polyline) { map.removeOverlay(polyline); polyline = null; }
  if (marker) { map.removeOverlay(marker); marker = null; }
  for (const id of Object.keys(eventDots)) { map.removeOverlay(eventDots[id]); delete eventDots[id]; }
  trackPath = [];
  lastPointId = 0;
  $("#trackStats").textContent = "";
}

function appendPoints(points, { fit = false } = {}) {
  if (!points.length) return;
  for (const p of points) {
    lastPointId = Math.max(lastPointId, p.id);
  }
  const last = points[points.length - 1];
  const pos = new BMapGL.Point(last.lon_bd, last.lat_bd);

  // 轨迹线只在"历史轨迹"模式画;实时页只动骑手图标,保持画面干净
  if (mode === "history") {
    for (const p of points) {
      trackPath.push(new BMapGL.Point(p.lon_bd, p.lat_bd));
    }
    if (!polyline) {
      polyline = new BMapGL.Polyline(trackPath, {
        strokeColor: "#ff5000",
        strokeWeight: 5,
        strokeOpacity: 0.85,
      });
      map.addOverlay(polyline);
    } else {
      polyline.setPath(trackPath);
    }
  }

  if (!marker) {
    marker = riderMarker(pos);
  } else {
    marker.setPosition(pos);
  }
  updateTelemetry(last);

  if (mode === "history") {
    if (fit && trackPath.length > 1) map.setViewport(trackPath);
    $("#trackStats").textContent = `轨迹点:${trackPath.length} · 最新:${last.gps_time}`;
  } else {
    if (fit) map.centerAndZoom(pos, 16);
    else if ($("#followSwitch").checked) map.panTo(pos);
    $("#trackStats").textContent = `最新上报:${last.gps_time}`;
  }
}

/* ── 事件面板 ── */

const EVENT_META = {
  fall: "摔车",
  fall_suspect: "疑似摔车",
  hard_brake: "急刹车",
  bump: "颠簸路段",
  stop_short: "短暂停留",
  stop_long: "长时驻留",
};

function eventDesc(e) {
  const d = e.detail || {};
  const parts = [];
  if (e.type === "fall" || e.type === "fall_suspect") {
    if (d.direction && d.direction !== "不明") parts.push(`向${d.direction}倒`);
    if (d.tilt_max != null) parts.push(`倾角${d.tilt_max}°`);
  } else if (e.type === "hard_brake") {
    parts.push(`${d.from_kmh} → ${d.to_kmh} km/h`);
  } else if (e.type === "bump") {
    if (d.std_g != null) parts.push(`振动 ${d.std_g}g`);
  } else if (d.duration_s != null) {
    const m = Math.floor(d.duration_s / 60), s = d.duration_s % 60;
    parts.push(m ? `${m}分${s}秒` : `${s}秒`);
    if (d.ongoing) parts.push("进行中");
  }
  return parts.join(" · ");
}

function renderEvents(events) {
  const list = $("#eventList");
  $("#evtCount").textContent = events.length ? `${events.length} 条` : "";
  if (!events.length) {
    list.innerHTML = '<div class="empty muted">暂无事件</div>';
    return;
  }
  list.innerHTML = events
    .map((e) => {
      const d = e.detail || {};
      const pan = d.lon_bd ? ` onclick="panToEvent(${d.lon_bd},${d.lat_bd})"` : "";
      const end = e.end_time && e.end_time !== e.start_time ? ` ~ ${e.end_time.slice(11)}` : "";
      return `<div class="event-item"${pan}>
        <span class="event-tag ${e.type}">${EVENT_META[e.type] || e.type}</span>
        <div class="event-body">
          <div class="event-time">${(e.start_time || "").slice(5)}${end}</div>
          <div class="event-desc">${eventDesc(e)}</div>
        </div>
      </div>`;
    })
    .join("");
}

window.panToEvent = function (lon, lat) {
  if (map) map.panTo(new BMapGL.Point(lon, lat));
};

async function refreshEvents(range) {
  if (!currentDevice) return;
  try {
    let qs = "limit=50";
    if (range) qs += `&start=${encodeURIComponent(range.start)}&end=${encodeURIComponent(range.end)}`;
    const data = await api(`/devices/${currentDevice}/events?${qs}`);
    renderEvents(data.events);
    renderEventDots(data.events);
  } catch (e) { /* 下轮再试 */ }
}

/* ── 事件红点图层(摔车/急刹上图) ── */

let eventDots = {};  // event_id -> overlay

function renderEventDots(events) {
  const wanted = {};
  // events 最新在前;红点只画最近 10 条,避免测试期高频事件铺满地图
  for (const e of events.slice(0, 10)) {
    const d = e.detail || {};
    if (!d.lon_bd || !d.lat_bd) continue;
    if (!["fall", "fall_suspect", "hard_brake"].includes(e.type)) continue;
    wanted[e.id] = e;
  }
  // 移除消失的
  for (const id of Object.keys(eventDots)) {
    if (!wanted[id]) { map.removeOverlay(eventDots[id]); delete eventDots[id]; }
  }
  // 添加新增的
  for (const [id, e] of Object.entries(wanted)) {
    if (eventDots[id]) continue;
    const d = e.detail;
    const icon = e.type === "hard_brake" ? BRAKE_ICON : FALL_ICON;
    const m = new BMapGL.Marker(new BMapGL.Point(d.lon_bd, d.lat_bd), { icon });
    m.setTitle(`${EVENT_META[e.type] || e.type} ${e.start_time || ""}`);
    map.addOverlay(m);
    eventDots[id] = m;
  }
}

function updateTelemetry(p) {
  $("#telemetry").style.display = "block";
  $("#teleTime").textContent = p.gps_time + (p.located ? "" : " (未定位)");
  $("#tSpeed").textContent = p.speed;
  $("#tDirection").textContent = p.direction + "°";
  // 设备上报的陀螺/加速度是浮点原始值,尾数很长;显示层统一收敛:
  // 陀螺仪保留 1 位小数,加速度取整(mG)。存储与事件判定仍用原始精度。
  const gyro = (v) => (v == null ? "-" : Math.round(v * 10) / 10);
  const acc = (v) => (v == null ? "-" : Math.round(v));
  $("#tGyroX").textContent = gyro(p.gyro_x);
  $("#tGyroY").textContent = gyro(p.gyro_y);
  $("#tGyroZ").textContent = gyro(p.gyro_z);
  $("#tAccX").textContent = acc(p.acc_x);
  $("#tAccY").textContent = acc(p.acc_y);
  $("#tAccZ").textContent = acc(p.acc_z);
}

/* ── 实时模式:先取近期尾巴,再增量轮询 ── */

async function startLive() {
  stopLive();
  resetTrack();
  if (!currentDevice) return;
  try {
    const latest = await api(`/devices/${currentDevice}/latest`);
    // 回填窗口只取最近 3 分钟:静止时 GPS 室内漂移会积累成满屏乱线,
    // 窗口越短开场越干净;更早的轨迹用"历史轨迹"页查
    const toLocal = (d) => new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 19).replace("T", " ");
    const start = toLocal(new Date(Date.now() - 3 * 60 * 1000));
    const data = await api(`/devices/${currentDevice}/track?start=${encodeURIComponent(start)}`);
    lastPointId = latest.id;  // 增量从当前最新开始,避免重画回填窗口之前的漂移
    appendPoints(data.points, { fit: true });
    // 近期没有有效移动轨迹时,把车标定在最后一次有效定位;从未定位过则不放标(避免画到 (0,0) 海上)
    if (!marker && latest.located) {
      const pos = new BMapGL.Point(latest.lon_bd, latest.lat_bd);
      marker = riderMarker(pos);
      map.centerAndZoom(pos, 16);
    }
    if (!marker && !latest.located) {
      $("#trackStats").textContent = "设备当前未定位(GPS 无效),数据照常接收,定位有效后自动显示";
    }
    updateTelemetry(latest);
  } catch (e) {
    /* 设备还没有数据,等轮询 */
  }
  liveTimer = setInterval(async () => {
    if (!currentDevice || mode !== "live") return;
    try {
      const data = await api(`/devices/${currentDevice}/track?since_id=${lastPointId}`);
      appendPoints(data.points);
      // 轨迹线只画有效移动;但遥测和车标要跟最新上报走(静止/漂移时轨迹不动,数据照样刷新)
      if (!data.points.length) {
        const latest = await api(`/devices/${currentDevice}/latest`);
        updateTelemetry(latest);
        if (latest.located) {
          const pos = new BMapGL.Point(latest.lon_bd, latest.lat_bd);
          if (!marker) {
            marker = riderMarker(pos);
          } else {
            marker.setPosition(pos);
          }
        }
      }
    } catch (e) { /* 网络抖动,下轮再试 */ }
  }, 1000);
  refreshEvents();
  evtTimer = setInterval(() => { if (mode === "live") refreshEvents(); }, 5000);
}

function stopLive() {
  if (liveTimer) { clearInterval(liveTimer); liveTimer = null; }
  if (evtTimer) { clearInterval(evtTimer); evtTimer = null; }
}

/* ── 历史模式 ── */

async function queryHistory() {
  if (!currentDevice) return alert("请先在左侧选择设备");
  const start = $("#histStart").value.replace("T", " ");
  const end = $("#histEnd").value.replace("T", " ");
  if (!start || !end) return alert("请选择开始和结束时间");
  resetTrack();
  const params = new URLSearchParams({ start, end, limit: "50000" });
  const data = await api(`/devices/${currentDevice}/track?${params}`);
  refreshEvents({ start, end });
  if (!data.count) {
    $("#trackStats").textContent = "该时间段没有轨迹数据";
    return;
  }
  appendPoints(data.points, { fit: true });
  const first = data.points[0];
  const startMarker = new BMapGL.Marker(new BMapGL.Point(first.lon_bd, first.lat_bd));
  map.addOverlay(startMarker);
}

/* ── 模式切换 ── */

$("#tabLive").onclick = () => switchMode("live");
$("#tabHistory").onclick = () => switchMode("history");
$("#btnQuery").onclick = queryHistory;

function switchMode(m) {
  mode = m;
  $("#tabLive").classList.toggle("active", m === "live");
  $("#tabHistory").classList.toggle("active", m === "history");
  $("#liveBox").style.display = m === "live" ? "block" : "none";
  $("#historyBox").style.display = m === "history" ? "flex" : "none";
  if (m === "live") {
    startLive();
  } else {
    stopLive();
    resetTrack();
    // 默认查最近一小时
    const now = new Date();
    const ago = new Date(now.getTime() - 3600 * 1000);
    const toLocal = (d) => new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 19);
    $("#histEnd").value = toLocal(now);
    $("#histStart").value = toLocal(ago);
  }
}

/* ── 派单订单图层:店铺/买家标记 + 侧栏卡片(路线规划交给平台地图,不画连线) ── */

const ORDER_COLORS = { PENDING: "#94949e", ACCEPTED: "#f97316", DELIVERING: "#2563eb", DELIVERED: "#16a34a" };
let poiMarkers = {};    // key -> {marker, label}
let orderTimer = null;

function startOrders() {
  refreshOrders();
  orderTimer = setInterval(refreshOrders, 2000);
}

async function refreshOrders() {
  let state;
  try {
    const res = await fetch(`${dispatchApi}/api/state`);
    if (!res.ok) return;
    state = await res.json();
  } catch (e) { return; }

  const active = (state.orders || []).filter((o) => ["PENDING", "ACCEPTED", "DELIVERING"].includes(o.status));

  // 1) 需要展示的 POI:活跃订单涉及的店铺与买家(有经纬度才画)
  const wantedPois = {};
  for (const o of active) {
    for (const p of o.pickups) {
      const shop = (state.shops || []).find((s) => s.id === p.shop_id);
      if (shop && shop.lng) wantedPois[`shop_${shop.id}`] = { lng: shop.lng, lat: shop.lat, name: shop.name, icon: SHOP_ICON };
    }
    const b = (state.buyers || []).find((x) => x.id === o.buyer.id);
    if (b && b.lng) wantedPois[`buyer_${b.id}`] = { lng: b.lng, lat: b.lat, name: b.name, icon: BUYER_ICON };
  }
  for (const key of Object.keys(poiMarkers)) {
    if (!wantedPois[key]) {
      map.removeOverlay(poiMarkers[key].marker);
      if (poiMarkers[key].label) map.removeOverlay(poiMarkers[key].label);
      delete poiMarkers[key];
    }
  }
  for (const [key, poi] of Object.entries(wantedPois)) {
    if (poiMarkers[key]) continue;
    const pos = new BMapGL.Point(poi.lng, poi.lat);
    const m = new BMapGL.Marker(pos, { icon: poi.icon });
    map.addOverlay(m);
    const label = new BMapGL.Label(poi.name, { position: pos, offset: new BMapGL.Size(20, -8) });
    label.setStyle({
      color: "#333", fontSize: "12px", fontWeight: "700", border: "1px solid #d8dae0",
      borderRadius: "6px", padding: "2px 7px", backgroundColor: "rgba(255,255,255,.94)",
    });
    map.addOverlay(label);
    poiMarkers[key] = { marker: m, label };
  }

  renderOrderCards(active, state);
}

function renderOrderCards(active, state) {
  $("#orderSection").style.display = "block";
  $("#orderCount").textContent = active.length ? `${active.length} 单在途` : "无在途";
  const list = $("#orderList");
  if (!active.length) {
    list.innerHTML = '<div class="empty muted">暂无在途订单</div>';
    return;
  }
  list.innerHTML = active
    .map((o) => {
      const shops = o.pickups
        .map((p) => `<span class="pick ${p.status}">${p.shop_name.split("(")[0]}${p.status === "PICKED" ? " ✓" : ""}</span>`)
        .join(" ");
      return `<div class="order-card">
        <div class="order-head">
          <span class="order-id">${o.id}</span>
          <span class="order-status" style="background:${ORDER_COLORS[o.status]}">${o.status_label}</span>
        </div>
        <div class="order-route">${shops} → ${o.buyer.name}</div>
        <div class="order-meta">${o.kind} · ${o.delivery_fee}元 · 时限剩 ${o.deadline_left_minutes}分</div>
      </div>`;
    })
    .join("");
}
