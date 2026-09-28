// Consultation workspace: left = patient context, right = the consultation form.
// The form edits the §10 prescription JSON (D) directly; every change autosaves.
let W = null;          // workspace payload
let D = null;          // draft prescription JSON
let TPL = { diet: [], exercise: [], advice: [], charts: [] };
let TPLBODY = {};      // cache "kind/id" -> template body
let INVLIB = [];
let locked = false;

// ---------------------------------------------------------------- path helpers
function get(path, obj = D) { return path.split(".").reduce((a, k) => (a == null ? a : a[k]), obj); }
function set(path, value, obj = D) {
  const ks = path.split("."); let o = obj;
  ks.slice(0, -1).forEach((k, i) => { if (o[k] == null) o[k] = /^\d+$/.test(ks[i + 1]) ? [] : {}; o = o[k]; });
  o[ks[ks.length - 1]] = value;
}
function arr(path) { let a = get(path); if (!Array.isArray(a)) { a = []; set(path, a); } return a; }
const clean = v => (v === "" ? null : v);
const lines = t => (t || "").split("\n").map(s => s.trim()).filter(Boolean);

// ---------------------------------------------------------------- save
const save = debounce(async () => {
  if (locked) return;
  $("#savestate").textContent = "Saving…";
  try {
    const r = await api("PUT", `/api/consultations/${CID}/draft`, prune(D));
    $("#savestate").textContent = "Saved " + new Date().toLocaleTimeString("en-IN", { hour: "numeric", minute: "2-digit" });
    W.issues = r.issues; W.suggestions = r.suggestions; drawIssues(); drawAdviceChips();
  } catch (e) { $("#savestate").textContent = "Not saved: " + e.message; }
}, 700);
function changed() { save(); }

// drop empty rows / empty optional objects so validation reflects what will print
function prune(d) {
  const x = JSON.parse(JSON.stringify(d));
  const nonEmpty = (o, keys) => keys.some(k => o[k] != null && String(o[k]).trim() !== "");
  x.complaints = (x.complaints || []).filter(r => nonEmpty(r, ["text"]));
  x.diagnoses = (x.diagnoses || []).filter(r => nonEmpty(r, ["text"]));
  x.labs = (x.labs || []).filter(r => nonEmpty(r, ["test", "value"])).map(r => ({ ...r, value: r.value || "", test: r.test || "" }));
  x.investigations = (x.investigations || []).filter(r => nonEmpty(r, ["name"]));
  x.referrals = (x.referrals || []).filter(r => nonEmpty(r, ["to"]));
  x.contacts = (x.contacts || []).filter(r => nonEmpty(r, ["label"]));
  x.taper_plans = (x.taper_plans || []).filter(r => nonEmpty(r, ["medicine", "instruction"]));
  x.vitals.strip = (x.vitals.strip || []).filter(r => nonEmpty(r, ["label"])).map(r => ({ ...r, value: r.value || "" }));
  x.patient.extra_rows = (x.patient.extra_rows || []).filter(r => nonEmpty(r, ["label"])).map(r => ({ ...r, value: r.value || "" }));
  x.taper_plans = x.taper_plans.map(r => ({ medicine: r.medicine || "", instruction: r.instruction || "" }));
  if (x.plain_terms && x.plain_terms.title == null) delete x.plain_terms.title;
  if (x.free_text && x.free_text.section_title == null) x.free_text.section_title = "";
  for (const k of ["stopped", "started", "continued", "sos", "administered", "nutrition"])
    x.medications[k] = (x.medications[k] || []).filter(r => nonEmpty(r, ["name"]));
  if (x.plain_terms && !(x.plain_terms.paragraphs || []).length) x.plain_terms = null;
  if (x.treatment_options && !(x.treatment_options.rows || []).length) x.treatment_options = null;
  if (x.free_text && !(x.free_text.body_html || "").trim()) x.free_text = null;
  if (x.follow_up && !x.follow_up.interval && !(x.follow_up.bring || []).length && !(x.follow_up.report_sooner_if || []).length) x.follow_up = null;
  return x;
}

