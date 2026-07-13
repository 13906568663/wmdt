/* 车辆轨迹监控前端:百度地图 GL + 轮询式实时跟踪/历史轨迹 */

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

async function api(path) {
  const res = await fetch(`/api${path}`);
  if (!res.ok) throw new Error(`${path} -> ${res.status}`);
  return res.json();
}

/* ── 初始化:拿配置,动态加载百度地图 JS ── */

(async function boot() {
  const cfg = await api("/config");
  $("#connInfo").textContent =
    `JT808 接入: TCP ${cfg.tcp_port}(2013/2019,0xF1 陀螺仪)\nMQTT 接入: TCP ${cfg.mqtt_port}(JSON,按 devId 识别)`;
  window.__initMap = initMap;
  const script = document.createElement("script");
  script.src = `https://api.map.baidu.com/api?type=webgl&v=1.0&ak=${cfg.baidu_ak}&callback=__initMap`;
  document.body.appendChild(script);
})();

function initMap() {
  map = new BMapGL.Map("map");
  map.centerAndZoom(new BMapGL.Point(113.93, 22.53), 13); // 默认深圳
  map.enableScrollWheelZoom(true);
  map.addControl(new BMapGL.ZoomControl());
  map.addControl(new BMapGL.ScaleControl());

  refreshDevices();
  deviceTimer = setInterval(refreshDevices, 5000);
}

/* ── 设备列表 ── */

async function refreshDevices() {
  let data;
  try {
    data = await api("/devices");
  } catch (e) {
    return;
  }
  const devices = data.devices || [];
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
  refreshDevices();
  if (mode === "live") startLive();
};

/* ── 轨迹绘制 ── */

function resetTrack() {
  if (polyline) { map.removeOverlay(polyline); polyline = null; }
  if (marker) { map.removeOverlay(marker); marker = null; }
  trackPath = [];
  lastPointId = 0;
  $("#trackStats").textContent = "";
}

function appendPoints(points, { fit = false } = {}) {
  if (!points.length) return;
  for (const p of points) {
    trackPath.push(new BMapGL.Point(p.lon_bd, p.lat_bd));
    lastPointId = Math.max(lastPointId, p.id);
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

  const last = points[points.length - 1];
  const pos = new BMapGL.Point(last.lon_bd, last.lat_bd);
  if (!marker) {
    marker = new BMapGL.Marker(pos);
    map.addOverlay(marker);
  } else {
    marker.setPosition(pos);
  }
  updateTelemetry(last);

  if (fit && trackPath.length > 1) {
    map.setViewport(trackPath);
  } else if (mode === "live" && $("#followSwitch").checked) {
    map.panTo(pos);
  }
  $("#trackStats").textContent = `轨迹点:${trackPath.length} · 最新:${last.gps_time}`;
}

function updateTelemetry(p) {
  $("#telemetry").style.display = "block";
  $("#teleTime").textContent = p.gps_time + (p.located ? "" : " (未定位)");
  $("#tSpeed").textContent = p.speed;
  $("#tDirection").textContent = p.direction + "°";
  const fmt = (v) => (v == null ? "-" : v);
  $("#tGyroX").textContent = fmt(p.gyro_x);
  $("#tGyroY").textContent = fmt(p.gyro_y);
  $("#tGyroZ").textContent = fmt(p.gyro_z);
  $("#tAccX").textContent = fmt(p.acc_x);
  $("#tAccY").textContent = fmt(p.acc_y);
  $("#tAccZ").textContent = fmt(p.acc_z);
}

/* ── 实时模式:先取近期尾巴,再增量轮询 ── */

async function startLive() {
  stopLive();
  resetTrack();
  if (!currentDevice) return;
  try {
    const latest = await api(`/devices/${currentDevice}/latest`);
    const since = Math.max(0, latest.id - 600); // 先画最近 ~10 分钟
    const data = await api(`/devices/${currentDevice}/track?since_id=${since}`);
    appendPoints(data.points, { fit: true });
    // 近期没有有效移动轨迹时,也要把车标定在最后上报位置
    if (!marker) {
      const pos = new BMapGL.Point(latest.lon_bd, latest.lat_bd);
      marker = new BMapGL.Marker(pos);
      map.addOverlay(marker);
      map.centerAndZoom(pos, 16);
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
        const pos = new BMapGL.Point(latest.lon_bd, latest.lat_bd);
        if (!marker) {
          marker = new BMapGL.Marker(pos);
          map.addOverlay(marker);
        } else if (latest.located) {
          marker.setPosition(pos);
        }
      }
    } catch (e) { /* 网络抖动,下轮再试 */ }
  }, 1000);
}

function stopLive() {
  if (liveTimer) { clearInterval(liveTimer); liveTimer = null; }
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
