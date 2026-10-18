// API adapters. The UI only talks to one of these, so switching from mock
// data to the real FastAPI backend is a one-line change (or ?api=http).
//
// HTTP contract the real backend should implement:
//   POST /jobs                  multipart, field "file"      -> {job_id}
//   GET  /jobs                                               -> [Job]
//   GET  /jobs/{job_id}                                      -> Job
//   POST /jobs/{job_id}/approve                              -> {ok: true}
//   POST /jobs/{job_id}/reject     JSON {reason?}            -> {ok: true}   "Don't approve": needs changes
//   PUT  /jobs/{job_id}/transcript JSON {turns: [{idx, text}]} -> {ok, changed}  doctor's corrections
//   POST /jobs/{job_id}/rerun                                -> {ok: true}   rewrite the note from the transcript
//
// Job = {
//   job_id, filename, status,          // queued|diarizing|transcribing|summarising|done|failed
//   audio_s,                           // known after diarization (or upload)
//   turns: [{idx, speaker, start, end, done, text?}],   // text appears as each turn is transcribed
//   created_at,                        // epoch seconds
//   finished_at,                       // epoch seconds or null
//   error,                             // string or null
//   approved,                          // bool
//   review,                            // null or {status: "approved"|"changes_requested", reason, at}
//   note_stale,                        // true after a transcript correction until the note is rewritten
//   reruns,                            // how many times the note was rewritten from a corrected transcript
//   result                             // once status == done, else null: the agent output
//                                      // (docs/agent_contract.md) plus transcript and timings.
//                                      // The CLAUDE.md contract shape is accepted too.
// }
// Optional extra in result.timings: llm_tokens (completion tokens) for the tokens/s metric.