// ---------------------------------------------------------------- rendering helpers
function sec(n, title, inner, id) { return `<div class="sec" id="${id || ""}"><h3>${n ? `<span class="n">${n}</span>` : ""}${esc(title)}</h3>${inner}</div>`; }
function inp(path, ph, cls = "", type = "text", list = "") {
  const v = get(path); return `<input data-p="${path}" placeholder="${esc(ph)}" class="${cls}" type="${type}" value="${esc(v ?? "")}" ${list ? `list="${list}"` : ""}>`;
}
function area(path, ph, asLines = false) {
  const v = get(path); const text = asLines ? (v || []).join("\n") : (v || "");
  return `<textarea data-p="${path}" ${asLines ? 'data-lines="1"' : ""} placeholder="${esc(ph)}">${esc(text)}</textarea>`;
}
function selectEl(path, opts, blank = true) {
  const v = get(path) ?? "";
  return `<select data-p="${path}">${blank ? '<option value=""></option>' : ""}${opts.map(([k, l]) => `<option value="${esc(k)}" ${k === v ? "selected" : ""}>${esc(l)}</option>`).join("")}</select>`;
}
function rows(path, fields, addLabel) {
  const list = arr(path);
  const html = list.map((row, i) => `<div class="r">${fields.map(f => f.type === "check"
    ? `<label style="text-transform:none;font-size:13px;color:var(--ink);display:flex;gap:4px;align-items:center"><input type="checkbox" data-p="${path}.${i}.${f.k}" ${row[f.k] ? "checked" : ""}>${esc(f.ph)}</label>`
    : inp(`${path}.${i}.${f.k}`, f.ph, f.cls || "", f.type || "text", f.list || "")).join("")}
    <button type="button" class="btn sm x" data-del="${path}" data-i="${i}">✕</button></div>`).join("");
  return `<div class="rows" data-rows="${path}">${html}</div><button type="button" class="btn sm" data-add="${path}" data-fields='${JSON.stringify(fields.map(f => f.k))}'>+ ${esc(addLabel)}</button>`;
}

