"use strict";

const names = {index: "IndexTTS", gpt: "GPT-SoVITS"};
const actions = {
  configure: "保存 GPU",
  bootstrap: "启动网关",
  prepare: "准备模型",
  deploy: "发布",
  restart: "热重启",
  rollback: "回滚",
  probe: "测试合成",
  "set-reference": "保存参考音",
};

const node = id => document.getElementById(id);
const set = (id, value) => { node(id).textContent = value; };
const tabs = [...document.querySelectorAll("[data-engine]")];
const controls = [...document.querySelectorAll("[data-action]")];
const pending = new Set();
const referenceSubmit = node("reference-form").querySelector("button");
const loginSubmit = node("login-form").querySelector("button");
const referenceForm = node("reference-form");
const configurationForm = node("configuration-form");
const configurationSubmit = configurationForm.querySelector("button");
const storageForm = node("storage-form");
const storageInput = node("data-root");
const storageSubmit = storageForm.querySelector("button");
let storage = null;
let status = {};
let selected = "index";
let credential = "";
let polling = false;
let pollError = "";
let actionError = "";
let referenceError = "";
let configurationError = "";
let keyFeedbackTimer;

async function api(path, key, options = {}) {
  const response = await fetch(path, {
    method: options.method || "GET",
    body: options.body,
    headers: {
      "Authorization": `Bearer ${key}`,
      ...(options.body && {"Content-Type": "application/json"}),
    },
    cache: "no-store",
  });
  const result = await response.json();
  if (!response.ok) {
    const problem = new Error(typeof result.detail === "string" ? result.detail : "暂时无法完成请求。");
    problem.status = response.status;
    throw problem;
  }
  return result;
}

function render() {
  const info = status[selected] || {};
  const configured = Boolean(info.configured);
  const hasReference = Boolean(info.reference_configured);
  const gatewayOnline = Boolean(info.gateway_online || info.online);
  const job = info.job || {};
  const busy = pending.has(selected) || job.state === "running";
  for (const tab of tabs) {
    const engine = tab.dataset.engine;
    const indicator = tab.querySelector(".signal");
    const state = status[engine];
    tab.setAttribute("aria-pressed", String(engine === selected));
    const text = state?.status_unavailable ? "状态未知"
      : state?.online ? state.resident === true ? "模型已加载"
        : state.resident === false ? "网关在线 · 模型休眠" : "网关在线"
      : state?.gateway_online ? "网关在线 · 等待发布"
        : state?.configured ? "网关离线" : state ? "未配置" : "读取中";
    indicator.textContent = text;
    indicator.dataset.online = String(state?.status_unavailable ? "unknown" : (state?.online || state?.gateway_online) ?? "unknown");
    indicator.dataset.resident = String(state?.online ? state.resident ?? "unknown" : "unknown");
  }
  set("engine-name", names[selected]);
  node("copy-key").hidden = !configured;
  const revision = typeof info.revision === "string" ? info.revision : "";
  node("revision").title = revision;
  set("revision", revision.slice(0, 12) || "—");
  set("slot", info.active_slot || "—");
  set("sessions", Number.isInteger(info.sessions) ? String(info.sessions) : "—");
  set("gpu", info.gpu || "—");
  node("reference-state").dataset.online = hasReference ? "true" : "unknown";
  set("reference-state", hasReference ? "已设置" : "未设置");

  const needsConfiguration = info.configured === false && !info.status_unavailable;
  configurationForm.hidden = !needsConfiguration;
  configurationSubmit.disabled = busy;
  for (const button of controls) {
    const action = button.dataset.action;
    button.disabled = busy || !configured
      || (action === "bootstrap" && gatewayOnline)
      || (action === "prepare" && info.resident)
      || (!["bootstrap", "prepare"].includes(action) && !hasReference)
      || (["restart", "rollback"].includes(action) && !info.active_slot)
      || (action === "deploy" && !gatewayOnline)
      || (action === "probe" && !info.online);
  }
  referenceSubmit.disabled = busy || !configured;
  set("engine-hint", actionError || pollError || (info.status_unavailable ? "无法读取主机状态，请刷新。"
    : configured && !gatewayOnline ? "启动网关后即可发布。"
    : configured && !hasReference ? "设置部署参考音后即可发布。" : ""));
  node("engine-hint").classList.toggle("error", Boolean(actionError || pollError));
  set("reference-error", referenceError);
  set("configuration-error", configurationError);

  const failure = job.action === "configure" ? "保存失败，请检查 GPU"
    : job.action === "prepare" ? "准备失败，请检查下载环境和可用空间"
    : job.action === "bootstrap" ? "网关启动失败，请检查端口占用"
    : job.action === "rollback" ? "回滚失败，请检查上一个发布版本和参考音"
    : "验证失败，请检查模型和部署参考音";
  const jobText = job.state === "running" ? "正在执行"
    : job.state === "done" ? "已完成" : job.state === "failed" ? failure : "";
  node("job-state").closest(".job").dataset.state = job.state || "";
  const text = jobText ? `${actions[job.action]} · ${jobText}` : "暂无操作";
  // Updating identical text would repeat this message for screen-reader users.
  if (node("job-state").textContent !== text) set("job-state", text);
}