(function () {
  const now = () => Date.now() / 1000;
  const rand = (a, b) => a + Math.random() * (b - a);

  class HttpApi {
    constructor(base = "") { this.base = base.replace(/\/$/, ""); this.kind = "http"; }
    async _json(res) {
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
      return res.json();
    }
    async list() { return this._json(await fetch(`${this.base}/jobs`)); }
    async get(id) { return this._json(await fetch(`${this.base}/jobs/${encodeURIComponent(id)}`)); }
    // XHR rather than fetch so the processing window can show upload progress.
    upload(file, onProgress = () => {}) {
      return new Promise((resolve, reject) => {
        const fd = new FormData();
        fd.append("file", file, file.name);
        const xhr = new XMLHttpRequest();
        xhr.open("POST", `${this.base}/jobs`);
        xhr.upload.onprogress = (e) => e.lengthComputable && onProgress((100 * e.loaded) / e.total);
        xhr.onload = () => {
          if (xhr.status < 200 || xhr.status >= 300) return reject(new Error(`${xhr.status} ${xhr.responseText.slice(0, 200)}`));
          try { resolve(JSON.parse(xhr.responseText)); } catch (e) { reject(e); }
        };
        xhr.onerror = () => reject(new Error("network error"));
        xhr.send(fd);
      });
    }
    async _send(id, path, method, body) {
      const res = await fetch(`${this.base}/jobs/${encodeURIComponent(id)}/${path}`, {
        method, headers: body ? { "Content-Type": "application/json" } : {}, body: body ? JSON.stringify(body) : undefined,
      });
      if (!res.ok) throw new Error((await res.json().catch(() => null))?.detail || `${res.status} ${res.statusText}`);
      return res.json();
    }
    approve(id) { return this._send(id, "approve", "POST"); }
    reject(id, reason) { return this._send(id, "reject", "POST", { reason }); }
    saveTranscript(id, turns) { return this._send(id, "transcript", "PUT", { turns }); }
    rerun(id) { return this._send(id, "rerun", "POST"); }
  }

  // In-browser simulation of the async pipeline: diar_queue -> asr_queue -> agent_queue,
  // each with a fixed number of worker slots, so a batch shows real queueing behaviour.
  class MockApi {
    constructor() {
      this.kind = "mock";
      this.jobs = new Map();
      this.seq = 0;
      this.queues = { diar: [], asr: [], agent: [] };
      this.slots = { diar: 2, asr: 6, agent: 6 };
      this.running = [];
      this.speed = 1;
      setInterval(() => this._tick(0.1 * this.speed), 100);
    }

    async list() { return [...this.jobs.values()].map((j) => this._public(j)); }
    async get(id) {
      const j = this.jobs.get(id);
      if (!j) throw new Error("404 job not found");
      return this._public(j);
    }
    async upload(file, onProgress = () => {}) {
      onProgress(100);
      const consults = window.MOCK_CONSULTS;
      // The hero consult goes to files that look like it, otherwise round-robin.
      let idx = this.seq % consults.length;
      if (/hero|aia/i.test(file.name)) idx = 0;
      return this._create(file.name, consults[idx]);
    }
    async approve(id) {
      const j = this.jobs.get(id);
      if (j) { j.approved = true; j.review = { status: "approved", reason: "", at: now() }; }
      return { ok: true };
    }
    async reject(id, reason = "") {
      const j = this.jobs.get(id);
      if (j) { j.approved = false; j.review = { status: "changes_requested", reason, at: now() }; }
      return { ok: true };
    }
    async saveTranscript(id, turns) {
      const j = this.jobs.get(id);
      let changed = false;
      for (const { idx, text } of turns) {
        const t = j?.result?.transcript[idx];
        if (t && t.text !== text) { j.result.transcript[idx] = { ...t, text, edited: true }; changed = true; }
      }
      if (changed) {
        j.edits = j.result.transcript.map((t) => (t.edited ? t.text : null));
        j.turns.forEach((t) => { if (j.edits[t.idx] != null) { t.text = j.edits[t.idx]; t.edited = true; } });
        Object.assign(j, { note_stale: true, approved: false, review: null });
      }
      return { ok: true, changed };
    }
    // The mock note is canned, so a rewrite only replays the agent stage; the corrections stay in the transcript.
    async rerun(id) {
      const j = this.jobs.get(id);
      if (!j || j.status !== "done") throw new Error("the note is not ready yet");
      Object.assign(j, { status: "summarising", result: null, approved: false, review: null, reruns: j.reruns + 1 });
      j.agentQueued = now();
      this.queues.agent.push({ job: j, stage: "agent", left: rand(2.5, 4.5) });
      return { ok: true };
    }

    // Demo helper: hero file plus a batch of short files.
    async loadDemoBatch(n = 10) {
      const consults = window.MOCK_CONSULTS;
      const ids = [(await this._create(consults[0].name, consults[0])).job_id];
      for (let i = 0; i < n; i++) {
        const c = consults[1 + (i % (consults.length - 1))];
        ids.push((await this._create(`batch_${String(i + 1).padStart(2, "0")}_${c.name}`, c)).job_id);
      }
      return ids;
    }

    _create(filename, consult) {
      this.seq += 1;
      const job_id = `job_${String(this.seq).padStart(3, "0")}`;
      const job = {
        job_id, filename, status: "queued", audio_s: consult.audio_s,
        turns: [], created_at: now(), finished_at: null, error: null, approved: false,
        review: null, note_stale: false, reruns: 0, edits: null,
        result: null, consult, t: { diar: 0, asr: 0, agent: 0 }, asrStart: null,
      };
      this.jobs.set(job_id, job);
      this.queues.diar.push({ job, stage: "diar", left: 0.8 + consult.audio_s * 0.012 });
      return Promise.resolve({ job_id });
    }

    _tick(dt) {
      for (const stage of ["diar", "asr", "agent"]) {
        const busy = this.running.filter((r) => r.stage === stage).length;
        for (let i = busy; i < this.slots[stage] && this.queues[stage].length; i++) {
          const task = this.queues[stage].shift();
          if (stage === "diar") task.job.status = "diarizing";
          if (stage === "agent") task.job.status = "summarising";
          if (stage === "asr" && task.job.asrStart == null) task.job.asrStart = now();
          this.running.push(task);
        }
      }
      const done = [];
      for (const r of this.running) {
        r.left -= dt;
        if (r.left <= 0) done.push(r);
      }
      this.running = this.running.filter((r) => r.left > 0);
      for (const r of done) this._finish(r);
    }

    _finish(task) {
      const { job } = task;
      if (task.stage === "diar") {
        job.t.diar = now() - job.created_at;
        job.status = "transcribing";
        job.turns = job.consult.transcript.map((t, idx) => ({
          idx, speaker: t.speaker, start: t.start, end: t.end, done: false,
        }));
        for (const turn of job.turns) {
          const dur = turn.end - turn.start;
          this.queues.asr.push({ job, stage: "asr", turn, left: 0.25 + dur * rand(0.04, 0.09) });
        }
      } else if (task.stage === "asr") {
        task.turn.done = true;
        task.turn.text = job.consult.transcript[task.turn.idx].text;
        if (job.turns.every((t) => t.done)) {
          job.t.asr = now() - job.asrStart;
          job.agentQueued = now();
          this.queues.agent.push({ job, stage: "agent", left: rand(2.5, 4.5) });
        }
      } else if (task.stage === "agent") {
        job.t.agent = now() - job.agentQueued;
        job.status = "done";
        job.note_stale = false;
        const rerun = job.reruns > 0;
        if (!rerun) job.finished_at = now();
        const { name, audio_s, llm_tokens, ...contract } = job.consult;
        const transcript = contract.transcript.map((t, i) => (job.edits?.[i] != null ? { ...t, text: job.edits[i], edited: true } : t));
        job.result = {
          job_id: job.job_id,
          ...contract,
          transcript,
          meta: rerun ? { rerun: true } : undefined,
          timings: {
            diarization_s: +job.t.diar.toFixed(2),
            asr_s: +job.t.asr.toFixed(2),
            agent_s: +job.t.agent.toFixed(2),
            total_s: +(job.finished_at - job.created_at).toFixed(2),
            audio_s,
            llm_tokens,
          },
        };
      }
    }

    _public(j) {
      const { consult, t, asrStart, agentQueued, edits, ...rest } = j;
      return JSON.parse(JSON.stringify(rest));
    }
  }

  window.ClinicApi = { HttpApi, MockApi };
})();