// ---------------------------------------------------------------- the form (ordered the way the doctor thinks)
function drawForm() {
  const docs = Object.entries(W.doctors).map(([k, d]) => [k, d.name]);
  D.vitals = D.vitals || { strip: [] }; D.medications = D.medications || {};
  let n = 0; const N = () => ++n;
  const medFields = [{ k: "name", ph: "Brand (e.g. Tab. Jardiance 10)", cls: "w2 medname", list: "dl-med" }, { k: "generic", ph: "Generic" },
    { k: "dose", ph: "Dose 1–0–1", cls: "w05" }, { k: "timing", ph: "Timing" }, { k: "duration", ph: "Duration", cls: "w05" }, { k: "note", ph: "Note" }];
  const html = [
    sec(N(), "Visit", `<div class="fields">
      <div><label>Visit type</label>${selectEl("meta.consult_type", [["in_clinic", "First visit — in clinic"], ["teleconsult", "Teleconsult"], ["follow_up", "Follow-up"]], false)}</div>
      <div><label>Seen by (required)</label>${selectEl("meta.seen_by", docs)}</div>
      <div><label>Co-signatory</label>${selectEl("meta.co_signatory", docs)}</div>
      <div><label>Date</label>${inp("meta.consult_date", "", "", "date")}</div>
      <div><label>Visit no.</label>${inp("meta.visit_number", "", "", "number")}</div>
      <div><label>Layout</label>${selectEl("meta.section_order_preset", [["standard", "Standard"], ["medication_last", "Medication last"], ["two_page_clinical", "Two-page clinical"]], false)}</div>
    </div>`),
    sec(0, "Patient details", `<div class="fields">
      <div><label>Name</label>${inp("patient.name", "")}</div><div><label>Age</label>${inp("patient.age_years", "", "", "number")}</div>
      <div><label>Sex</label>${selectEl("patient.sex", [["F", "Female"], ["M", "Male"], ["O", "Other"]])}</div>
      <div><label>Patient ID</label>${inp("patient.patient_id", "")}</div><div><label>Diet type</label>${inp("patient.diet_type", "vegetarian / mixed / Jain")}</div></div>
      <div class="fields" style="margin-top:8px"><div><label>Allergies (one per line)</label>${area("patient.allergies", "nil known", true)}</div>
      <div><label>Known conditions</label>${area("patient.known_conditions", "", true)}</div><div><label>Family history</label>${area("patient.family_history", "", true)}</div>
      <div><label>Recorded intolerances</label>${area("patient.intolerances", "e.g. statin — myalgia", true)}</div></div>
      <div class="sub">Extra rows</div>${rows("patient.extra_rows", [{ k: "label", ph: "Label (Resident of, Status, Reports…)" }, { k: "value", ph: "Value", cls: "w2" }], "Row")}`),
    sec(N(), "Vitals strip", `<div class="fields"><div><label>Weight kg</label>${inp("vitals.weight_kg", "", "", "number")}</div>
      <div><label>Height cm</label>${inp("vitals.height_cm", "", "", "number")}</div><div><label>Waist cm</label>${inp("vitals.waist_cm", "", "", "number")}</div>
      <div><label>BMI (auto)</label><div id="bmi" class="badge grey">—</div></div></div>
      <div class="sub">Printed strip — 4 to 7 columns</div>
      ${rows("vitals.strip", [{ k: "label", ph: "LABEL" }, { k: "value", ph: "Value" }, { k: "sub", ph: "Sub-label" }, { k: "earlier", ph: "Earlier value" }], "Column")}
      <div class="chips" style="margin-top:6px">${["WEIGHT", "HEIGHT", "WAIST", "BP", "PULSE", "SpO2", "HbA1c", "FASTING SUGAR", "RBS", "eGFR", "TSH", "TARGET WEIGHT"]
        .map(l => `<span class="chip addv" data-l="${l}">+ ${l}</span>`).join("")}</div>`),
    sec(N(), "Complaints / interval review", `${rows("complaints", [{ k: "text", ph: "Complaint", cls: "w2" }, { k: "pointer", ph: "Pointer (optional)" }], "Complaint")}
      <div class="sub">Interval review (follow-up) — one point per line</div>${area("interval_review", "Sugars still above target on the starting dose…", true)}
      ${W.consultation.previous_consultation_id ? '<button type="button" class="btn sm" id="bChange">Insert change summary from last visit</button>' : ""}`),
    sec(N(), "Diagnosis", rows("diagnoses", [{ k: "text", ph: "Diagnosis", cls: "w2 dx", list: "dl-dx" }, { k: "detail", ph: "Detail (optional)" }], "Diagnosis")),
    sec(N(), "Labs / reports reviewed", `${rows("labs", [{ k: "test", ph: "Test" }, { k: "value", ph: "Value" }, { k: "earlier", ph: "Earlier", cls: "w05" },
      { k: "remark", ph: "Remark" }, { k: "source", ph: "Source", cls: "w05" }, { k: "date", ph: "Date", cls: "w05" }, { k: "highlight", ph: "bold", type: "check" }], "Lab")}
      ${W.consultation.previous_consultation_id ? '<button type="button" class="btn sm" id="bPull">Pull last values for comparison</button>' : ""}
      <div style="margin-top:6px"><label>Footnote (relevant normals)</label>${inp("labs_footnote", "Relevant normals: CBC, LFT…", "", "text")}</div>`),
    sec(N(), "Plain terms (optional)", `<div class="fields"><div><label>Title</label>${inp("plain_terms.title", "WHAT THIS MEANS IN PLAIN TERMS")}</div></div>
      <label>Paragraphs — one per line</label>${area("plain_terms.paragraphs", "1–3 short paragraphs", true)}`),
    sec(N(), "Treatment options (optional)", `<div><label>Intro</label>${inp("treatment_options.intro", "")}</div>
      <div><label>Columns (comma separated)</label><input id="toCols" value="${esc((get("treatment_options.columns") || []).join(", "))}" placeholder="OPTION 1 — TABLET, OPTION 2 — INJECTION" style="width:100%"></div>
      <label>Rows — "Label | value 1 | value 2" per line</label><textarea id="toRows">${esc((get("treatment_options.rows") || []).map(r => [r.label, ...r.values].join(" | ")).join("\n"))}</textarea>
      <div><label>Recommendation</label>${inp("treatment_options.recommendation", "")}</div>`),
    sec(N(), "Investigations advised", `${rows("investigations", [{ k: "name", ph: "Test", cls: "w2" }, { k: "instruction", ph: "Instruction" }, { k: "purpose", ph: "Purpose" }, { k: "timing", ph: "When (now / at 3 months)", cls: "w05" }], "Investigation")}
      <div class="sub">Library</div><div class="chips">${INVLIB.map((x, i) => `<span class="chip addinv" data-i="${i}">+ ${esc(x.name)}</span>`).join("")}</div>`),
    sec(N(), "Medication (Rx)", ["started", "continued", "sos", "nutrition"].map(k =>
      `<div class="sub">${{ started: "Started / changed", continued: "Continue as before", sos: "SOS — only if needed", nutrition: "Nutrition" }[k]}</div>${rows("medications." + k, medFields, "Medicine")}`).join("") +
      `<div class="sub">Stopped</div>${rows("medications.stopped", [{ k: "name", ph: "Medicine", cls: "w2 medname", list: "dl-med" }, { k: "reason", ph: "Reason" }], "Stopped medicine")}
      <div class="sub">Given at the clinic</div>${rows("medications.administered", [{ k: "name", ph: "Inj. …", cls: "w2 medname", list: "dl-med" }, { k: "dose", ph: "Dose" }, { k: "route", ph: "Route" }, { k: "note", ph: "Note" }], "Administered")}
      <div class="sub">Taper plans</div>${rows("taper_plans", [{ k: "medicine", ph: "Medicine" }, { k: "instruction", ph: "Instruction", cls: "w2" }], "Taper")}
      <div class="sub">Footnotes — one per line</div>${area("medications.footnotes", "Gaps, interactions, drowsiness, expected effects", true)}`),
    sec(N(), "Diet", planEditor("diet")),
    sec(N(), "Exercise", planEditor("exercise")),
    sec(N(), "Advice blocks and charts", `<div class="sub">Advice</div><div class="chips" id="advChips"></div><div class="sub">Charts</div><div class="chips" id="chartChips"></div>`),
    sec(N(), "Referral", rows("referrals", [{ k: "to", ph: "To (Psychiatry…)" }, { k: "reason", ph: "Why" }, { k: "named_doctor", ph: "Named doctor" }, { k: "contact", ph: "Contact number" }], "Referral")),
    sec(N(), "Follow-up", `<div class="fields"><div><label>Interval</label>${inp("follow_up.interval", "3 months")}</div></div>
      <div class="fields"><div><label>What to bring — one per line</label>${area("follow_up.bring", "", true)}</div>
      <div><label>Report sooner if — one per line</label>${area("follow_up.report_sooner_if", "", true)}</div></div>
      <div class="sub">Contact lines</div>${rows("contacts", [{ k: "label", ph: "For any queries" }, { k: "name", ph: "Name" }, { k: "number", ph: "Number" }], "Contact")}`),
    sec(N(), "Certificates (optional)", certEditor()),
    sec(0, "Free-text section (optional)", `<div class="fields"><div><label>Section title</label>${inp("free_text.section_title", "")}</div></div>
      <label>Body (simple HTML allowed: p, b, i, ul, li, table)</label>${area("free_text.body_html", "")}
      <label style="text-transform:none;font-size:13px;color:var(--ink);margin-top:8px"><input type="checkbox" data-p="meta.medicine_anchors" ${D.meta.medicine_anchors !== false ? "checked" : ""}> Print medicine timing anchors in the diet plan</label>`),
  ];
  $("#rx").innerHTML = html.join("");
  drawAdviceChips(); updateBmi(); loadPlanBodies();
}