function updateStorage(next) {
  if (!next.editable || !storageInput.value || storageInput.value === storage?.data_root) {
    storageInput.value = next.data_root;
  }
  storage = next;
  storageInput.readOnly = !next.editable;
  storageSubmit.hidden = !next.editable;
}

function readConsole(key) {
  return Promise.all([api("/api/status", key), api("/api/storage", key)]);
}

function update(list) {
  pollError = "";
  status = Object.fromEntries(list.map(info => [info.engine, info]));
  render();
}

async function refresh() {
  const key = credential;
  if (!key || polling) return;
  polling = true;
  node("refresh").disabled = true;
  try {
    const [engines, layout] = await readConsole(key);
    if (credential === key) {
      updateStorage(layout);
      update(engines);
    }
  } catch (error) {
    if (credential !== key) return;
    if (error.status === 401) disconnect("请重新输入管理密钥。");
    else {
      pollError = "暂时无法读取状态，请刷新。";
      render();
    }
  } finally {
    polling = false;
    node("refresh").disabled = false;
  }
}

function disconnect(message = "") {
  credential = "";
  status = {};
  storage = null;
  storageForm.reset();
  set("storage-feedback", "");
  pollError = "";
  actionError = "";
  referenceError = "";
  configurationError = "";
  set("key-feedback", "");
  clearTimeout(keyFeedbackTimer);
  configurationForm.reset();
  configurationForm.hidden = true;
  node("gpu-devices").replaceChildren();
  node("logout").hidden = true;
  node("workspace").hidden = true;
  node("login").hidden = false;
  referenceForm.reset();
  set("login-error", message);
  node("key").focus();
}

node("login-form").addEventListener("submit", async event => {
  event.preventDefault();
  const key = node("key").value.trim();
  if (!key) return;
  loginSubmit.disabled = true;
  set("login-error", "");
  try {
    const [engines, layout] = await readConsole(key);
    credential = key;
    node("key").value = "";
    node("login").hidden = true;
    node("workspace").hidden = false;
    node("logout").hidden = false;
    updateStorage(layout);
    update(engines);
    void loadGpus(key);
    document.querySelector(`[data-engine="${selected}"]`).focus();
  } catch (error) {
    set("login-error", error.status === 401 ? "管理密钥有误。" : "无法连接管理台。");
    node("key").focus();
  } finally {
    loginSubmit.disabled = false;
  }
});

async function loadGpus(key) {
  try {
    const devices = await api("/api/gpus", key);
    if (key !== credential) return;
    node("gpu-devices").replaceChildren(...devices.map(gpu => {
      const option = document.createElement("option");
      option.value = gpu.uuid;
      option.label = `GPU ${gpu.index} · ${gpu.name}`;
      return option;
    }));
  } catch {
    // Manual UUID entry remains available if the host cannot list GPUs.
  }
}

node("audio").addEventListener("change", () => set("audio-name", node("audio").files[0]?.name || "未选择文件"));
referenceForm.addEventListener("reset", () => set("audio-name", "未选择文件"));
node("logout").addEventListener("click", () => disconnect());
node("refresh").addEventListener("click", () => void refresh());
node("copy-key").addEventListener("click", async () => {
  const engine = selected;
  const key = credential;
  const button = node("copy-key");
  button.disabled = true;
  clearTimeout(keyFeedbackTimer);
  set("key-feedback", "");
  try {
    if (!navigator.clipboard) throw new Error("clipboard unavailable");
    const retrieve = () => api(`/api/${engine}/api-key`, key, {method: "POST"});
    if (typeof ClipboardItem === "function" && navigator.clipboard.write) {
      const content = retrieve().then(result => {
        if (credential !== key) throw new Error("session ended");
        return new Blob([result.key], {type: "text/plain"});
      });
      await navigator.clipboard.write([new ClipboardItem({"text/plain": content})]);
    } else {
      const result = await retrieve();
      if (credential !== key) return;
      await navigator.clipboard.writeText(result.key);
    }
    if (credential === key && selected === engine) set("key-feedback", "API 密钥已复制。");
  } catch {
    if (credential === key && selected === engine) set("key-feedback", "未能复制密钥，请检查浏览器的剪贴板权限。");
  } finally {
    button.disabled = false;
    keyFeedbackTimer = setTimeout(() => set("key-feedback", ""), 4500);
  }
});

