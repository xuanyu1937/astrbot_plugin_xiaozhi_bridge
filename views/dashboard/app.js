// 视图脚本：走注入的 window.AstrBotPluginView 桥接，内部一律用相对路径。
const bridge = window.AstrBotPluginView;
const $ = (id) => document.getElementById(id);

function pill(level, text) {
  const el = $("pill");
  el.className = "pill " + (level || "gray");
  el.textContent = text || "未知";
}

function renderCheck(items) {
  const box = $("checkList");
  box.innerHTML = "";
  (items || []).forEach((it) => {
    const li = document.createElement("li");
    li.textContent = (it.ok ? "✅ " : "❌ ") + it.name + "：" + (it.msg || "");
    if (!it.ok && it.action) {
      const tip = document.createElement("div");
      tip.className = "act";
      tip.textContent = "下一步：" + it.action;
      li.appendChild(tip);
    }
    box.appendChild(li);
  });
}

function renderInfo(data) {
  const box = $("info");
  box.innerHTML = "";
  const rows = [
    ["发送人身份", data.sender || "未设置"],
    ["推送目标", data.target || "未设置"],
    ["会话标识", data.session || "-"],
    ["召唤词", (data.trigger_words || []).join("、") || "-"],
    ["今日计数", String(data.count ?? 0)],
    ["运行时长", Math.floor((data.uptime || 0) / 60) + " 分钟"],
    ["最近错误", data.last_error || "无"],
  ];
  rows.forEach(([k, v]) => {
    const dt = document.createElement("dt");
    dt.textContent = k;
    const dd = document.createElement("dd");
    dd.textContent = v;
    box.append(dt, dd);
  });
}

async function load() {
  try {
    await (bridge && bridge.ready ? bridge.ready() : Promise.resolve());
    if (bridge && bridge.getContext) {
      const ctx = bridge.getContext() || {};
      document.documentElement.dataset.theme = ctx.isDark ? "dark" : "light";
    }
    const data = await bridge.apiGet("status");
    pill(data.level, data.connected ? "已连接" : "未连接");
    renderCheck(data.diag);
    $("pushToggle").checked = !!data.push;
    $("triggerToggle").checked = (data.mode || "idle") === "persona";
    $("quiet").value = data.quiet_hours || "";
    renderInfo(data);
  } catch (err) {
    pill("red", "读取失败");
    $("checkList").innerHTML = "<li>❌ " + ((err && err.message) || err) + "</li>";
  }
}

async function toggle(name, enabled) {
  try {
    await bridge.apiPost("toggle", { name: name, enabled: enabled });
    await load();
  } catch (err) {
    pill("red", "切换失败：" + ((err && err.message) || err));
  }
}

$("pushToggle").onchange = (e) => toggle("push", e.target.checked);
$("triggerToggle").onchange = (e) => toggle("trigger", e.target.checked);

$("saveQuiet").onclick = async () => {
  try {
    await bridge.apiPost("quiet", { quiet_hours: $("quiet").value.trim() });
    await load();
  } catch (err) {
    pill("red", "保存失败：" + ((err && err.message) || err));
  }
};

$("reconnect").onclick = async () => {
  try {
    await bridge.apiPost("reconnect", {});
    await load();
  } catch (err) {
    pill("red", "重连失败：" + ((err && err.message) || err));
  }
};

$("cleanup").onclick = async () => {
  try {
    const res = await bridge.apiPost("cleanup", {});
    alert("已清理 " + (res.removed || 0) + " 个文件，释放 " + (res.freed_mb || 0) + " MB");
    await load();
  } catch (err) {
    alert("清理失败：" + ((err && err.message) || err));
  }
};

load();