// ---------------------------------------------------------------- diet / exercise inline editors
function planEditor(kind) {
  const key = kind === "diet" ? "diet_plan" : "exercise_plan";
  const ref = D[key];
  const opts = TPL[kind].map(t => [t.id, `${t.title} (v${t.version})`]);
  return `<div class="fields"><div><label>Template</label><select data-plan="${kind}"><option value="">— none —</option>
    ${opts.map(([k, l]) => `<option value="${k}" ${ref && ref.template === k ? "selected" : ""}>${esc(l)}</option>`).join("")}</select></div>
    ${ref ? `<div><label>Printed title</label>${inp(key + ".title_override", "(template title)")}</div>` : ""}</div>
    <div id="plan-${kind}"></div>`;
}
async function loadPlanBodies() {
  for (const kind of ["diet", "exercise"]) {
    const ref = D[kind === "diet" ? "diet_plan" : "exercise_plan"];
    const box = $("#plan-" + kind); if (!box) continue;
    if (!ref) { box.innerHTML = ""; continue; }
    const k = `${kind}/${ref.template}`;
    if (!TPLBODY[k]) TPLBODY[k] = await api("GET", `/api/templates/${kind}/${ref.template}`);
    box.innerHTML = kind === "diet" ? dietBody(TPLBODY[k], ref.overrides || {}) : exerciseBody(TPLBODY[k], ref.overrides || {});
  }
}
function ov(ref, path, fallback) { const v = get(path, ref.overrides || {}); return v === undefined ? fallback : v; }
function dietBody(t, o) {
  const ref = { overrides: o };
  const slots = (t.slots || []).map(s => `<div class="r"><b style="flex:0 0 130px;font-size:13px">${esc(s.time)}</b>
    <textarea data-ov="diet" data-ovp="slots.${s.key}.options" data-lines="1" style="flex:1">${esc((ov(ref, `slots.${s.key}.options`, s.options) || []).join("\n"))}</textarea></div>`).join("");
  const tg = ov(ref, "target", t.target || {});
  return `<div class="small muted">Edit inline — options one per line. Changes are stored as overrides of template v${esc(t.version)}.</div>
    <div class="fields">${Object.entries({ kcal: "Energy", protein: "Protein", salt: "Salt", other: "Other" }).map(([k, l]) =>
      `<div><label>${l}</label><input data-ov="diet" data-ovp="target.${k}" value="${esc(tg[k] || "")}"></div>`).join("")}</div>
    <div class="rows">${slots}</div>
    <div class="fields"><div><label>Include — one per line</label><textarea data-ov="diet" data-ovp="include_avoid.include" data-lines="1">${esc((ov(ref, "include_avoid.include", (t.include_avoid || {}).include) || []).join("\n"))}</textarea></div>
    <div><label>Avoid or limit</label><textarea data-ov="diet" data-ovp="include_avoid.avoid" data-lines="1">${esc((ov(ref, "include_avoid.avoid", (t.include_avoid || {}).avoid) || []).join("\n"))}</textarea></div></div>
    <label>Closing bullets</label><textarea data-ov="diet" data-ovp="closing_bullets" data-lines="1">${esc((ov(ref, "closing_bullets", t.closing_bullets) || []).join("\n"))}</textarea>`;
}
function exerciseBody(t, o) {
  const ref = { overrides: o };
  return `<div class="small muted">Weekly plan and precautions can be edited inline (template v${esc(t.version)}).</div>
    <label>Weekly plan — "Day | exercise | duration" per line</label>
    <textarea data-ov="exercise" data-ovp="weekly" data-table="day,activity,duration">${esc((ov(ref, "weekly", t.weekly) || []).map(w => [w.day, w.activity, w.duration || ""].join(" | ")).join("\n"))}</textarea>
    <label>Precautions — one per line</label><textarea data-ov="exercise" data-ovp="precautions" data-lines="1">${esc((ov(ref, "precautions", t.precautions) || []).join("\n"))}</textarea>`;
}

