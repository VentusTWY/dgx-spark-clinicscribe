"""ClinicScribe agent: transcript in, structured visit record out.

Flow:
  1. Agent loop: Lightning reads the transcript and calls tools
     (get_panel_rules, check_drug, draft_mc, write_soap_note, flag) until done.
  2. Verifier pass: a second, separate call re-reads the transcript against
     the draft and lists anything unsupported or missing.
  3. Assemble the output JSON contract (see docs/agent_contract.md).
"""

import json
import time

from .llm import LLMClient, LLMError
from .tools import TOOL_SCHEMAS, Workspace

MAX_STEPS = 12

SYSTEM_PROMPT = """You are ClinicScribe, a clinical documentation assistant for a Malaysian GP clinic.
You receive a doctor-patient consultation transcript and the patient's details.
Use the tools to build the visit record. Work in this order:
1. If the patient has a panel_id, call get_panel_rules.
2. Call check_drug once for EACH medication the doctor prescribed (not ones the patient took at home).
3. If the doctor agreed to an MC, call draft_mc with the days the doctor said.
4. Call flag for anything the doctor must review: allergy conflicts, missing vitals, red flags, unclear doses.
5. Call write_soap_note last.
Rules: only use facts stated in the transcript. Never invent vitals, doses or diagnoses.
When you have called write_soap_note, reply with the single word DONE."""

VERIFIER_PROMPT = """You are a strict clinical documentation verifier.
Compare the DRAFT visit record against the TRANSCRIPT.
Report:
- unsupported: statements in the draft that are NOT supported by the transcript (hallucinations)
- missing: clinically important facts in the transcript that the draft omits (e.g. allergies, return precautions, vitals)
Respond with JSON only, exactly this shape:
{"passed": true|false, "unsupported": ["..."], "missing": ["..."]}
passed is false if anything is unsupported or a safety-relevant fact is missing."""


def _user_message(transcript, patient, visit_date):
    return (
        f"Visit date: {visit_date}\n"
        f"Patient: {json.dumps(patient, ensure_ascii=False)}\n\n"
        f"TRANSCRIPT:\n{transcript}"
    )


def _run_tool_loop(client, ws, transcript):
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": _user_message(transcript, ws.patient, ws.visit_date)},
    ]
    for _ in range(MAX_STEPS):
        msg = client.chat(messages, tools=TOOL_SCHEMAS)
        calls = msg.get("tool_calls") or []
        messages.append({"role": "assistant", "content": msg.get("content") or "", **({"tool_calls": calls} if calls else {})})
        if not calls:
            if ws.soap is None:
                # The model stopped without writing the note; nudge it once more.
                messages.append({"role": "user", "content": "You have not called write_soap_note yet. Call it now."})
                continue
            break
        for call in calls:
            fn = call.get("function", {})
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            result = ws.call(fn.get("name", ""), args)
            messages.append({
                "role": "tool",
                "tool_call_id": call.get("id", ""),
                "name": fn.get("name", ""),
                "content": json.dumps(result, ensure_ascii=False, default=str),
            })
        if ws.soap is not None and all(c.get("function", {}).get("name") == "write_soap_note" for c in calls):
            break


def _fallback_soap(client, ws, transcript):
    """If the server has no tool calling enabled, ask for the SOAP note as plain JSON."""
    prompt = (
        "Write a SOAP note for this consultation. Only use facts from the transcript. "
        'Respond with JSON only: {"subjective":"","objective":"","assessment":"","plan":"","diagnoses":[]}\n\n'
        + _user_message(transcript, ws.patient, ws.visit_date)
    )
    data = client.chat_json([{"role": "user", "content": prompt}])
    ws.write_soap_note(**{k: data.get(k, "") for k in ("subjective", "objective", "assessment", "plan")}, diagnoses=data.get("diagnoses", []))
    ws._add_flag("info", "Tool calling unavailable; drug, MC and panel checks were skipped.", "agent")


def _verify(client, ws, transcript):
    draft = {"soap": ws.soap, "prescriptions": ws.prescriptions, "mc": ws.mc}
    try:
        v = client.chat_json([
            {"role": "system", "content": VERIFIER_PROMPT},
            {"role": "user", "content": f"TRANSCRIPT:\n{transcript}\n\nDRAFT:\n{json.dumps(draft, ensure_ascii=False)}"},
        ])
        result = {
            "passed": bool(v.get("passed")),
            "unsupported": list(v.get("unsupported") or []),
            "missing": list(v.get("missing") or []),
        }
    except (ValueError, AttributeError) as e:
        result = {"passed": False, "unsupported": [], "missing": [], "error": f"Verifier did not return JSON: {e}"}
    for item in result["unsupported"]:
        ws._add_flag("warning", f"Not supported by transcript: {item}", "verifier")
    for item in result["missing"]:
        ws._add_flag("info", f"Possibly missing: {item}", "verifier")
    return result


def run_agent(transcript, patient=None, visit_date=None, client=None, verify=True):
    """Run the full pipeline and return the output contract as a dict."""
    client = client or LLMClient()
    ws = Workspace(patient=patient, visit_date=visit_date)
    started = time.time()

    try:
        _run_tool_loop(client, ws, transcript)
    except LLMError as e:
        # vLLM returns 400 when the server was started without tool calling.
        if "HTTP 400" not in str(e):
            raise
        ws.tool_log.append({"name": "tool_calling_unavailable", "arguments": {}, "ok": False})
    if ws.soap is None:
        _fallback_soap(client, ws, transcript)
    if ws.panel is None and ws.patient.get("panel_id"):
        ws.get_panel_rules(ws.patient["panel_id"])

    verification = _verify(client, ws, transcript) if verify else None

    order = {"critical": 0, "warning": 1, "info": 2}
    return {
        "soap": ws.soap,
        "prescriptions": ws.prescriptions,
        "mc": ws.mc,
        "panel": ws.panel,
        "flags": sorted(ws.flags, key=lambda f: order.get(f["severity"], 3)),
        "verification": verification,
        "meta": {
            "model": client.model,
            "base_url": client.base_url,
            "visit_date": ws.visit_date,
            "latency_ms": int((time.time() - started) * 1000),
            "tool_calls": ws.tool_log,
        },
    }
