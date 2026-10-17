// ClinicScribe UI. Polls the API, renders the job list, detail view and metrics.
// API selection: ?api=mock or ?api=http forces one; otherwise it uses the real
// backend when GET /jobs answers, and falls back to the in-browser mock.

(async function () {
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const fmtTime = (s) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
  const fmtSecs = (s) => (s == null ? "–" : s < 60 ? `${s.toFixed(1)} s` : `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`);

  const STAGES = ["queued", "diarizing", "transcribing", "summarising", "done"];
  const STAGE_LABEL = { queued: "Queued", diarizing: "Diarization", transcribing: "ASR", summarising: "Lightning agent", done: "Done" };

  // Accept both result shapes: the agent's output (docs/agent_contract.md) and the
  // original CLAUDE.md contract. Everything below renders the normalised shape.
  const SEVERITY_RANK = { critical: 0, warning: 1, info: 2 };
  function normalizeResult(r, job) {
    const s = r.soap || {};
    const transcript = r.transcript
      || job.turns.filter((t) => t.done).map((t) => ({ speaker: t.speaker, start: t.start, end: t.end, text: t.text }));
    const prescriptions = r.prescriptions || [];
    const drug_alerts = r.drug_alerts
      || prescriptions.filter((p) => p.status && p.status !== "ok")
        .map((p) => ({ drug: p.drug, issue: (p.notes || []).join("; ") || p.status }));
    const flags = (r.flags || []).map((f) => ({
      issue: f.issue ?? f.message ?? "",
      fix: f.fix || "",
      severity: f.severity || "warning",
      source: f.source || "",
      verified: f.verified ?? true,
    })).sort((a, b) => (SEVERITY_RANK[a.severity] ?? 1) - (SEVERITY_RANK[b.severity] ?? 1));
    const tool_calls = (r.tool_calls || r.meta?.tool_calls || []).map((c) => ({
      name: c.name,
      args: c.args ?? c.arguments ?? {},
      result: "result" in c ? c.result : c.ok === false ? "failed" : "ok",
    }));
    const timings = { ...(r.timings || {}) };
    if (timings.agent_s == null && r.meta?.latency_ms != null) timings.agent_s = r.meta.latency_ms / 1000;
    return {
      job_id: r.job_id ?? job.job_id,
      transcript,
      soap: { S: s.S ?? s.subjective ?? "", O: s.O ?? s.objective ?? "", A: s.A ?? s.assessment ?? "", P: s.P ?? s.plan ?? "" },
      diagnoses: s.diagnoses || [],
      prescriptions,
      mc: r.mc || null,
      panel: r.panel && typeof r.panel === "object" ? r.panel : null,
      drug_alerts,
      flags,
      verification: r.verification || null,
      tool_calls,
      timings,
    };
  }
  const isAlert = (f) => f.verified && f.severity !== "info";

  async function pickApi() {
    const forced = new URLSearchParams(location.search).get("api");
    if (forced === "mock") return new ClinicApi.MockApi();
    if (forced === "http") return new ClinicApi.HttpApi();
    if (location.protocol.startsWith("http")) {
      try {
        const res = await fetch("/jobs", { headers: { Accept: "application/json" } });
        if (res.ok && Array.isArray(await res.json())) return new ClinicApi.HttpApi();
      } catch (_) { /* no backend */ }
    }
    return new ClinicApi.MockApi();
  }

  const api = await pickApi();
  const POLL_MS = api.kind === "mock" ? 250 : 1000;
  $("modeBadge").textContent = api.kind === "mock" ? "MOCK DATA" : "LIVE";
  $("modeBadge").classList.add(api.kind === "mock" ? "mock" : "live");
  if (api.kind !== "mock") $("demoBtn").classList.add("hidden");

  let jobs = [];
  let selectedId = null;
  const seenTurns = new Map();       // job_id -> Set of turn idx already shown done (for chip glow)
  const animated = new Set();        // job_ids whose SOAP has been typed out
  let detailKey = "";                // what the detail view currently shows, to avoid needless re-renders
  let typing = null;                 // active typewriter timer

  // ---------- upload ----------
  const dz = $("dropzone");
  const input = $("fileInput");
  async function uploadFiles(files) {
    for (const f of files) {
      try {
        const { job_id } = await api.upload(f);
        if (!selectedId) selectedId = job_id;
      } catch (e) { alert(`Upload failed for ${f.name}: ${e.message}`); }
    }
    refresh();
  }
  input.addEventListener("change", () => { uploadFiles([...input.files]); input.value = ""; });
  ["dragenter", "dragover"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("over"); }));
  ["dragleave", "drop"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove("over"); }));
  dz.addEventListener("drop", (e) => uploadFiles([...e.dataTransfer.files]));
  $("demoBtn").addEventListener("click", async () => {
    const ids = await api.loadDemoBatch(10);
    if (!selectedId) selectedId = ids[0];
    refresh();
  });

  $("approveBtn").addEventListener("click", async () => {
    if (!selectedId) return;
    await api.approve(selectedId);
    refresh();
  });

  // ---------- job list ----------
  function renderJobs() {
    const list = $("jobList");
    list.innerHTML = jobs.map((j) => {
      const seen = seenTurns.get(j.job_id) || new Set();
      const done = j.turns.filter((t) => t.done).length;
      const chips = j.turns.length
        ? j.turns.map((t) => {
            const sp = (t.speaker || "").toLowerCase() === "patient" ? "patient" : "doctor";
            const fresh = t.done && !seen.has(t.idx) ? " fresh" : "";
            return `<i class="chip ${sp}${t.done ? " done" : ""}${fresh}" title="${esc(t.speaker)} ${fmtTime(t.start)}"></i>`;
          }).join("")
        : `<span class="pending-diar">${j.status === "queued" ? "waiting for diarization…" : j.status === "diarizing" ? "finding speaker turns…" : ""}</span>`;
      const flags = j.result ? j.result.flags.filter(isAlert).length : 0;
      const label = j.status === "transcribing" ? `transcribing ${done}/${j.turns.length}` : j.status;
      return `<li class="job${j.job_id === selectedId ? " selected" : ""}" data-id="${esc(j.job_id)}">
        <div class="job-top">
          <span class="job-name">${esc(j.filename)}</span>
          <span class="status ${esc(j.status)}">${esc(label)}</span>
        </div>
        <div class="job-meta">
          <span>${esc(j.job_id)}</span>
          ${j.audio_s ? `<span>${fmtTime(j.audio_s)} audio</span>` : ""}
          ${j.result ? `<span>${fmtSecs(j.result.timings.total_s)} total</span>` : ""}
          ${flags ? `<span class="job-flag">⚑ ${flags} FLAG${flags > 1 ? "S" : ""}</span>` : ""}
          ${j.approved ? `<span style="color:var(--accent)">✓ approved</span>` : ""}
        </div>
        <div class="chips">${chips}</div>
      </li>`;
    }).join("");
    // Mark turns as seen after one paint so the glow shows once.
    for (const j of jobs) {
      const s = seenTurns.get(j.job_id) || new Set();
      j.turns.forEach((t) => t.done && s.add(t.idx));
      seenTurns.set(j.job_id, s);
    }
  }
  $("jobList").addEventListener("click", (e) => {
    const li = e.target.closest(".job");
    if (!li) return;
    selectedId = li.dataset.id;
    detailKey = "";
    renderJobs();
    renderDetail();
  });

  // ---------- metrics ----------
  function renderMetrics() {
    const done = jobs.filter((j) => j.status === "done" && j.result);
    const audio = done.reduce((a, j) => a + (j.result.timings.audio_s || 0), 0);
    const start = Math.min(...jobs.map((j) => j.created_at));
    const running = jobs.some((j) => j.status !== "done" && j.status !== "failed");
    const end = running ? Date.now() / 1000 : Math.max(...jobs.map((j) => j.finished_at || j.created_at));
    const wall = jobs.length ? Math.max(0, end - start) : 0;
    const tokens = done.reduce((a, j) => a + (j.result.timings.llm_tokens || 0), 0);
    const agentS = done.reduce((a, j) => a + (j.result.timings.agent_s || 0), 0);

    $("mAudio").textContent = (audio / 60).toFixed(1);
    $("mWall").textContent = fmtSecs(wall);
    // Seconds of processing per minute of audio, plus the real-time factor underneath.
    const perMin = audio && wall ? (60 * wall) / audio : null;
    $("mRtf").textContent = perMin == null ? "–" : perMin < 10 ? `${perMin.toFixed(1)} s` : `${Math.round(perMin)} s`;
    $("mRtfX").textContent = perMin == null ? "" : `${(audio / wall).toFixed(0)}× faster than real time`;
    $("mTps").textContent = tokens && agentS ? Math.round(tokens / agentS).toString() : "–";
    $("mJobs").textContent = `${done.length} / ${jobs.length}`;
  }

  // ---------- detail ----------
  function renderStages(j) {
    const cur = j.status === "failed" ? -1 : STAGES.indexOf(j.status);
    const t = j.result?.timings;
    const secs = { diarizing: t?.diarization_s, transcribing: t?.asr_s, summarising: t?.agent_s };
    return STAGES.slice(1).map((s, i) => {
      const idx = i + 1;
      const cls = idx < cur || j.status === "done" ? "complete" : idx === cur ? "active" : "";
      let label = STAGE_LABEL[s];
      if (s === "transcribing" && j.turns.length) label += ` ${j.turns.filter((x) => x.done).length}/${j.turns.length}`;
      if (secs[s] != null) label += ` · ${secs[s].toFixed(1)} s`;
      return `<span class="stage ${cls}">${esc(label)}</span>`;
    }).join('<span class="stage-arrow">→</span>') + (j.status === "failed" ? `<span class="status failed">failed: ${esc(j.error)}</span>` : "");
  }

  function renderTranscript(j) {
    const rows = j.result ? j.result.transcript.map((t, idx) => ({ ...t, idx, done: true })) : j.turns;
    if (!rows.length) return `<p class="muted">${j.status === "failed" ? "No transcript." : "Waiting for speaker turns…"}</p>`;
    return rows.map((t) => `<div class="turn ${esc(t.speaker)}">
      <div class="who">${esc(t.speaker)}<span class="ts">${fmtTime(t.start)}</span></div>
      <div class="text${t.done ? "" : " pending"}">${t.done ? esc(t.text) : "…"}</div>
    </div>`).join("");
  }

  const FLAG_LABEL = { critical: "CRITICAL", warning: "FLAG", info: "NOTE" };
  function renderChecks(r) {
    $("dBanner").innerHTML = r.flags.filter(isAlert)
      .map((f) => `<span class="pill ${esc(f.severity)}"><b>⚑ ${FLAG_LABEL[f.severity] || "FLAG"}</b>${esc(f.issue)}</span>`).join("");
    const flags = r.flags.map((f) => `<div class="flag-card ${esc(f.severity)}${f.verified ? "" : " unverified"}">
        <span class="verify">${f.verified ? esc(f.source || "verified") : "dropped by verifier"}</span>
        <span class="flag-label">${FLAG_LABEL[f.severity] || "FLAG"}</span><span class="issue">${esc(f.issue)}</span>
        ${f.fix ? `<div class="fix">Fix: ${esc(f.fix)}</div>` : ""}
      </div>`).join("");
    const v = r.verification;
    const verifier = v
      ? `<div class="kv"><div class="k">Verifier</div><div class="v">${v.passed ? '<span class="ok-text">✓ passed, every claim found in the transcript</span>' : `<span class="bad-text">✗ ${(v.unsupported || []).length} unsupported, ${(v.missing || []).length} missing</span>`}</div></div>`
      : "";
    $("dFlags").innerHTML = (flags || `<div class="ok-card">✓ No panel or rule issues found</div>`) + verifier;

    const p = r.panel;
    const panel = p
      ? `<div class="kv"><div class="k">Panel</div><div class="v"><b>${esc(p.name)}</b>${p.max_mc_days != null ? ` · MC limit ${esc(p.max_mc_days)} day${p.max_mc_days === 1 ? "" : "s"}` : ""}${p.formulary_only ? " · formulary only" : ""}</div></div>`
      : "";
    const mc = r.mc;
    const mcHtml = mc
      ? `<div class="kv"><div class="k">Medical certificate</div><div class="v"><span class="mc-days">${esc(mc.days)} day${mc.days > 1 ? "s" : ""}</span>${mc.start_date ? ` · ${esc(mc.start_date)} → ${esc(mc.end_date)}` : ""} · ${esc(mc.reason)}</div></div>`
      : `<div class="kv"><div class="k">Medical certificate</div><div class="v muted">None issued</div></div>`;
    const dx = r.diagnoses.length
      ? `<div class="kv"><div class="k">Diagnoses</div><div class="v">${r.diagnoses.map(esc).join(", ")}</div></div>` : "";
    $("dMc").innerHTML = panel + mcHtml + dx;

    if (r.prescriptions.length) {
      $("dDrugs").innerHTML = `<div class="kv"><div class="k">Prescriptions</div>${r.prescriptions.map((rx) => `
        <div class="rx ${esc(rx.status || "ok")}"><b>${esc(rx.drug)}</b> ${esc([rx.dose, rx.frequency, rx.duration].filter(Boolean).join(" · "))}
          ${rx.in_formulary === false ? '<span class="rx-tag">not in formulary</span>' : ""}
          ${(rx.notes || []).map((n) => `<div class="rx-note">${esc(n)}</div>`).join("")}
        </div>`).join("")}</div>`;
    } else {
      $("dDrugs").innerHTML = r.drug_alerts.length
        ? `<div class="kv"><div class="k">Drug alerts</div>${r.drug_alerts.map((d) => `<div class="drug"><b>${esc(d.drug)}</b> ${esc(d.issue)}</div>`).join("")}</div>`
        : "";
    }
    const t = r.timings;
    $("dTimings").innerHTML = [
      ["Diarization", t.diarization_s], ["ASR", t.asr_s], ["Agent", t.agent_s], ["Total", t.total_s],
    ].map(([l, v]) => `<div class="timing"><div class="t">${fmtSecs(v)}</div><div class="l">${l}</div></div>`).join("");
  }

  function toolHtml(c) {
    const cls = c.name === "flag" ? "fn flag" : "fn";
    return `<li class="tool"><span class="${cls}">${esc(c.name)}</span>(<span class="args">${esc(JSON.stringify(c.args))}</span>)
      <span class="res">→ ${esc(JSON.stringify(c.result))}</span></li>`;
  }

  function soapHtml(soap, upto) {
    // upto: number of characters to reveal across S, O, A, P (Infinity = all)
    let left = upto;
    return ["S", "O", "A", "P"].map((k) => {
      const full = soap[k] || "";
      const shown = full.slice(0, Math.max(0, left));
      const typingHere = left > 0 && left < full.length;
      left -= full.length;
      if (!shown && !typingHere) return "";
      return `<div class="soap-row"><div class="soap-key">${k}</div><div class="soap-text${typingHere ? " cursor" : ""}">${esc(shown)}</div></div>`;
    }).join("");
  }

  function playResult(r) {
    // Type out the SOAP note and drip the tool-call feed, as if streaming from Lightning.
    clearInterval(typing);
    const total = ["S", "O", "A", "P"].reduce((a, k) => a + (r.soap[k] || "").length, 0);
    const steps = 90;
    let i = 0;
    $("soapState").textContent = "streaming…";
    typing = setInterval(() => {
      i += 1;
      const upto = Math.ceil((total * i) / steps);
      $("dSoap").innerHTML = soapHtml(r.soap, upto);
      const nTools = Math.ceil((r.tool_calls.length * i) / steps);
      if ($("dTools").children.length < nTools) $("dTools").innerHTML = r.tool_calls.slice(0, nTools).map(toolHtml).join("");
      if (i >= steps) {
        clearInterval(typing);
        typing = null;
        $("soapState").textContent = "";
        renderChecks(r);
      }
    }, 30);
  }

  function renderDetail() {
    const j = jobs.find((x) => x.job_id === selectedId);
    $("emptyState").classList.toggle("hidden", !!j);
    $("detailBody").classList.toggle("hidden", !j);
    if (!j) return;

    const key = `${j.job_id}|${j.status}|${j.turns.filter((t) => t.done).length}|${j.approved}`;
    if (key === detailKey) return;
    const jobChanged = !detailKey.startsWith(j.job_id + "|");
    detailKey = key;

    $("dTitle").textContent = j.filename;
    if (jobChanged || j.status !== "done") $("dBanner").innerHTML = "";
    $("dStages").innerHTML = renderStages(j);
    const tr = $("dTranscript");
    const atBottom = tr.scrollHeight - tr.scrollTop - tr.clientHeight < 40;
    tr.innerHTML = renderTranscript(j);
    if (atBottom || jobChanged) tr.scrollTop = j.status === "done" && jobChanged ? 0 : tr.scrollHeight;

    const btn = $("approveBtn");
    btn.disabled = j.status !== "done" || j.approved;
    btn.textContent = j.approved ? "Approved ✓" : "Approve";
    btn.classList.toggle("approved", !!j.approved);

    if (j.status === "done" && j.result) {
      if (!animated.has(j.job_id)) {
        animated.add(j.job_id);
        $("dTools").innerHTML = "";
        $("dFlags").innerHTML = $("dMc").innerHTML = $("dDrugs").innerHTML = $("dTimings").innerHTML = "";
        playResult(j.result);
      } else if (jobChanged || !typing) {
        clearInterval(typing); typing = null;
        $("soapState").textContent = "";
        $("dSoap").innerHTML = soapHtml(j.result.soap, Infinity);
        $("dTools").innerHTML = j.result.tool_calls.map(toolHtml).join("");
        renderChecks(j.result);
      }
    } else {
      clearInterval(typing); typing = null;
      const waiting = j.status === "summarising" ? "Lightning is writing the note…" : j.status === "failed" ? "Job failed." : "Waiting for the transcript…";
      $("soapState").textContent = j.status === "summarising" ? "thinking…" : "";
      $("dSoap").innerHTML = `<p class="muted${j.status === "summarising" ? " cursor" : ""}">${waiting}</p>`;
      $("dTools").innerHTML = `<li class="muted">No tool calls yet.</li>`;
      $("dFlags").innerHTML = $("dMc").innerHTML = $("dDrugs").innerHTML = $("dTimings").innerHTML = "";
    }
  }

  // ---------- polling ----------
  let inflight = false;
  async function refresh() {
    if (inflight) return;
    inflight = true;
    try {
      const list = await api.list();
      // The list may omit results to stay small; fetch the selected job in full.
      if (selectedId) {
        const i = list.findIndex((x) => x.job_id === selectedId);
        if (i >= 0 && list[i].status === "done" && !list[i].result) list[i] = await api.get(selectedId);
      }
      jobs = list
        .map((j) => (j.result ? { ...j, result: normalizeResult(j.result, j) } : j))
        .sort((a, b) => a.created_at - b.created_at);
      renderJobs();
      renderMetrics();
      renderDetail();
    } catch (e) {
      console.error("refresh failed", e);
    } finally {
      inflight = false;
    }
  }
  setInterval(refresh, POLL_MS);
  refresh();
})();