// ---------------------------------------------------------------- certificates
const CERTS = { treatment_certificate: ["Treatment certificate", [["treatment_period", "Treatment period"], ["fitness", "Fitness statement"]]],
  fitness_certificate: ["Fitness certificate", [["fit_for", "Fit to (work / travel / gym)"], ["restriction", "Restriction"]]],
  medical_leave_certificate: ["Medical leave", [["rest_from", "Rest from"], ["rest_to", "Rest to"], ["resume_on", "Resume on"]]],
  insurance_summary_letter: ["Insurance summary letter (2 pages)", []] };
function certList() { return (D.certificates || []).map(c => typeof c === "string" ? { type: c, fields: {} } : c); }
function certEditor() {
  const list = certList();
  return Object.entries(CERTS).map(([k, [label, fields]]) => {
    const c = list.find(x => x.type === k);
    return `<div style="margin-bottom:8px"><label style="text-transform:none;font-size:14px;color:var(--ink)"><input type="checkbox" data-cert="${k}" ${c ? "checked" : ""}> ${label}</label>
      ${c && fields.length ? `<div class="fields">${fields.map(([f, l]) => `<div><label>${l}</label><input data-certf="${k}.${f}" value="${esc((c.fields || {})[f] || "")}"></div>`).join("")}</div>` : ""}</div>`;
  }).join("") + '<div class="small muted">Certificates print with a blank signature space and a stamp box — signed and stamped by hand.</div>';
}

// ---------------------------------------------------------------- advice chips (suggestions highlighted, never auto-inserted)
function drawAdviceChips() {
  const sug = new Set((W.suggestions || []).map(s => s.template));
  const on = new Set(D.advice_blocks || []), onc = new Set(D.charts || []);
  if ($("#advChips")) $("#advChips").innerHTML = TPL.advice.map(t => `<span class="chip ${on.has(t.id) ? "on" : ""} ${!on.has(t.id) && sug.has(t.id) ? "sug" : ""}" data-adv="${t.id}"
    title="${esc((W.suggestions || []).find(s => s.template === t.id)?.reason || "")}">${esc(t.title)}</span>`).join("");
  if ($("#chartChips")) $("#chartChips").innerHTML = TPL.charts.map(t => `<span class="chip ${onc.has(t.id) ? "on" : ""}" data-chart="${t.id}">${esc(t.title)}</span>`).join("");
  $("#suggest").innerHTML = (W.suggestions || []).length ? `<div class="issue info"><b>Suggested:</b>&nbsp;${W.suggestions.map(s =>
    `<a href="#" data-sugadd="${s.template}">${esc(s.template.replace(/_/g, " "))}</a> <span class="muted">(${esc(s.reason)})</span>`).join(" · ")}</div>` : "";
}

// ---------------------------------------------------------------- issues
function drawIssues() {
  const iss = W.issues || [];
  const warn = iss.filter(i => i.level === "warn");
  $("#issues").innerHTML = iss.map(i => `<div class="issue ${i.level}">
    ${i.level === "warn" ? `<input type="checkbox" class="ack" value="${esc(i.code)}" title="Acknowledge" ${ACK.has(i.code) ? "checked" : ""}>` : `<b>${i.level === "block" ? "Blocking" : "Note"}</b>`}
    <span>${esc(i.message)}</span></div>`).join("") + (W.schema_error ? `<div class="issue block">${esc(W.schema_error)}</div>` : "");
  const blocking = iss.some(i => i.level === "block");
  $("#bApprove").disabled = locked || blocking;
  $("#bApprove").title = blocking ? "Fix the blocking problems first" : (warn.length ? "Tick each warning to acknowledge it" : "");
}
const ACK = new Set();

