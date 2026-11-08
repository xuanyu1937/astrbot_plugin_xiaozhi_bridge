// 视图脚本：全部走注入的 window.AstrBotPluginView，内部用相对路径。
const bridge = window.AstrBotPluginView;
const $ = (id) => document.getElementById(id);

function pill(level, text) {
  const el = $("pill");
  el.className = "pill " + (level || "gray");
  el.textContent = text || "未知";
}

function fmtDuration(sec) {
  const s = Math.max(0, Math.floor(sec || 0));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  return h ? h + " 小时 " + m + " 分" : m ? m + " 分钟" : s + " 秒";
}

function renderConn(data) {
  const box = $("conn");
  box.innerHTML = "";
  const rows = [
    ["接入点", data.token_masked || "未填"],
    ["连接状态", data.connected ? "在线" : "离线"],
    ["连接时间", data.connected_at ? new Date(data.connected_at * 1000).toLocaleTimeString() : "-"],
    ["当前模式", (data.mode || "idle") === "persona" ? "人格接管中" : "交给小智"],
    ["今日问答", String(data.count ?? 0)],
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
    ["静音时段", data.quiet_hours || "未设置"],
    ["运行时长", fmtDuration(data.uptime)],
  ];
  rows.forEach(([k, v]) => {
    const dt = document.createElement("dt");
    dt.textContent = k;
    const dd = document.createElement("dd");
    dd.textContent = v;
    box.append(dt, dd);
  });
}

async function loadStatus() {
  try {
    await (bridge && bridge.ready ? bridge.ready() : Promise.resolve());
    if (bridge && bridge.getContext) {
      const ctx = bridge.getContext() || {};
      document.documentElement.dataset.theme = ctx.isDark ? "dark" : "light";
    }
    const data = await bridge.apiGet("status");
    pill(data.level, data.connected ? "已连接" : "未连接");
    renderConn(data);
    renderCheck(data.diag);
    renderInfo(data);
    $("pushToggle").checked = !!data.push;
    $("triggerToggle").checked = (data.mode || "idle") === "persona";
    $("quiet").value = data.quiet_hours || "";
  } catch (err) {
    pill("red", "读取失败");
    $("checkList").innerHTML = "<li>❌ " + ((err && err.message) || err) + "</li>";
  }
}

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
    $("withQuestion").checked = !!c.push_with_question;
    $("mirrorEnable").checked = !!c.mirror_enable;
  } catch (err) {
    pill("red", "读设置失败：" + ((err && err.message) || err));
  }
}

async function saveSettings(patch, tipOk) {
  try {
    await bridge.apiPost("settings/save", patch);
    await loadStatus();
    await loadSettings();
    pill("green", tipOk || "已保存");
  } catch (err) {
    pill("red", "保存失败：" + ((err && err.message) || err));
  }
}

$("refresh").onclick = () => { loadStatus(); loadSettings(); };
$("reconnect").onclick = async () => {
  try {
    await bridge.apiPost("reconnect", {});
    pill("yellow", "重连中…");
    setTimeout(loadStatus, 3000);
  } catch (err) {
    pill("red", "重连失败：" + ((err && err.message) || err));
  }
};

$("testpush").onclick = async () => {
  try {
    const res = await bridge.apiPost("testpush", {});
    pill(res.ok ? "green" : "red", res.ok ? "测试推送已发送" : "推送失败");
  } catch (err) {
    pill("red", "推送失败：" + ((err && err.message) || err));
  }
};

$("pushToggle").onchange = (e) => {
  bridge.apiPost("toggle", { name: "push", enabled: e.target.checked }).then(loadStatus);
};
$("triggerToggle").onchange = (e) => {
  bridge.apiPost("toggle", { name: "trigger", enabled: e.target.checked }).then(loadStatus);
};
$("withQuestion").onchange = (e) => saveSettings({ push_with_question: e.target.checked });
$("mirrorEnable").onchange = (e) => saveSettings({ mirror_enable: e.target.checked });
$("kbEnable").onchange = (e) => saveSettings({ kb_enable: e.target.checked });

$("saveQuiet").onclick = async () => {
  try {
    await bridge.apiPost("quiet", { quiet_hours: $("quiet").value.trim() });
    await loadStatus();
    pill("green", "静音时段已保存");
  } catch (err) {
    pill("red", "保存失败：" + ((err && err.message) || err));
  }
};

$("saveSettings").onclick = () => saveSettings({
  reply_max_chars: Number($("replyMax").value),
  long_task_threshold_sec: Number($("ltSec").value),
  long_task_remind_sec: Number($("ltRemind").value),
  context_seed_messages: Number($("seedN").value),
  context_max_messages: Number($("seedMax").value),
});

$("cleanup").onclick = async () => {
  try {
    const res = await bridge.apiPost("cleanup", {});
    alert("已清理 " + (res.removed || 0) + " 个文件，释放 " + (res.freed_mb || 0) + " MB");
    await loadStatus();
  } catch (err) {
    alert("清理失败：" + ((err && err.message) || err));
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
    pill("green", "已导出（不含密钥）");
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
      const payload = JSON.parse(await file.text());
      const res = await bridge.apiPost("import", payload);
      alert("已导入 " + (res.keys || 0) + " 项设置");
      await loadStatus();
      await loadSettings();
    } catch (err) {
      alert("导入失败：" + ((err && err.message) || err));
    }
  };
  input.click();
};

loadStatus();
loadSettings();
setInterval(loadStatus, 15000);
