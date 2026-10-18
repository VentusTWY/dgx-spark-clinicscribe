// Clinic terms editor: GET /terms to load, PUT /terms to save (see agent/terms.py).
// Without the backend (the static demo copy) it loads mock/clinic_terms.json, a copy of
// data/clinic_terms.json, and saves to this browser only.
(function () {
  const $ = (id) => document.getElementById(id);
  const KINDS = ["drug", "shorthand", "panel", "other"];
  const base = (new URLSearchParams(location.search).get("base") || "").replace(/\/$/, "");

  function msg(text, cls = "") { $("termsMsg").textContent = text; $("termsMsg").className = cls || "muted"; }

  function input(value, placeholder) {
    const el = document.createElement("input");
    el.value = value || "";
    el.placeholder = placeholder || "";
    return el;
  }

  function addRow(t = {}) {
    const tr = document.createElement("tr");
    const kind = document.createElement("select");
    KINDS.forEach((k) => kind.add(new Option(k, k, false, k === (t.kind || "drug"))));
    const del = document.createElement("button");
    del.className = "del"; del.title = "Remove"; del.textContent = "×";
    del.onclick = () => tr.remove();
    const cells = [
      input(t.term, "e.g. Bisolvon"), kind, input(t.meaning, "e.g. Bromhexine cough syrup"),
      input(t.maps_to, "formulary drug or panel id"), input((t.mishearings || []).join(", "), "e.g. by solvent"), del,
    ];
    cells.forEach((c) => { const td = document.createElement("td"); td.appendChild(c); tr.appendChild(td); });
    $("termsBody").appendChild(tr);
    return tr;
  }

  function rows() {
    return [...$("termsBody").rows].map((tr) => {
      const [term, kind, meaning, maps, mis] = [...tr.querySelectorAll("input, select")].map((el) => el.value);
      return { term, kind, meaning, maps_to: maps, mishearings: mis.split(",").map((s) => s.trim()).filter(Boolean) };
    });
  }

  const DEMO_KEY = "clinicscribe.demoTerms";
  let demo = false;
  function fill(terms) { $("termsBody").innerHTML = ""; terms.forEach(addRow); }

  async function load() {
    try {
      const res = await fetch(`${base}/terms`);
      if (!res.ok) throw new Error(`${res.status}`);
      fill((await res.json()).terms);
      msg("");
    } catch (_) {
      demo = true;
      let saved = null;
      try { saved = JSON.parse(localStorage.getItem(DEMO_KEY)); } catch (_) { /* storage blocked */ }
      try {
        fill(saved || (await (await fetch("mock/clinic_terms.json")).json()).terms);
        msg("Demo mode: no ClinicScribe backend here, so changes are saved in this browser only.");
      } catch (e) {
        msg(`Could not load terms (${e.message}).`, "err");
      }
    }
  }

  async function save() {
    if (demo) {
      const terms = rows().filter((t) => t.term.trim());
      try { localStorage.setItem(DEMO_KEY, JSON.stringify(terms)); } catch (_) { /* storage blocked */ }
      fill(terms);
      return msg(`Saved ${terms.length} terms in this browser (demo mode). With the backend they feed the next consult.`, "ok");
    }
    msg("Saving…");
    try {
      const res = await fetch(`${base}/terms`, {
        method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ terms: rows() }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.detail || `${res.status} ${res.statusText}`);
      fill(body.terms);
      msg(`Saved ${body.terms.length} terms.`, "ok");
    } catch (e) {
      msg(`Not saved: ${e.message}`, "err");
    }
  }

  $("addTerm").onclick = () => addRow().querySelector("input").focus();
  $("saveTerms").onclick = save;
  load();
})();