// ---------------------------------------------------------------- left pane
function drawLeft() {
  const p = W.patient, it = (W.intake || {}).data || {}, b = W.booking;
  const kv = (k, v) => v ? `<b>${esc(k)}</b><span>${esc(v)}</span>` : "";
  const meds = (it.current_medicines || []).filter(m => m.name).map(m => [m.name, m.dose, m.timing].filter(Boolean).join(" ")).join("; ");
  const vit = it.vitals || {};
  $("#left").innerHTML = `<div class="lp">
    <div class="row"><h1 style="margin:0;font-size:20px">${esc(p.name)}</h1><span class="badge">${esc(p.code)}</span></div>
    <div class="small muted">${[p.age_years && p.age_years + " y", p.sex, p.city, p.phone].filter(Boolean).map(esc).join(" · ")}</div>
    ${b ? `<div class="row" style="margin-top:8px">${b.meet_link ? `<a class="btn primary sm" href="${esc(b.meet_link)}" target="_blank" rel="noopener">Open Google Meet</a>` : '<span class="badge warn">No Meet link</span>'}
      <span class="small muted">${fmtWhen(b.slot_start)}</span></div>${b.notes ? `<div class="small" style="margin-top:6px">Booking note: ${esc(b.notes)}</div>` : ""}` : ""}
    <h2>Intake history ${W.intake.consent_given_at ? '<span class="badge">consent ✓</span>' : '<span class="badge warn">no consent yet</span>'}</h2>
    <div class="kv">${kv("Complaints", it.complaints)}${kv("Duration", it.duration)}${kv("Conditions", it.known_conditions)}${kv("Medicines", meds)}
      ${kv("Allergies", it.allergies)}${kv("Surgeries", it.surgeries)}${kv("Family", it.family_history)}${kv("Diet", it.diet_type)}
      ${kv("Smoking", it.smoking)}${kv("Alcohol", it.alcohol)}${kv("Occupation", it.occupation)}${kv("Sleep", it.sleep_hours && it.sleep_hours + " h")}
      ${kv("Weight", vit.weight_kg && vit.weight_kg + " kg")}${kv("Height", vit.height_cm && vit.height_cm + " cm")}${kv("Waist", vit.waist_cm && vit.waist_cm + " cm")}
      ${kv("BP", vit.bp)}${kv("Pulse", vit.pulse)}${kv("Fasting sugar", vit.fasting_sugar)}</div>
    ${Object.keys(it).length ? "" : '<div class="empty">The patient has not filled the intake form yet.</div>'}
    <h2>Reports (${W.uploads.length})</h2>
    ${W.uploads.map(u => `<div class="thumb ${u.extracted_patient_name && !nameOk(u.extracted_patient_name) ? "bad" : ""}">
      <input type="checkbox" class="vsel" value="${u.id}"><span class="badge grey">${esc(u.tag_label)}</span>
      <a href="#" data-view="${u.id}" style="flex:1;word-break:break-all">${esc(u.filename)}</a><span class="small muted">${esc(u.report_date || "")}</span>
      ${u.extracted_patient_name && !nameOk(u.extracted_patient_name) ? `<span class="small">Name on report: ${esc(u.extracted_patient_name)}</span>` : ""}
      <button class="btn sm" data-detach="${u.id}" data-state="${u.detached ? 0 : 1}">${u.detached ? "Re-attach" : "Detach"}</button></div>`).join("") || '<div class="empty">No uploads.</div>'}
    <div class="row"><button class="btn sm" id="vopen">View selected side by side</button>
      <label class="btn sm" style="text-transform:none;margin:0">Add report<input type="file" id="staffup" hidden accept="image/*,application/pdf,.heic"></label></div>
    <h2>Previous prescriptions</h2>
    ${W.history.map(h => `<div class="thumb"><a href="${h.url}" target="_blank" style="flex:1">${esc(h.file_name)}</a><span class="small muted">v${h.version} · ${fmtWhen(h.approved_at)}</span></div>`).join("") || '<div class="empty">None.</div>'}
  </div>`;
}
function nameOk(n) {
  const norm = s => s.toLowerCase().replace(/^(mr|mrs|ms|miss|dr|master|baby)\.?\s+/, "").replace(/[^a-z ]/g, "").trim().split(/\s+/);
  const a = norm(n), b = norm(W.patient.name); return a.every(x => b.includes(x)) || b.every(x => a.includes(x));
}

// ---------------------------------------------------------------- report viewer (zoom, rotate, side-by-side)
let vstate = { rot: 0, zoom: 1 };
function openViewer(ids) {
  const ups = W.uploads.filter(u => ids.includes(u.id)); if (!ups.length) return;
  vstate = { rot: 0, zoom: 1 };
  $("#vbody").innerHTML = ups.map(u => `<div class="pane">${u.content_type === "application/pdf" ? `<iframe src="${u.url}"></iframe>`
    : u.content_type.startsWith("image/heic") ? `<a href="${u.url}" target="_blank">Open HEIC photo</a>` : `<img src="${u.url}" alt="">`}</div>`).join("");
  $("#vtitle").textContent = ups.map(u => u.filename).join("  |  "); $("#viewer").showModal();
}
function vapply() { $$("#vbody img").forEach(i => i.style.transform = `rotate(${vstate.rot}deg) scale(${vstate.zoom})`); }

