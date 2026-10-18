"use strict";

const $ = (id) => document.getElementById(id);
const els = {
  form: $("form"), url: $("url"), format: $("format"), filename: $("filename"),
  submit: $("submit"), status: $("status"), message: $("message"), bar: $("bar"),
  result: $("result"), download: $("download"), expiry: $("expiry"), info: $("info"),
};

let timer = null;

function load(key) {
  try { return localStorage.getItem(key); } catch (_) { return null; }
}
function save(key, value) {
  try { localStorage.setItem(key, value); } catch (_) {}
}

async function init() {
  try {
    const cfg = await (await fetch("api/config")).json();
    for (const f of cfg.formats) {
      const opt = document.createElement("option");
      opt.value = f.key;
      opt.textContent = f.label;
      els.format.appendChild(opt);
    }
    const saved = load("format");
    if (saved && cfg.formats.some((f) => f.key === saved)) els.format.value = saved;
    els.info.textContent =
      `yt-dlp ${cfg.ytdlp_version} / ${cfg.max_duration_minutes}分までの動画に対応 / ` +
      `変換後 ${cfg.ttl_minutes}分でファイルを削除します。`;
  } catch (_) {
    els.info.textContent = "サーバーに接続できません。";
  }
}

function showMessage(text, isError = false) {
  els.status.hidden = false;
  els.message.textContent = text;
  els.message.classList.toggle("error", isError);
}

async function poll(id) {
  let job;
  try {
    const res = await fetch(`api/jobs/${id}`);
    if (!res.ok) throw new Error();
    job = await res.json();
  } catch (_) {
    showMessage("サーバーとの通信に失敗しました。", true);
    return finish();
  }

  els.bar.value = job.progress || 0;
  if (job.status === "done") {
    showMessage(job.title ? `完了: ${job.title}` : "完了しました。");
    els.download.href = `api/jobs/${id}/file`;
    els.download.setAttribute("download", job.filename || "");
    els.expiry.textContent = job.expires_in != null ? `あと約${Math.ceil(job.expires_in / 60)}分で削除されます` : "";
    els.result.hidden = false;
    return finish();
  }
  if (job.status === "error" || job.status === "expired") {
    showMessage(job.message, true);
    return finish();
  }
  showMessage(job.message);
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
  els.bar.value = 0;
  save("format", els.format.value);
  showMessage("ジョブを送信しています…");

  try {
    const res = await fetch("api/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        url: els.url.value.trim(),
        format: els.format.value,
        filename: els.filename.value.trim() || null,
      }),
    });
    const data = await res.json();
    if (!res.ok) {
      showMessage(typeof data.detail === "string" ? data.detail : "入力内容を確認してください。", true);
      return finish();
    }
    poll(data.id);
  } catch (_) {
    showMessage("サーバーに接続できません。", true);
    finish();
  }
});

init();
