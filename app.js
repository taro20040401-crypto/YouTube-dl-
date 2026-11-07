"use strict";

const $ = (id) => document.getElementById(id);
const els = {
  form: $("form"), url: $("url"), paste: $("paste"), filename: $("filename"), submit: $("submit"),
  audio: $("audio"), video: $("video"), version: $("version"), note: $("note"),
  status: $("status"), message: $("message"), meter: $("meter"), fill: $("fill"), pct: $("pct"),
  hint: $("hint"), detail: $("detail"), detailbox: $("detailbox"), result: $("result"), rfile: $("rfile"), rexp: $("rexp"),
  download: $("download"), history: $("history"), hlist: $("hlist"), clear: $("clear"),
};
const AUDIO_KEYS = ["m4a", "mp3", "opus", "wav"];
let timer = null;

/* ---------- storage ---------- */
function load(key) { try { return localStorage.getItem(key); } catch (_) { return null; } }
function save(key, value) { try { localStorage.setItem(key, value); } catch (_) {} }
function getHistory() {
  try { return JSON.parse(load("history") || "[]"); } catch (_) { return []; }
}
function setHistory(list) { save("history", JSON.stringify(list.slice(0, 10))); }

/* ---------- 起動 ---------- */
async function init() {
  try {
    const cfg = await (await fetch("api/config")).json();
    const saved = load("format") || "m4a";
    for (const f of cfg.formats) {
      const label = (f.label.match(/\((.*)\)/) || [, f.label])[1];
      const wrap = document.createElement("label");
      wrap.className = "opt";
      const input = document.createElement("input");
      input.type = "radio";
      input.name = "format";
      input.value = f.key;
      input.checked = f.key === saved;
      const text = document.createElement("span");
      text.textContent = label;
      wrap.append(input, text);
      (AUDIO_KEYS.includes(f.key) ? els.audio : els.video).appendChild(wrap);
    }
    if (!document.querySelector('input[name="format"]:checked')) {
      document.querySelector('input[name="format"]').checked = true;
    }
    els.version.textContent = `yt-dlp ${cfg.ytdlp_version}`;
    els.note.textContent =
      `権利を持つ動画、または保存が許可された動画だけに使ってください。変換後${cfg.ttl_minutes}分でファイルを削除します。` +
      `${cfg.max_duration_minutes}分を超える動画は変換できません。`;
  } catch (_) {
    els.version.textContent = "サーバーに接続できません";
  }
  refreshHistory();
}

/* ---------- 表示 ---------- */
function setStatus({ text, error = false, wait = false, pct = null }) {
  els.status.hidden = false;
  els.message.textContent = text;
  els.message.classList.toggle("error", error);
  els.meter.hidden = error;
  els.pct.hidden = error;
  els.meter.classList.toggle("wait", wait);
  els.fill.style.width = pct == null ? "" : `${pct}%`;
  els.pct.textContent = pct == null || wait ? "" : `${Math.floor(pct)}%`;
}

function showError(message, detail) {
  setStatus({ text: "変換できませんでした", error: true });
  els.hint.hidden = false;
  els.hint.textContent = message;
  els.detailbox.hidden = !detail;
  els.detailbox.open = false;
  els.detail.textContent = detail || "";
}

function minutesText(sec) { return `あと約${Math.max(1, Math.ceil(sec / 60))}分で削除`; }

/* ---------- 変換ジョブ ---------- */
async function poll(id) {
  let job;
  try {
    const res = await fetch(`api/jobs/${id}`);
    if (!res.ok) throw new Error();
    job = await res.json();
  } catch (_) {
    showError("サーバーと通信できません。");
    return finish();
  }

  if (job.status === "done") {
    setStatus({ text: "変換しました", pct: 100 });
    els.rfile.textContent = job.filename || job.title || "";
    els.rexp.textContent = job.expires_in != null ? minutesText(job.expires_in) : "";
    els.download.href = `api/jobs/${id}/file`;
    els.download.setAttribute("download", job.filename || "");
    els.result.hidden = false;
    const list = getHistory().filter((h) => h.id !== id);
    list.unshift({ id, name: job.filename || job.title || "ファイル" });
    setHistory(list);
    refreshHistory();
    return finish();
  }
  if (job.status === "error" || job.status === "expired") {
    showError(job.message, job.detail);
    return finish();
  }
  const waiting = job.status === "queued" || !job.progress;
  setStatus({ text: job.message, wait: waiting, pct: waiting ? null : job.progress });
  timer = setTimeout(() => poll(id), 1000);
}

function finish() {
  clearTimeout(timer);
  els.submit.disabled = false;
}

els.form.addEventListener("submit", async (e) => {
  e.preventDefault();
  clearTimeout(timer);
  els.submit.disabled = true;
  els.result.hidden = true;
  els.hint.hidden = true;
  els.detailbox.hidden = true;
  const format = (document.querySelector('input[name="format"]:checked') || {}).value || "m4a";
  save("format", format);
  setStatus({ text: "送信しています", wait: true });
  try {
    const res = await fetch("api/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url: els.url.value.trim(), format, filename: els.filename.value.trim() || null }),
    });
    const data = await res.json();
    if (!res.ok) {
      showError(typeof data.detail === "string" ? data.detail : "入力内容を確認してください。");
      return finish();
    }
    poll(data.id);
  } catch (_) {
    showError("サーバーに接続できません。");
    finish();
  }
});

/* ---------- 貼り付け ---------- */
els.paste.addEventListener("click", async () => {
  try {
    const text = await navigator.clipboard.readText();
    if (text) els.url.value = text.trim();
  } catch (_) { /* 許可されなかった場合は手入力 */ }
  els.url.focus();
});

/* ---------- 履歴 ---------- */
async function refreshHistory() {
  const list = getHistory();
  els.history.hidden = list.length === 0;
  els.hlist.textContent = "";
  await Promise.all(list.map(async (h) => {
    try {
      const res = await fetch(`api/jobs/${h.id}`);
      const job = res.ok ? await res.json() : null;
      h.alive = !!job && job.status === "done";
      h.left = job ? job.expires_in : null;
    } catch (_) { h.alive = null; }
  }));
  for (const h of list) {
    const li = document.createElement("li");
    const name = document.createElement("span");
    name.className = "t";
    name.textContent = h.name;
    const state = document.createElement("span");
    state.className = "s";
    if (h.alive) {
      const a = document.createElement("a");
      a.href = `api/jobs/${h.id}/file`;
      a.textContent = "保存";
      a.setAttribute("download", h.name);
      state.append(h.left != null ? `${minutesText(h.left)}　` : "", a);
    } else {
      li.className = "gone";
      state.textContent = h.alive === null ? "確認できません" : "期限切れ";
    }
    li.append(name, state);
    els.hlist.appendChild(li);
  }
}

els.clear.addEventListener("click", () => { setHistory([]); refreshHistory(); });

init();
