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
      // Served from out/<stem>.result.json because Lightning failed: not a measurement of this run.
      cached: !!r.meta?.cached,
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

  // ---------- view ----------
  // The main page is the clinician view: transcript, SOAP note, items to review, MC and
  // prescriptions, and Approve. /dashboard/ (body data-view="tech") is the same app with the
  // pipeline, models, tool calls and metrics shown, for demos and debugging.
  const clinician = document.body.dataset.view !== "tech";
  function applyView() {
    document.body.classList.toggle("clinician", clinician);
    $("localBadge").textContent = clinician ? "● Runs in the clinic: no patient data leaves" : "● 100% local · Gigabyte AI TOP ATOM";
    $("checksTitle").textContent = clinician ? "To review before approving" : "Checks";
    $("emptyHint").textContent = clinician
      ? "Pick a consult to review its note."
      : "Pick a job to see its transcript, SOAP note, tool calls and flags.";
  }
  applyView();
  const PLAIN_STATUS = {
    queued: "Waiting", diarizing: "Transcribing", transcribing: "Transcribing",
    summarising: "Writing note", done: "Ready to review", failed: "Couldn't process",
  };

  // On a phone the list and the open consult take turns on screen (styles.css, max-width 860px):
  // opening a consult shows it, "All consults" goes back. Wider screens show both and ignore this.
  function showDetail(on) {
    document.body.classList.toggle("show-detail", on);
    if (on) window.scrollTo(0, 0);
  }
  $("backBtn").addEventListener("click", () => showDetail(false));

  let jobs = [];
  let selectedId = null;
  const seenTurns = new Map();       // job_id -> Set of turn idx already shown done (for chip glow)
  const animated = new Set();        // job_ids whose SOAP has been typed out
  let detailKey = "";                // what the detail view currently shows, to avoid needless re-renders
  let typing = null;                 // active typewriter timer

  const originalAudioUrls = new Map();
  window.addEventListener("pagehide", (event) => {
    if (!event.persisted) originalAudioUrls.forEach((url) => URL.revokeObjectURL(url));
  });

  // ---------- upload ----------
  const dz = $("dropzone");
  const input = $("fileInput");
  async function uploadFiles(files) {
    pruneFinished();
    const entries = files.map((f) => track({ filename: f.name, uploadPct: 0 }));
    openOverlay();
    for (const [i, f] of files.entries()) {
      const entry = entries[i];
      entry.uploading = true;
      entry.startedAt = Date.now();
      try {
        const { job_id } = await api.upload(f, (pct) => { entry.uploadPct = pct; renderOverlay(); });
        entry.jobId = job_id;
        if (f.type.startsWith("audio/") || /\.(wav|mp3|m4a|flac|ogg|aac|webm)$/i.test(f.name)) {
          originalAudioUrls.set(job_id, URL.createObjectURL(f));
        }
        if (!selectedId) selectedId = job_id;
      } catch (e) {
        entry.error = `Upload failed: ${e.message}`;
      }
      entry.uploading = false;
      refresh();
    }
  }
  input.addEventListener("change", () => { uploadFiles([...input.files]); input.value = ""; });
  ["dragenter", "dragover"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("over"); }));
  ["dragleave", "drop"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove("over"); }));
  dz.addEventListener("drop", (e) => uploadFiles([...e.dataTransfer.files]));
  $("demoBtn").addEventListener("click", async () => {
    const ids = await api.loadDemoBatch(10);
    pruneFinished();
    if (!selectedId) selectedId = ids[0];
    ids.forEach((id, i) => track({ jobId: id, filename: window.MOCK_CONSULTS[i === 0 ? 0 : 1 + ((i - 1) % (window.MOCK_CONSULTS.length - 1))].name, startedAt: Date.now() }));
    openOverlay();
    refresh();
  });

  // Relative sample URLs also work when the UI is hosted under /clinicscribe/.
  const audioSamples = ["1.wav", "2.wav", "3.wav", "4.wav", "5.wav", "6.wav"];
  const textSamples = ["4468919.txt", "4468679.txt", "4469528.txt"];
  $("sampleFiles").innerHTML = audioSamples.map((name) => `
    <div class="sample-file">
      <strong>${name}</strong>
      <audio controls preload="none" src="samples/${name}" aria-label="Play sample ${name}"></audio>
      <div><a href="samples/${name}" download>Download</a>
      <button class="btn secondary" data-sample="${name}">${api.kind === "mock" ? "Simulate demo" : "Process audio"}</button></div>
    </div>`).join("") + `<h4>Text references</h4>` + textSamples.map((name) => `
    <p><a href="samples/${name}" target="_blank" rel="noopener">${name}</a> · <a href="samples/${name}" download>Download</a></p>`).join("");
  $("sampleNote").textContent = api.kind === "mock"
    ? "Audio playback is real. Simulate demo uses canned results, not transcription of the recording. Connect the backend for real processing."
    : "Process audio sends the recording to the configured backend.";
  $("sampleFiles").addEventListener("click", async (event) => {
    const button = event.target.closest("[data-sample]");
    if (!button || button.disabled) return;
    button.disabled = true;
    try {
      const response = await fetch(`samples/${encodeURIComponent(button.dataset.sample)}`);
      if (!response.ok) throw new Error(`Sample download failed (${response.status})`);
      await uploadFiles([new File([await response.blob()], button.dataset.sample, { type: "audio/wav" })]);
    } catch (error) {
      $("sampleNote").textContent = error.message;
    } finally { button.disabled = false; }
  });
  document.addEventListener("play", (event) => {
    if (event.target.tagName === "AUDIO") {
      document.querySelectorAll("audio").forEach((audio) => { if (audio !== event.target) audio.pause(); });
    }
  }, true);

  $("approveBtn").addEventListener("click", async () => {
    if (!selectedId) return;
    await api.approve(selectedId);
    refresh();
  });

  // ---------- processing window ----------
  // Opens on upload and follows each uploaded job through the real job status from the API:
  // upload progress, queue, diarization + ASR (turn count when the first layer reports turns),
  // the Lightning agent, then done. Hiding it keeps the jobs running.
  let tracked = [];
  let overlayOpen = false;
  let closeTimer = null;
  const track = (entry) => { tracked.push(entry); return entry; };
  // A new upload starts a fresh window: consults that already finished leave it (they stay in the
  // job list), so the new files are not buried under old results.
  function pruneFinished() {
    tracked = tracked.filter((entry) => {
      const st = procState(entry);
      const keep = !(st.step === "done" || st.failed);
      if (!keep && entry.el) entry.el.remove();
      return keep;
    });
  }
  function openOverlay() {
    overlayOpen = true;
    clearTimeout(closeTimer);
    $("procOverlay").classList.remove("hidden");
    $("procShow").classList.add("hidden");
    renderOverlay();
  }
  function hideOverlay() {
    overlayOpen = false;
    $("procOverlay").classList.add("hidden");
    $("procShow").classList.toggle("hidden", !tracked.length);
  }
  $("procHide").addEventListener("click", hideOverlay);
  $("procShow").addEventListener("click", openOverlay);
  $("procOverlay").addEventListener("click", (e) => {
    if (e.target.id === "procOverlay") return hideOverlay();
    const btn = e.target.closest("[data-open]");
    if (!btn) return;
    selectedId = btn.dataset.open;
    showDetail(true);
    detailKey = "";
    hideOverlay();
    renderJobs();
    renderDetail();
  });
  document.addEventListener("keydown", (e) => e.key === "Escape" && overlayOpen && hideOverlay());

  const PROC_STEPS_TECH = [["upload", "Upload"], ["first", "Diarization + ASR"], ["agent", "Lightning agent"], ["done", "Done"]];
  const PROC_STEPS_PLAIN = [["upload", "Upload"], ["first", "Transcribe"], ["agent", "Write note"], ["done", "Ready"]];
  function procState(entry) {
    const j = entry.jobId && jobs.find((x) => x.job_id === entry.jobId);
    if (entry.error) return { step: "upload", failed: true, label: entry.error };
    if (!entry.jobId) {
      return entry.uploading
        ? { step: "upload", pct: entry.uploadPct, label: `Uploading ${Math.round(entry.uploadPct)}%` }
        : { step: "upload", pct: 0, label: "Waiting to upload" };
    }
    if (!j) return { step: "first", label: "Starting…" };
    const doneTurns = j.turns.filter((t) => t.done).length;
    const last = [...j.turns].reverse().find((t) => t.done && t.text);
    const line = last ? `${last.speaker}: ${last.text}` : "";
    switch (j.status) {
      case "queued":
        return { step: "first", label: "Waiting in the queue" };
      case "diarizing":
        return { step: "first", label: clinician ? "Transcribing the consult" : "Finding who speaks when and transcribing" };
      case "transcribing":
        return j.turns.length
          ? { step: "first", pct: (100 * doneTurns) / j.turns.length, label: `Transcribed ${doneTurns} of ${j.turns.length} speaker turns`, line }
          : { step: "first", label: "Transcribing" };
      case "summarising":
        return { step: "agent", label: clinician ? "Writing the SOAP note and checking drugs, MC and panel rules" : "Lightning is writing the SOAP note and checking drugs, MC and panel rules", line: j.turns.length ? `${j.turns.length} turns transcribed` : "" };
      case "done": {
        const t = j.result?.timings || {};
        return { step: "done", pct: 100, label: `Done in ${fmtSecs(t.total_s ?? (j.finished_at - j.created_at))}${j.result?.cached ? " (cached result)" : ""}`, open: j.job_id };
      }
      case "failed":
        return { step: "first", failed: true, label: j.error || "Failed" };
      default:
        return { step: "first", label: j.status };
    }
  }

  function renderOverlay() {
    if (!tracked.length) return;
    const states = tracked.map(procState);
    const finished = states.filter((st) => st.step === "done" || st.failed).length;
    $("procTitle").textContent = finished === tracked.length
      ? `Finished ${tracked.length} consult${tracked.length > 1 ? "s" : ""}`
      : `Processing ${tracked.length - finished} of ${tracked.length} consult${tracked.length > 1 ? "s" : ""}`;
    $("procShow").textContent = finished === tracked.length ? "Show processing results" : `Show processing (${tracked.length - finished} running)`;
    $("procNote").textContent = api.kind === "mock"
      ? "Mock data: this run is simulated in the browser."
      : clinician ? "You can close this window. Consults keep processing." : "Live: every stage runs on the ATOM. You can hide this window; jobs keep running.";
    if (!overlayOpen) return;
    // Each card is built once and then updated in place. Rebuilding the list on every poll replaced
    // the progress bar each time, which restarted its animation and width transition.
    const list = $("procList");
    const PROC_STEPS = clinician ? PROC_STEPS_PLAIN : PROC_STEPS_TECH;
    tracked.forEach((entry, i) => {
      const st = states[i];
      if (!entry.el) {
        entry.el = document.createElement("li");
        entry.el.innerHTML = `<div class="proc-top"><span class="proc-name"></span><span class="proc-elapsed"></span></div>
          <div class="proc-steps"></div>
          <div class="proc-bar"><div class="proc-fill"></div></div>
          <div class="proc-label"></div><div class="proc-line"></div>
          <button class="btn secondary proc-open hidden">Open result</button>`;
        list.appendChild(entry.el);
      }
      const el = entry.el;
      const q = (sel) => el.querySelector(sel);
      const idx = PROC_STEPS.findIndex(([k]) => k === st.step);
      const steps = PROC_STEPS.map(([k, label], n) =>
        `<span class="proc-step ${n < idx || st.step === "done" ? "complete" : n === idx && !st.failed ? "active" : ""}">${label}</span>`).join("");
      const elapsedS = entry.startedAt ? (Date.now() - entry.startedAt) / 1000 : 0;
      const determinate = st.pct != null;
      el.className = `proc-item${st.failed ? " failed" : ""}`;
      q(".proc-name").textContent = jobs.find((x) => x.job_id === entry.jobId)?.filename || entry.filename;
      q(".proc-elapsed").textContent = st.step === "done" || st.failed ? "" : entry.startedAt ? fmtSecs(elapsedS) : "";
      if (q(".proc-steps").innerHTML !== steps) q(".proc-steps").innerHTML = steps;
      q(".proc-bar").classList.toggle("indeterminate", !determinate && !st.failed);
      q(".proc-fill").style.width = `${st.failed ? 100 : determinate ? st.pct : 35}%`;
      q(".proc-label").innerHTML = st.failed ? `<span class="err">${esc(st.label)}</span>` : esc(st.label);
      q(".proc-line").textContent = st.line || "";
      q(".proc-line").classList.toggle("hidden", !st.line);
      const open = q(".proc-open");
      open.classList.toggle("hidden", !st.open);
      if (st.open) open.dataset.open = st.open;
    });
    // When everything has finished, show the result for a moment, then open the first finished job.
    if (finished === tracked.length && !closeTimer) {
      closeTimer = setTimeout(() => {
        const first = states.find((st) => st.open);
        if (overlayOpen && first) {
          selectedId = first.open;
          showDetail(true);
          detailKey = "";
          hideOverlay();
          renderJobs();
          renderDetail();
        }
      }, 2500);
    }
    if (finished < tracked.length) { clearTimeout(closeTimer); closeTimer = null; }
  }

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
      const label = clinician
        ? (j.approved ? "Approved" : PLAIN_STATUS[j.status] || j.status)
        : j.status === "transcribing" ? `transcribing ${done}/${j.turns.length}` : j.status;
      return `<li class="job${j.job_id === selectedId ? " selected" : ""}" data-id="${esc(j.job_id)}">
        <div class="job-top">
          <span class="job-name">${esc(j.filename)}</span>
          <span class="status ${esc(j.status)}">${esc(label)}</span>
        </div>
        <div class="job-meta">
          <span class="tech">${esc(j.job_id)}</span>
          ${j.audio_s ? `<span>${fmtTime(j.audio_s)} audio</span>` : ""}
          ${j.result ? `<span class="tech">${fmtSecs(j.result.timings.total_s)} total</span>` : ""}
          ${flags ? `<span class="job-flag">⚑ ${flags} ${clinician ? "to review" : `FLAG${flags > 1 ? "S" : ""}`}</span>` : ""}
          ${j.approved ? `<span style="color:var(--accent)">✓ approved</span>` : ""}
        </div>
        <div class="chips tech">${chips}</div>
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
    showDetail(true);
    detailKey = "";
    renderJobs();
    renderDetail();
  });

  // ---------- metrics ----------
  // Every number here comes from the API: server timestamps, the first layer's audio length and the
  // agent's own timings (agent_s, completion tokens from vLLM's usage field). Cached fallback results
  // are left out, since they were not produced by this run.
  function busySeconds(intervals) {
    // Wall time while at least one job was in flight, so idle gaps between uploads don't count.
    const sorted = intervals.filter(([a, b]) => b > a).sort((x, y) => x[0] - y[0]);
    let total = 0, curA = null, curB = null;
    for (const [a, b] of sorted) {
      if (curB == null || a > curB) { if (curB != null) total += curB - curA; curA = a; curB = b; }
      else curB = Math.max(curB, b);
    }
    return curB == null ? 0 : total + (curB - curA);
  }

  function renderMetrics() {
    const done = jobs.filter((j) => j.status === "done" && j.result);
    const measured = done.filter((j) => !j.result.cached);
    const audio = measured.reduce((a, j) => a + (j.result.timings.audio_s || 0), 0);
    const nowS = Date.now() / 1000;
    const wall = busySeconds(jobs
      .filter((j) => !(j.result && j.result.cached) && j.status !== "failed")
      .map((j) => [j.created_at, j.finished_at || nowS]));
    const doneWall = busySeconds(measured.map((j) => [j.created_at, j.finished_at]));
    const tokens = measured.reduce((a, j) => a + (j.result.timings.llm_tokens || 0), 0);
    const agentS = measured.reduce((a, j) => a + (j.result.timings.agent_s || 0), 0);

    $("mAudio").textContent = (audio / 60).toFixed(1);
    $("mWall").textContent = fmtSecs(wall);
    // Seconds of processing per minute of audio (finished jobs only), plus the real-time factor underneath.
    const perMin = audio && doneWall ? (60 * doneWall) / audio : null;
    $("mRtf").textContent = perMin == null ? "–" : perMin < 10 ? `${perMin.toFixed(1)} s` : `${Math.round(perMin)} s`;
    $("mRtfX").textContent = perMin == null ? "" : `${(audio / doneWall).toFixed(0)}× faster than real time`;
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
      if (s === "diarizing" && t && t.diarization_s == null && t.asr_s != null) return null;
      if (s === "transcribing" && t && t.diarization_s == null && t.asr_s != null) label = "Diarization + ASR";
      if (s === "transcribing" && j.turns.length) label += ` ${j.turns.filter((x) => x.done).length}/${j.turns.length}`;
      if (secs[s] != null) label += ` · ${secs[s].toFixed(1)} s`;
      return `<span class="stage ${cls}">${esc(label)}</span>`;
    }).filter(Boolean).join('<span class="stage-arrow">→</span>') + (j.status === "failed" ? `<span class="status failed">failed: ${esc(j.error)}</span>` : "");
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
  const flagLabel = (sev) => (clinician && sev === "warning" ? "REVIEW" : FLAG_LABEL[sev] || "FLAG");
  function renderChecks(r) {
    $("dBanner").innerHTML = (r.cached ? '<span class="cached-pill">Cached result from an earlier run: Lightning was unavailable</span>' : "") + r.flags.filter(isAlert)
      .map((f) => `<span class="pill ${esc(f.severity)}"><b>⚑ ${flagLabel(f.severity)}</b>${esc(f.issue)}</span>`).join("");
    // Flags the verifier dropped are pipeline detail, so the clinician view leaves them out.
    const flags = r.flags.filter((f) => !clinician || f.verified).map((f) => `<div class="flag-card ${esc(f.severity)}${f.verified ? "" : " unverified"}">
        <span class="verify tech">${f.verified ? esc(f.source || "verified") : "dropped by verifier"}</span>
        <span class="flag-label">${flagLabel(f.severity)}</span><span class="issue">${esc(f.issue)}</span>
        ${f.fix ? `<div class="fix">Fix: ${esc(f.fix)}</div>` : ""}
      </div>`).join("");
    const v = r.verification;
    const verifier = v
      ? `<div class="kv tech"><div class="k">Verifier</div><div class="v">${v.passed ? '<span class="ok-text">✓ passed, every claim found in the transcript</span>' : `<span class="bad-text">✗ ${(v.unsupported || []).length} unsupported, ${(v.missing || []).length} missing</span>`}</div></div>`
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
    const audio = $("originalAudio");
    const source = j ? originalAudioUrls.get(j.job_id) : null;
    $("originalAudioPanel").classList.toggle("hidden", !source);
    if ((audio.getAttribute("src") || null) !== (source || null)) {
      audio.pause();
      if (source) audio.src = source;
      else audio.removeAttribute("src");
      audio.load();
    }
    if (!j) return;

    const key = `${j.job_id}|${j.status}|${j.turns.filter((t) => t.done).length}|${j.approved}|${!!j.result}`;
    if (key === detailKey) return;
    const jobChanged = !detailKey.startsWith(j.job_id + "|");
    detailKey = key;

    $("dTitle").textContent = j.filename;
    if (jobChanged || j.status !== "done") $("dBanner").innerHTML = "";
    $("dStages").innerHTML = renderStages(j);
    $("dPlain").textContent = j.status === "failed" ? `Couldn't process this file: ${j.error || "unknown error"}`
      : j.approved ? "Approved" : j.status === "done" ? "Ready for your review" : `${PLAIN_STATUS[j.status] || j.status}…`;
    const tr = $("dTranscript");
    const atBottom = tr.scrollHeight - tr.scrollTop - tr.clientHeight < 40;
    tr.innerHTML = renderTranscript(j);
    if (atBottom || jobChanged) tr.scrollTop = j.status === "done" && jobChanged ? 0 : tr.scrollHeight;

    const btn = $("approveBtn");
    btn.disabled = j.status !== "done" || j.approved;
    btn.textContent = j.approved ? "Approved ✓" : "Approve";
    btn.classList.toggle("approved", !!j.approved);

    if (j.status === "done" && j.result) {
      if (!animated.has(j.job_id) && !clinician) {
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
      const waiting = j.status === "summarising" ? (clinician ? "Writing the note…" : "Lightning is writing the note…") : j.status === "failed" ? "Job failed." : "Waiting for the transcript…";
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
      renderOverlay();
    } catch (e) {
      console.error("refresh failed", e);
    } finally {
      inflight = false;
    }
  }
  setInterval(refresh, POLL_MS);
  refresh();
})();
