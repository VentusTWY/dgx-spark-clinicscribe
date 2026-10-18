"""Tools the model can call. Each tool is plain Python over local JSON data.

The model decides *what* to call; the facts (formulary, panel rules, dates,
allergy checks) come from here, not from the model's memory.
"""

import datetime as dt
import json
from pathlib import Path

from .terms import find_term

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def _load(name):
    return json.loads((DATA_DIR / name).read_text())


def _schema(name, description, properties, required):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required},
        },
    }


TOOL_SCHEMAS = [
    _schema(
        "get_panel_rules",
        "Look up the billing panel (insurer / corporate) rules for this patient: max MC days, formulary limits, notes. Call this early if the patient has a panel.",
        {"panel_id": {"type": "string", "description": "Panel id, e.g. PANEL-AIA. Use the patient's panel_id."}},
        ["panel_id"],
    ),
    _schema(
        "check_drug",
        "Check ONE drug the doctor prescribed against the clinic formulary, the patient's allergies and the panel. Call once per prescribed drug. The result is recorded as a prescription line.",
        {
            "drug": {"type": "string", "description": "Drug name exactly as said, generic, brand or clinic term. Do not guess a name from a garbled word."},
            "dose": {"type": "string", "description": "Dose per administration, e.g. '1 g' or '10 mg'"},
            "frequency": {"type": "string", "description": "e.g. 'QID PRN', 'TDS', 'ON'"},
            "duration": {"type": "string", "description": "e.g. '5 days'; empty if not stated"},
            "dose_mg": {"type": "number", "description": "Dose per administration in mg, if known"},
            "times_per_day": {"type": "number", "description": "Maximum administrations per day, if known"},
        },
        ["drug"],
    ),
    _schema(
        "draft_mc",
        "Draft a medical certificate (MC) if and only if the doctor agreed to give one.",
        {
            "days": {"type": "integer", "description": "Number of MC days the doctor stated"},
            "start_date": {"type": "string", "description": "YYYY-MM-DD; omit to use the visit date"},
            "reason": {"type": "string", "description": "Short, non-sensitive reason, e.g. 'Upper respiratory tract infection'"},
        },
        ["days", "reason"],
    ),
    _schema(
        "write_soap_note",
        "Write the final SOAP note for the consultation. Only include facts stated in the transcript.",
        {
            "subjective": {"type": "string", "description": "History: complaints, duration, relevant negatives, allergies, meds"},
            "objective": {"type": "string", "description": "Exam findings and vitals as stated"},
            "assessment": {"type": "string", "description": "Working diagnosis / impression"},
            "plan": {"type": "string", "description": "Medications, MC, advice, return precautions, follow-up"},
            "diagnoses": {"type": "array", "items": {"type": "string"}, "description": "Short diagnosis labels"},
        },
        ["subjective", "objective", "assessment", "plan"],
    ),
    _schema(
        "flag",
        "Raise something the doctor should review before signing: safety issues, missing information, inconsistencies.",
        {
            "severity": {"type": "string", "enum": ["info", "warning", "critical"]},
            "message": {"type": "string"},
        },
        ["severity", "message"],
    ),
]


