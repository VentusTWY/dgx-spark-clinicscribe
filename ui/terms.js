// Clinic terms editor: GET /terms to load, PUT /terms to save (see agent/terms.py).
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

  async function load() {
    try {
      const res = await fetch(`${base}/terms`);
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
      $("termsBody").innerHTML = "";
      (await res.json()).terms.forEach(addRow);
      msg("");
    } catch (e) {
      msg(`Could not load terms (${e.message}). This page needs the ClinicScribe backend (./scripts/start_demo.sh).`, "err");
    }
  }

  async function save() {
    msg("Saving…");
    try {
      const res = await fetch(`${base}/terms`, {
        method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ terms: rows() }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.detail || `${res.status} ${res.statusText}`);
      $("termsBody").innerHTML = "";
      body.terms.forEach(addRow);
      msg(`Saved ${body.terms.length} terms.`, "ok");
    } catch (e) {
      msg(`Not saved: ${e.message}`, "err");
    }
  }

  $("addTerm").onclick = () => addRow().querySelector("input").focus();
  $("saveTerms").onclick = save;
  load();
})();
