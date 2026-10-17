// API adapters. The UI only talks to one of these, so switching from mock
// data to the real FastAPI backend is a one-line change (or ?api=http).
//
// HTTP contract the real backend should implement:
//   POST /jobs                  multipart, field "file"      -> {job_id}
//   GET  /jobs                                               -> [Job]
//   GET  /jobs/{job_id}                                      -> Job
//   POST /jobs/{job_id}/approve                              -> {ok: true}
//
// Job = {
//   job_id, filename, status,          // queued|diarizing|transcribing|summarising|done|failed
//   audio_s,                           // known after diarization (or upload)
//   turns: [{idx, speaker, start, end, done, text?}],   // text appears as each turn is transcribed
//   created_at,                        // epoch seconds
//   finished_at,                       // epoch seconds or null
//   error,                             // string or null
//   approved,                          // bool
//   result                             // output JSON contract (CLAUDE.md) once status == done, else null
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
    async upload(file) {
      const fd = new FormData();
      fd.append("file", file, file.name);
      return this._json(await fetch(`${this.base}/jobs`, { method: "POST", body: fd }));
    }
    async approve(id) {
      return this._json(await fetch(`${this.base}/jobs/${encodeURIComponent(id)}/approve`, { method: "POST" }));
    }
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
    async upload(file) {
      const consults = window.MOCK_CONSULTS;
      // The hero consult goes to files that look like it, otherwise round-robin.
      let idx = this.seq % consults.length;
      if (/hero|panel\s*a/i.test(file.name)) idx = 0;
      return this._create(file.name, consults[idx]);
    }
    async approve(id) {
      const j = this.jobs.get(id);
      if (j) j.approved = true;
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
        job.finished_at = now();
        const c = job.consult;
        job.result = {
          job_id: job.job_id,
          transcript: c.transcript,
          soap: c.soap,
          mc: c.mc,
          drug_alerts: c.drug_alerts,
          flags: c.flags,
          tool_calls: c.tool_calls,
          timings: {
            diarization_s: +job.t.diar.toFixed(2),
            asr_s: +job.t.asr.toFixed(2),
            agent_s: +job.t.agent.toFixed(2),
            total_s: +(job.finished_at - job.created_at).toFixed(2),
            audio_s: c.audio_s,
            llm_tokens: c.llm_tokens,
          },
        };
      }
    }

    _public(j) {
      const { consult, t, asrStart, agentQueued, ...rest } = j;
      return JSON.parse(JSON.stringify(rest));
    }
  }

  window.ClinicApi = { HttpApi, MockApi };
})();