// ---------------------------------------------------------------- events
document.addEventListener("input", e => {
  const el = e.target;
  if (el.dataset.p) {
    let v = el.type === "checkbox" ? el.checked : el.value;
    if (el.dataset.lines) v = lines(v);
    else if (el.type === "number") v = el.value === "" ? null : Number(el.value);
    else v = clean(v);
    set(el.dataset.p, v);
    if (el.dataset.p.startsWith("vitals.")) updateBmi();
    changed();
  } else if (el.dataset.ov) {
    const kind = el.dataset.ov, key = kind === "diet" ? "diet_plan" : "exercise_plan";
    D[key].overrides = D[key].overrides || {};
    let v = el.value;
    if (el.dataset.lines) v = lines(v);
    else if (el.dataset.table) { const cols = el.dataset.table.split(","); v = lines(v).map(l => Object.fromEntries(l.split("|").map((x, i) => [cols[i], x.trim()]))); }
    set(el.dataset.ovp, v, D[key].overrides); changed();
  } else if (el.id === "toCols" || el.id === "toRows") {
    D.treatment_options = D.treatment_options || {};
    D.treatment_options.columns = $("#toCols").value.split(",").map(s => s.trim()).filter(Boolean);
    D.treatment_options.rows = lines($("#toRows").value).map(l => { const [label, ...values] = l.split("|").map(s => s.trim()); return { label, values }; });
    changed();
  } else if (el.dataset.certf) {
    const [type, f] = el.dataset.certf.split("."); const list = certList(); const c = list.find(x => x.type === type);
    c.fields[f] = el.value || undefined; D.certificates = list; changed();
  }
});
document.addEventListener("change", async e => {
  const el = e.target;
  if (el.dataset.plan !== undefined) {
    const key = el.dataset.plan === "diet" ? "diet_plan" : "exercise_plan";
    D[key] = el.value ? { template: el.value, title_override: null, overrides: {} } : null;
    changed(); drawForm();
  } else if (el.dataset.cert) {
    let list = certList();
    if (el.checked) list.push({ type: el.dataset.cert, fields: {} }); else list = list.filter(x => x.type !== el.dataset.cert);
    D.certificates = list; changed(); drawForm();
  } else if (el.classList.contains("ack")) {
    el.checked ? ACK.add(el.value) : ACK.delete(el.value);
  } else if (el.classList.contains("medname")) {
    // brand chosen from the formulary → generic auto-fills
    const hit = MEDHITS.find(h => el.value.toLowerCase().includes(h.brand.toLowerCase()));
    const gpath = el.dataset.p.replace(/\.name$/, ".generic");
    if (hit && !get(gpath) && $(`[data-p="${gpath}"]`)) { set(gpath, hit.generic); $(`[data-p="${gpath}"]`).value = hit.generic; changed(); }
  } else if (el.id === "staffup" && el.files[0]) {
    const fd = new FormData(); fd.append("file", el.files[0]); fd.append("tag", "other");
    try { W = await api("POST", `/api/consultations/${CID}/uploads`, fd); drawLeft(); } catch (err) { toast(err.message); }
  }
});
let MEDHITS = [];
document.addEventListener("keyup", debounce(async e => {
  const el = e.target;
  if (el.classList && el.classList.contains("medname") && el.value.length >= 2) {
    const q = el.value.replace(/^(tab|cap|inj|syp)\.?\s*/i, "");
    MEDHITS = await api("GET", "/api/formulary/search?q=" + encodeURIComponent(q.split(" ")[0]));
    const form = el.value.match(/^(tab|cap|inj|syp)\.?\s*/i)?.[0] || "";
    $("#dl-med").innerHTML = MEDHITS.flatMap(h => (h.strengths.length ? h.strengths : [""]).map(s =>
      `<option value="${esc((form || "") + h.brand + (s ? " " + s : ""))}">${esc(h.generic)}</option>`)).join("");
  }
  if (el.classList && el.classList.contains("dx") && el.value.length >= 2) {
    const hits = await api("GET", "/api/diagnoses/search?q=" + encodeURIComponent(el.value));
    $("#dl-dx").innerHTML = hits.map(h => `<option value="${esc(h)}">`).join("");
  }
}, 250));
document.addEventListener("click", async e => {
  const el = e.target;
  try {
    if (el.dataset.add) {
      const keys = JSON.parse(el.dataset.fields); arr(el.dataset.add).push(Object.fromEntries(keys.map(k => [k, null]))); drawForm();
    } else if (el.dataset.del) {
      arr(el.dataset.del).splice(+el.dataset.i, 1); changed(); drawForm();
    } else if (el.classList.contains("addv")) {
      arr("vitals.strip").push({ label: el.dataset.l, value: "" }); drawForm();
    } else if (el.classList.contains("addinv")) {
      const x = INVLIB[+el.dataset.i]; arr("investigations").push({ name: x.name, instruction: x.instruction || null }); changed(); drawForm();
    } else if (el.dataset.adv || el.dataset.sugadd) {
      e.preventDefault(); const id = el.dataset.adv || el.dataset.sugadd; const list = D.advice_blocks = D.advice_blocks || [];
      const i = list.indexOf(id); if (i >= 0 && el.dataset.adv) list.splice(i, 1); else if (i < 0) list.push(id);
      changed(); drawAdviceChips();
    } else if (el.dataset.chart) {
      const list = D.charts = D.charts || []; const i = list.indexOf(el.dataset.chart); i >= 0 ? list.splice(i, 1) : list.push(el.dataset.chart);
      changed(); drawAdviceChips();
    } else if (el.id === "bChange") {
      await flushSave(); W = await api("POST", `/api/consultations/${CID}/change-summary/insert`); D = W.draft; drawForm(); toast("Change summary added to the interval review");
    } else if (el.id === "bPull") {
      const r = await api("POST", `/api/consultations/${CID}/pull-labs`); arr("labs").push(...r.labs); drawForm(); toast(`${r.labs.length} rows pulled — fill in today's values`);
    } else if (el.dataset.view) { e.preventDefault(); openViewer([el.dataset.view]); }
    else if (el.id === "vopen") { openViewer($$(".vsel").filter(c => c.checked).map(c => c.value)); }
    else if (el.id === "vrot") { vstate.rot = (vstate.rot + 90) % 360; vapply(); }
    else if (el.id === "vzin") { vstate.zoom *= 1.25; vapply(); }
    else if (el.id === "vzout") { vstate.zoom /= 1.25; vapply(); }
    else if (el.id === "vside") { $("#vbody").style.flexDirection = $("#vbody").style.flexDirection === "column" ? "row" : "column"; }
    else if (el.dataset.detach) {
      await api("POST", `/api/uploads/${el.dataset.detach}/detach`, { detached: el.dataset.state === "1" }); await reload();
    }
  } catch (err) { toast(err.message); }
});
async function flushSave() { if (!locked) await api("PUT", `/api/consultations/${CID}/draft`, prune(D)); }
function updateBmi() {
  const w = D.vitals.weight_kg, h = D.vitals.height_cm;
  if (!$("#bmi")) return;
  if (!w || !h) { $("#bmi").textContent = "needs weight and height"; return; }
  const b = Math.round((w / ((h / 100) ** 2)) * 10) / 10;
  const cls = b >= 35 ? "Obesity III" : b >= 30 ? "Obesity II" : b >= 25 ? "Obesity I" : b >= 23 ? "Overweight" : b >= 18.5 ? "Normal" : "Underweight";
  $("#bmi").textContent = `${b.toFixed(1)} — ${cls}`;
}

