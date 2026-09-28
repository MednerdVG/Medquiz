// Tiny helpers shared by every page.
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => Array.from(el.querySelectorAll(s));
function esc(v) { return v == null ? "" : String(v).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c])); }
function toast(msg, ms = 2600) {
  const t = document.createElement("div"); t.className = "toast"; t.textContent = msg;
  document.body.appendChild(t); setTimeout(() => t.remove(), ms);
}
async function api(method, url, body, opts = {}) {
  const init = { method, headers: {}, credentials: "same-origin" };
  if (body instanceof FormData) init.body = body;
  else if (body !== undefined) { init.headers["Content-Type"] = "application/json"; init.body = JSON.stringify(body); }
  const r = await fetch(url, init);
  const ct = r.headers.get("content-type") || "";
  const data = ct.includes("json") ? await r.json() : await r.text();
  if (!r.ok && !opts.raw) {
    const d = data && data.detail;
    const msg = typeof d === "string" ? d : (d && d.message) || (Array.isArray(d) ? d.map(x => x.msg || x).join("; ") : `Error ${r.status}`);
    const err = new Error(msg); err.status = r.status; err.detail = d; throw err;
  }
  return opts.raw ? { ok: r.ok, status: r.status, data } : data;
}
function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }
function fmtWhen(iso) {
  const d = new Date(iso);
  return d.toLocaleString("en-IN", { weekday: "short", day: "numeric", month: "short", hour: "numeric", minute: "2-digit" });
}