class Workspace:
    """Holds the visit context and everything the tools produce."""

    def __init__(self, patient=None, visit_date=None, terms=None):
        self.patient = patient or {}
        self.terms = terms or []
        self.visit_date = visit_date or dt.date.today().isoformat()
        self.formulary = _load("formulary.json")["drugs"]
        self.panels = {p["id"]: p for p in _load("panels.json")["panels"]}
        self.soap = None
        self.prescriptions = []
        self.mc = None
        self.panel = None
        self.flags = []
        self.tool_log = []

    # --- helpers -----------------------------------------------------------

    def _add_flag(self, severity, message, source):
        if not any(f["message"] == message for f in self.flags):
            self.flags.append({"severity": severity, "message": message, "source": source})

    def _find_drug(self, name):
        key = name.strip().lower()
        for d in self.formulary:
            names = [d["name"].lower()] + [a.lower() for a in d.get("aliases", [])]
            # Exact name, or a longer name inside the phrase ("paracetamol 500mg").
            if key in names or any(len(n) >= 5 and n in key for n in names):
                return d
        return None

    def _panel_rules(self):
        pid = self.patient.get("panel_id")
        return self.panels.get(pid) if pid else None

    # --- tools -------------------------------------------------------------

    def get_panel_rules(self, panel_id):
        rules = self.panels.get(panel_id)
        if not rules:
            self._add_flag("warning", f"Unknown panel '{panel_id}', check billing manually.", "get_panel_rules")
            return {"found": False, "known_panels": list(self.panels)}
        self.panel = {
            "id": rules["id"],
            "name": rules["name"],
            "max_mc_days": rules["max_mc_days"],
            "formulary_only": rules["formulary_only"],
            "notes": list(rules["notes"]),
        }
        return {"found": True, **rules}

    def check_drug(self, drug, dose="", frequency="", duration="", dose_mg=None, times_per_day=None):
        entry = self._find_drug(drug)
        term = None if entry else find_term(drug, [t for t in self.terms if t["kind"] == "drug"])
        if term:
            # A clinic term ("Panadol Actifast", or a known mishearing of it) resolves via maps_to.
            entry = self._find_drug(term["maps_to"]) if term.get("maps_to") else None
        # A repeat check of the same drug replaces the earlier line.
        label = (entry["name"] if entry else drug).lower()
        self.prescriptions = [p for p in self.prescriptions if p["drug"].lower() != label]
        allergies = [a.lower() for a in self.patient.get("allergies", [])]
        line = {
            "drug": entry["name"] if entry else drug,
            "dose": dose,
            "frequency": frequency,
            "duration": duration,
            "in_formulary": bool(entry),
            "status": "ok",
            "notes": [],
        }
        if term:
            line["clinic_term"] = term["term"]
            line["notes"].append(f"Clinic term: {term['term']}" + (f" ({term['meaning']})" if term.get("meaning") else ""))
        if not entry:
            line["status"] = "warning"
            line["notes"].append("Not in clinic formulary")
            panel = self._panel_rules()
            if panel and panel["formulary_only"]:
                line["notes"].append(f"{panel['name']} only covers formulary drugs")
            if term:
                self._add_flag("info", f"{term['term']} is a clinic term but not in the formulary, so no dose or allergy check was run.", "check_drug")
            else:
                self._add_flag("warning", f"'{drug}' matches no formulary drug or clinic term. Check the name before signing.", "check_drug")
        else:
            group = entry.get("allergy_group")
            if group and any(group in a or entry["name"].lower() in a for a in allergies):
                line["status"] = "danger"
                line["notes"].append(f"Patient is allergic to {group}")
                self._add_flag("critical", f"{entry['name']} conflicts with documented {group} allergy.", "check_drug")
            if dose_mg and times_per_day and entry.get("max_daily_mg"):
                daily = dose_mg * times_per_day
                if daily > entry["max_daily_mg"]:
                    line["status"] = "danger"
                    line["notes"].append(f"{daily:g} mg/day exceeds max {entry['max_daily_mg']} mg/day")
                    self._add_flag("critical", f"{entry['name']} dose {daily:g} mg/day exceeds max {entry['max_daily_mg']} mg/day.", "check_drug")
            for c in entry.get("cautions", []):
                line["notes"].append(f"Caution: {c}")
            # Interactions with other drugs already prescribed this visit or listed as current meds.
            others = [p["drug"].lower() for p in self.prescriptions] + [m.lower() for m in self.patient.get("current_meds", [])]
            for other in entry.get("interactions", []):
                if any(other in o for o in others):
                    if line["status"] == "ok":
                        line["status"] = "warning"
                    line["notes"].append(f"Interacts with {other}")
                    self._add_flag("warning", f"{entry['name']} interacts with {other}.", "check_drug")
        self.prescriptions.append(line)
        return {**line, "formulary_entry": entry, "patient_allergies": allergies}

    def draft_mc(self, days, reason, start_date=None):
        days = int(days)
        start = dt.date.fromisoformat(start_date or self.visit_date)
        end = start + dt.timedelta(days=max(days, 1) - 1)
        name = self.patient.get("name", "the patient")
        ic = self.patient.get("ic")
        text = (
            f"This is to certify that {name}{f' (IC {ic})' if ic else ''} was examined on {self.visit_date} "
            f"and is unfit for duty for {days} day{'s' if days != 1 else ''}, "
            f"from {start.isoformat()} to {end.isoformat()} inclusive, due to {reason}."
        )
        self.mc = {"days": days, "start_date": start.isoformat(), "end_date": end.isoformat(), "reason": reason, "text": text}
        panel = self._panel_rules()
        if panel and panel["max_mc_days"] is not None and days > panel["max_mc_days"]:
            self._add_flag("warning", f"MC of {days} days exceeds {panel['name']} limit of {panel['max_mc_days']} days, needs pre-approval.", "draft_mc")
        return self.mc

    def write_soap_note(self, subjective, objective, assessment, plan, diagnoses=None):
        self.soap = {
            "subjective": subjective,
            "objective": objective,
            "assessment": assessment,
            "plan": plan,
            "diagnoses": diagnoses or [],
        }
        return {"saved": True}

    def flag(self, severity, message):
        if severity not in ("info", "warning", "critical"):
            severity = "warning"
        self._add_flag(severity, message, "model")
        return {"flagged": True}

    # --- dispatch ----------------------------------------------------------

    def call(self, name, arguments):
        fn = {
            "get_panel_rules": self.get_panel_rules,
            "check_drug": self.check_drug,
            "draft_mc": self.draft_mc,
            "write_soap_note": self.write_soap_note,
            "flag": self.flag,
        }.get(name)
        if fn is None:
            result = {"error": f"Unknown tool {name}"}
        else:
            try:
                result = fn(**arguments)
            except TypeError as e:
                result = {"error": f"Bad arguments for {name}: {e}"}
        self.tool_log.append({"name": name, "arguments": arguments, "ok": "error" not in result})
        return result