$("#bPreview").onclick = async () => {
  try {
    await flushSave();
    const r = await fetch(`/api/consultations/${CID}/preview.pdf`, { credentials: "same-origin" });
    if (!r.ok) { const j = await r.json(); toast(j.detail || "Preview failed", 6000); return; }
    const blob = await r.blob(); $("#pvframe").src = URL.createObjectURL(blob); $("#pvinfo").textContent = "Not yet approved"; $("#preview").showModal();
  } catch (err) { toast(err.message); }
};
$("#bApprove").onclick = async () => {
  await flushSave();
  const warns = (W.issues || []).filter(i => i.level === "warn").map(i => i.code);
  const missing = warns.filter(c => !ACK.has(c));
  if (missing.length) { toast("Tick each warning to acknowledge it first", 4000); return; }
  if (!confirm("Approve this prescription? It will be locked, sent to the patient and archived.")) return;
  $("#bApprove").disabled = true;
  try {
    const r = await api("POST", `/api/consultations/${CID}/approve`, { acknowledged: [...ACK] });
    toast(r.status === "approved" ? `Approved · ${r.pages} pages · sent` : "Signed — waiting for the co-signatory", 5000);
    await reload();
    if (r.url) window.open(r.url, "_blank");
  } catch (err) {
    if (err.detail && err.detail.issues) { W.issues = err.detail.issues; drawIssues(); }
    toast(err.message, 6000); $("#bApprove").disabled = false;
  }
};
$("#bAmend").onclick = async () => {
  const note = prompt("Amendment note (printed on version 2+):"); if (!note) return;
  try { W = await api("POST", `/api/consultations/${CID}/amend`, { note }); D = W.draft; setLock(); drawForm(); drawIssues(); toast("Amendment draft opened"); }
  catch (err) { toast(err.message); }
};

function setLock() {
  locked = W.consultation.status === "approved" || ME.role === "admin";
  document.body.classList.toggle("locked", locked);
  $("#status").textContent = W.consultation.status.replace("_", " ") + (D.meta.version > 1 ? ` · v${D.meta.version}` : "");
  $("#bAmend").hidden = W.consultation.status !== "approved" || ME.role === "admin";
  $("#bApprove").hidden = ME.role === "admin" || W.consultation.status === "approved";
  $("#title").innerHTML = `<b>${esc(W.patient.name)}</b> <span class="muted small">${esc(D.meta.consult_date)} · seen by ${esc((W.doctors[D.meta.seen_by] || {}).name || "—")}</span>`;
}
async function reload() { W = await api("GET", `/api/consultations/${CID}`); D = W.draft; setLock(); drawLeft(); drawForm(); drawIssues(); }
(async () => {
  const [d, ex, adv, ch, inv] = await Promise.all(["diet", "exercise", "advice", "charts"].map(k => api("GET", "/api/templates/" + k))
    .concat([api("GET", "/api/investigations/library")]));
  TPL = { diet: d, exercise: ex, advice: adv, charts: ch }; INVLIB = inv;
  await reload();
})();
