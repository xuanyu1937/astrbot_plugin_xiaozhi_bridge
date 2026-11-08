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

async function loadSettings() {
  try {
    const data = await bridge.apiGet("settings");
    const c = data.settings || {};
    $("replyMax").value = c.reply_max_chars ?? 150;
    $("ltSec").value = c.long_task_threshold_sec ?? 5;
    $("ltRemind").value = c.long_task_remind_sec ?? 45;
    $("seedN").value = c.context_seed_messages ?? 60;
    $("seedMax").value = c.context_max_messages ?? 200;
    $("kbEnable").checked = !!c.kb_enable;
  } catch (err) {
    pill("red", "读设置失败：" + ((err && err.message) || err));
  }
}

$("saveSettings").onclick = async () => {
  const patch = {
    reply_max_chars: Number($("replyMax").value),
    long_task_threshold_sec: Number($("ltSec").value),
    long_task_remind_sec: Number($("ltRemind").value),
    context_seed_messages: Number($("seedN").value),
    context_max_messages: Number($("seedMax").value),
    kb_enable: $("kbEnable").checked,
  };
  try {
    await bridge.apiPost("settings/save", patch);
    await load();
    await loadSettings();
    pill("green", "设置已保存");
  } catch (err) {
    pill("red", "保存失败：" + ((err && err.message) || err));
  }
};

$("exportCfg").onclick = async () => {
  try {
    const data = await bridge.apiGet("export");
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "xiaozhi-bridge-config.json";
    a.click();
    URL.revokeObjectURL(a.href);
  } catch (err) {
    alert("导出失败：" + ((err && err.message) || err));
  }
};

$("importCfg").onclick = () => {
  const input = document.createElement("input");
  input.type = "file";
  input.accept = "application/json";
  input.onchange = async () => {
    const file = input.files && input.files[0];
    if (!file) return;
    try {
      const text = await file.text();
      const payload = JSON.parse(text);
      const res = await bridge.apiPost("import", payload);
      alert("已导入 " + (res.keys || 0) + " 项设置，建议重载插件");
      await load();
      await loadSettings();
    } catch (err) {
      alert("导入失败：" + ((err && err.message) || err));
    }
  };
  input.click();
};

load();
loadSettings();
