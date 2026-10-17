"""Accept the diarization + ASR output (first layer) and run the agent on it.

First-layer output looks like:
  {"wav": "consult.wav", "total_duration": 164.0,
   "segments": [{"speaker": 0, "start": 1.0, "end": 10.0, "text": "..."}, ...]}

Key names vary between tools, so the loader accepts common spellings
(see SEGMENT_LIST_KEYS and _segment). Speaker ids (0-8) are mapped to roles
(Doctor / Patient / ...) by Lightning from what each speaker says, with a
keyword heuristic as fallback.
"""

import re
import time

from .agent import run_agent
from .llm import LLMClient, LLMError, parse_json_loose

SEGMENT_LIST_KEYS = ("segments", "breakdown", "turns", "utterances", "results")
ROLES = ("Doctor", "Patient", "Nurse", "Caregiver", "Other")

ROLE_PROMPT = """These are diarized turns from a GP consultation recording. Speaker ids come from diarization.
Decide each speaker's role. Allowed roles: Doctor, Patient, Nurse, Caregiver, Other.
The doctor asks clinical questions, examines, diagnoses and prescribes. Usually there is exactly one Doctor and one Patient.
Respond with JSON only, mapping every speaker id to a role, e.g. {"0": "Doctor", "1": "Patient"}."""

DOCTOR_CUES = re.compile(
    r"\b(i'll give you|i will give you|prescrib|let me (check|examine|listen)|any (allergies|drug allergies|fever)|"
    r"what brings you|temperature is|blood pressure|take (this|it)|times a day|mc for|come back if|diagnos|looks like)\b",
    re.I,
)


# --- loading ----------------------------------------------------------------

def _speaker_id(raw):
    if raw is None:
        return "0"
    m = re.search(r"\d+", str(raw))
    return m.group(0) if m else str(raw)


def _times(seg):
    start, end = seg.get("start", seg.get("start_time")), seg.get("end", seg.get("end_time"))
    if start is None or end is None:
        span = seg.get("speaker_duration", seg.get("duration", seg.get("time", seg.get("timestamp"))))
        if isinstance(span, dict):
            start, end = span.get("start"), span.get("end")
        elif isinstance(span, (list, tuple)) and len(span) == 2:
            start, end = span
        elif isinstance(span, str):
            nums = re.findall(r"[\d.]+", span)
            if len(nums) == 2:
                start, end = nums
    return float(start or 0), float(end or start or 0)


def _segment(seg):
    start, end = _times(seg)
    speaker = seg.get("speaker", seg.get("speaker_id", seg.get("spk", seg.get("speaker_label"))))
    return {"speaker_id": _speaker_id(speaker), "start": round(start, 2), "end": round(end, 2), "text": str(seg.get("text", "")).strip()}


def load_segments(data):
    """Return (segments, audio_info) from first-layer output (dict or bare list)."""
    if isinstance(data, list):
        raw, top = data, {}
    else:
        top = data
        raw = next((data[k] for k in SEGMENT_LIST_KEYS if isinstance(data.get(k), list)), [])
    segments = sorted((_segment(s) for s in raw), key=lambda s: s["start"])
    segments = [s for s in segments if s["text"]]
    duration = top.get("total_duration", top.get("duration", top.get("audio_s", top.get("duration_s"))))
    audio = {
        "file": top.get("wav") or top.get("file") or top.get("audio") or top.get("filename") or top.get("audio_file"),
        "duration_s": float(duration) if duration is not None else (segments[-1]["end"] if segments else 0.0),
    }
    return segments, audio


def is_segment_input(data):
    return isinstance(data, list) or any(isinstance(data.get(k), list) for k in SEGMENT_LIST_KEYS)


# --- speaker roles ----------------------------------------------------------

def _clock(seconds):
    m, s = divmod(int(seconds), 60)
    return f"{m:02d}:{s:02d}"


def heuristic_roles(segments):
    """Most doctor-like speaker is the Doctor, the most talkative of the rest is the Patient."""
    ids = list(dict.fromkeys(s["speaker_id"] for s in segments))
    cues = {i: 0 for i in ids}
    words = {i: 0 for i in ids}
    for s in segments:
        cues[s["speaker_id"]] += len(DOCTOR_CUES.findall(s["text"])) + s["text"].count("?")
        words[s["speaker_id"]] += len(s["text"].split())
    roles = {}
    if ids:
        doctor = max(ids, key=lambda i: (cues[i], -ids.index(i)))
        roles[doctor] = "Doctor"
        rest = sorted((i for i in ids if i != doctor), key=lambda i: -words[i])
        if rest:
            roles[rest[0]] = "Patient"
        for i in rest[1:]:
            roles[i] = f"Speaker {i}"
    return roles


def label_speakers(client, segments):
    """Ask Lightning who is who; fall back to the heuristic for anything it misses."""
    fallback = heuristic_roles(segments)
    lines = "\n".join(f"[speaker {s['speaker_id']}] {s['text']}" for s in segments[:60])
    try:
        msg = client.chat([{"role": "system", "content": ROLE_PROMPT}, {"role": "user", "content": lines}], temperature=0.0)
        answer = parse_json_loose(msg.get("content"))
        roles = {_speaker_id(k): v for k, v in answer.items() if v in ROLES}
    except (LLMError, ValueError, AttributeError):
        return fallback, "heuristic"
    if list(roles.values()).count("Doctor") != 1:
        return fallback, "heuristic"
    for sid, role in fallback.items():
        roles.setdefault(sid, role if role not in roles.values() else f"Speaker {sid}")
    return roles, "model"


def render_transcript(segments, roles):
    return "\n".join(
        f"[{_clock(s['start'])}-{_clock(s['end'])}] {roles.get(s['speaker_id'], 'Speaker ' + s['speaker_id'])}: {s['text']}"
        for s in segments
    )


# --- entry point ------------------------------------------------------------

def run_agent_on_segments(data, patient=None, visit_date=None, client=None, verify=True):
    """Run the agent on first-layer output. Returns the agent contract plus
    transcript, speakers, audio and timings (see docs/agent_contract.md)."""
    client = client or LLMClient()
    started = time.time()
    segments, audio = load_segments(data)
    if not segments:
        raise ValueError("No segments with text found in the input")
    roles, method = label_speakers(client, segments)

    result = run_agent(render_transcript(segments, roles), patient, visit_date, client=client, verify=verify)

    agent_s = round(time.time() - started, 2)
    upstream = data.get("timings", {}) if isinstance(data, dict) else {}
    result["transcript"] = [
        {"speaker": roles.get(s["speaker_id"], f"Speaker {s['speaker_id']}"), "speaker_id": s["speaker_id"],
         "start": s["start"], "end": s["end"], "text": s["text"]}
        for s in segments
    ]
    result["speakers"] = {"roles": roles, "method": method}
    result["audio"] = audio
    result["timings"] = {
        **upstream,
        "agent_s": agent_s,
        "audio_s": audio["duration_s"],
        "llm_tokens": client.completion_tokens,
    }
    if "diarization_s" in upstream or "asr_s" in upstream:
        result["timings"]["total_s"] = round(upstream.get("diarization_s", 0) + upstream.get("asr_s", 0) + agent_s, 2)
    return result
