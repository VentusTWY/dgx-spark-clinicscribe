"""End-to-end test of the agent loop against a fake OpenAI-compatible server.

Run: python3 -m unittest discover tests
No GPU or vLLM needed; the fake server replays the tool calls Lightning would make.
"""

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from agent import LLMClient, run_agent, run_agent_on_segments
from agent.__main__ import load_input
from agent.segments import heuristic_roles, load_segments

ROOT = Path(__file__).resolve().parent.parent


def tool_call(i, name, args):
    return {"id": f"call_{i}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


SCRIPT = [
    [tool_call(1, "get_panel_rules", {"panel_id": "PANEL-AIA"})],
    [
        tool_call(2, "check_drug", {"drug": "paracetamol", "dose": "1 g", "frequency": "QID PRN", "dose_mg": 1000, "times_per_day": 4}),
        tool_call(3, "check_drug", {"drug": "cetirizine", "dose": "10 mg", "frequency": "ON"}),
        tool_call(4, "check_drug", {"drug": "Augmentin", "dose": "625 mg", "frequency": "BD"}),
    ],
    [tool_call(5, "draft_mc", {"days": 3, "reason": "Upper respiratory tract infection"})],
    [tool_call(6, "write_soap_note", {
        "subjective": "Sore throat, fever, cough 2 days. Penicillin allergy (rash).",
        "objective": "T 38.2, PR 92, BP 124/78. Injected throat, lungs clear.",
        "assessment": "Viral URTI",
        "plan": "Paracetamol, cetirizine, bromhexine. MC 2 days. Return if T>39 or breathless.",
        "diagnoses": ["Viral URTI"],
    })],
]

VERIFIER = {"passed": False, "unsupported": ["Augmentin was not prescribed"], "missing": []}


class FakeVLLM(BaseHTTPRequestHandler):
    step = 0
    tools_supported = True
    roles_answer = {"1": "Doctor", "0": "Patient"}

    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._send(200, {"data": [{"id": "nvidia/fake-lightning"}]})

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if req.get("tools") and not FakeVLLM.tools_supported:
            return self._send(400, {"error": "auto tool choice requires --enable-auto-tool-choice"})
        if req["messages"][0]["content"].startswith("These are diarized turns"):
            msg = {"role": "assistant", "content": json.dumps(FakeVLLM.roles_answer)}
        elif req["messages"][0]["content"].startswith("You are a strict"):
            msg = {"role": "assistant", "content": "<think>checking</think>```json\n" + json.dumps(VERIFIER) + "\n```"}
        elif not req.get("tools"):
            msg = {"role": "assistant", "content": json.dumps({"subjective": "s", "objective": "o", "assessment": "a", "plan": "p"})}
        elif FakeVLLM.step < len(SCRIPT):
            msg = {"role": "assistant", "content": None, "tool_calls": SCRIPT[FakeVLLM.step]}
            FakeVLLM.step += 1
        else:
            msg = {"role": "assistant", "content": "DONE"}
        self._send(200, {"choices": [{"message": msg}], "usage": {"completion_tokens": 50}})


class AgentTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), FakeVLLM)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.client_url = f"http://127.0.0.1:{cls.server.server_port}/v1"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        FakeVLLM.step = 0
        FakeVLLM.tools_supported = True
        FakeVLLM.roles_answer = {"1": "Doctor", "0": "Patient"}
        self.transcript, self.patient, self.visit_date = load_input(ROOT / "samples" / "transcript_urti.json")

    def run_it(self):
        return run_agent(self.transcript, self.patient, self.visit_date, client=LLMClient(base_url=self.client_url))

    def test_full_pipeline(self):
        out = self.run_it()
        self.assertEqual(set(out), {"soap", "prescriptions", "mc", "panel", "flags", "verification", "meta"})
        self.assertEqual(out["soap"]["assessment"], "Viral URTI")
        self.assertEqual(out["meta"]["model"], "nvidia/fake-lightning")

        rx = {p["drug"]: p for p in out["prescriptions"]}
        self.assertEqual(rx["Paracetamol"]["status"], "ok")
        self.assertEqual(rx["Amoxicillin-clavulanate"]["status"], "danger")  # penicillin allergy

        self.assertEqual(out["mc"]["end_date"], "2026-10-19")
        self.assertEqual(out["panel"]["id"], "PANEL-AIA")

        severities = [f["severity"] for f in out["flags"]]
        self.assertEqual(severities[0], "critical")
        messages = " ".join(f["message"] for f in out["flags"])
        self.assertIn("penicillin allergy", messages)
        self.assertIn("exceeds AIA Corporate Panel limit", messages)
        self.assertFalse(out["verification"]["passed"])
        json.dumps(out)  # must be serialisable

    def test_falls_back_without_tool_calling(self):
        FakeVLLM.tools_supported = False
        out = self.run_it()
        self.assertEqual(out["soap"]["plan"], "p")
        self.assertEqual(out["prescriptions"], [])
        self.assertEqual(out["panel"]["id"], "PANEL-AIA")
        self.assertTrue(any("Tool calling unavailable" in f["message"] for f in out["flags"]))


    def test_diarized_input(self):
        data = json.loads((ROOT / "samples" / "diarized_urti.json").read_text())
        data["timings"] = {"diarization_s": 2.0, "asr_s": 3.0}
        out = run_agent_on_segments(data, data["patient"], data["visit_date"], client=LLMClient(base_url=self.client_url))
        self.assertEqual(out["speakers"], {"roles": {"1": "Doctor", "0": "Patient"}, "method": "model"})
        first = out["transcript"][0]
        self.assertEqual((first["speaker"], first["speaker_id"], first["start"], first["end"]), ("Doctor", "1", 1.0, 4.5))
        self.assertEqual(out["audio"], {"file": "consult_urti.wav", "duration_s": 100.1})
        self.assertEqual(out["timings"]["diarization_s"], 2.0)
        self.assertEqual(out["timings"]["total_s"], round(5.0 + out["timings"]["agent_s"], 2))
        self.assertGreater(out["timings"]["llm_tokens"], 0)
        self.assertEqual(out["panel"]["id"], "PANEL-AIA")
        json.dumps(out)

    def test_bad_role_answer_falls_back_to_heuristic(self):
        FakeVLLM.roles_answer = {"0": "Doctor", "1": "Doctor"}
        data = json.loads((ROOT / "samples" / "diarized_urti.json").read_text())
        out = run_agent_on_segments(data, client=LLMClient(base_url=self.client_url), verify=False)
        self.assertEqual(out["speakers"], {"roles": {"1": "Doctor", "0": "Patient"}, "method": "heuristic"})


class SegmentLoaderTest(unittest.TestCase):
    def test_accepts_other_key_spellings(self):
        data = {
            "audio_file": "a.wav",
            "duration": 20,
            "breakdown": [
                {"speaker_id": "SPEAKER_3", "speaker_duration": "(5, 9)", "text": "I've had a cough."},
                {"speaker": 2, "speaker_duration": [1, 4], "text": "What brings you in? Any allergies?"},
                {"speaker": 2, "time": {"start": 10, "end": 12}, "text": " "},
            ],
        }
        segs, audio = load_segments(data)
        self.assertEqual([(s["speaker_id"], s["start"], s["end"]) for s in segs], [("2", 1.0, 4.0), ("3", 5.0, 9.0)])
        self.assertEqual(audio, {"file": "a.wav", "duration_s": 20.0})
        self.assertEqual(heuristic_roles(segs), {"2": "Doctor", "3": "Patient"})

    def test_bare_list(self):
        segs, audio = load_segments([{"speaker": 0, "start": 0, "end": 2.5, "text": "hi"}])
        self.assertEqual(audio["duration_s"], 2.5)


if __name__ == "__main__":
    unittest.main()