setInterval(() => { if (!document.hidden) void refresh(); }, 4000);
document.addEventListener("visibilitychange", () => { if (!document.hidden) void refresh(); });

for (const tab of tabs) {
  tab.addEventListener("click", () => {
    if (selected === tab.dataset.engine) return;
    selected = tab.dataset.engine;
    actionError = "";
    referenceError = "";
    configurationError = "";
    set("key-feedback", "");
    clearTimeout(keyFeedbackTimer);
    configurationForm.reset();
    configurationForm.hidden = true;
    referenceForm.reset();
    render();
  });
}

async function run(engine, path, body) {
  const key = credential;
  const job = await api(path, key, {method: "POST", body});
  if (key === credential) status[engine] = {...status[engine], job};
}

for (const button of controls) {
  button.addEventListener("click", async () => {
    const engine = selected;
    const action = button.dataset.action;
    if (action === "rollback" && !window.confirm(`将 ${names[engine]} 切回上一个发布版本？`)) return;
    pending.add(engine);
    actionError = "";
    render();
    try {
      await run(engine, `/api/${engine}/actions/${action}`);
    } catch (error) {
      if (error.status === 401) disconnect("请重新输入管理密钥。");
      else if (engine === selected && credential) actionError = error.status ? error.message : "未收到操作确认，请刷新状态。";
    } finally {
      pending.delete(engine);
      render();
    }
  });
}

function encode(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(",")[1]);
    reader.onerror = reject;
    reader.readAsDataURL(file);
  });
}

referenceForm.addEventListener("submit", async event => {
  event.preventDefault();
  const engine = selected;
  const key = credential;
  const file = node("audio").files[0];
  const prompt = node("prompt").value.trim();
  const language = node("language").value;
  referenceError = "";
  if (!file || file.size > 10 * 1024 * 1024 || !prompt) {
    referenceError = "选一段不超过 10 MiB 的 WAV，并填写录音台词。";
    render();
    return;
  }
  pending.add(engine);
  render();
  try {
    const audio = await encode(file);
    if (key !== credential) return;
    await run(engine, `/api/${engine}/reference`, JSON.stringify({audio, prompt, language}));
    if (key === credential && engine === selected) referenceForm.reset();
  } catch (error) {
    if (error.status === 401) disconnect("请重新输入管理密钥。");
    else if (engine === selected && key === credential) referenceError = error.status ? error.message : "参考音上传失败，请重试。";
  } finally {
    pending.delete(engine);
    render();
  }
});

configurationForm.addEventListener("submit", async event => {
  event.preventDefault();
  const engine = selected;
  const key = credential;
  pending.add(engine);
  configurationError = "";
  render();
  try {
    await run(engine, `/api/${engine}/configuration`, JSON.stringify({
      gpu: node("gpu-choice").value.trim(),
    }));
  } catch (error) {
    if (error.status === 401 && key === credential) disconnect("请重新输入管理密钥。");
    else if (engine === selected && key === credential) configurationError = error.status
      ? error.message : "未收到配置确认，请刷新状态。";
  } finally {
    pending.delete(engine);
    render();
  }
});


storageForm.addEventListener("submit", async event => {
  event.preventDefault();
  const key = credential;
  if (!key || !storage?.editable) return;
  storageSubmit.disabled = true;
  set("storage-feedback", "");
  node("storage-feedback").classList.remove("error");
  try {
    const layout = await api("/api/storage", key, {
      method: "POST", body: JSON.stringify({data_root: storageInput.value.trim()}),
    });
    if (credential !== key) return;
    storageInput.value = layout.data_root;
    updateStorage(layout);
    set("storage-feedback", "已保存");
  } catch (error) {
    if (credential !== key) return;
    if (error.status === 401) disconnect("请重新输入管理密钥。");
    else {
      node("storage-feedback").classList.add("error");
      set("storage-feedback", error.status ? error.message : "保存失败，请重试。");
      void refresh();
    }
  } finally {
    storageSubmit.disabled = false;
  }
});
