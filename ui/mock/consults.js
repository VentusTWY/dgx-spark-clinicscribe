// Mock consults used by MockApi. Each entry is a finished result in the
// output JSON contract (see CLAUDE.md), minus job_id and timings, which the
// simulator fills in. The hero consult carries the planted error:
// a 3-day MC on Panel A, whose limit is 2 days.
window.MOCK_CONSULTS = [
  {
    name: "hero_consult_panelA.wav",
    panel: "Panel A",
    audio_s: 164,
    transcript: [
      { speaker: "Doctor", start: 0.0, end: 3.4, text: "Good morning, come in. What brings you in today?" },
      { speaker: "Patient", start: 3.8, end: 10.2, text: "Morning doctor. I've had a fever and a really sore throat since Monday, and I'm coughing a lot at night." },
      { speaker: "Doctor", start: 10.6, end: 14.1, text: "Okay. Any shortness of breath, chest pain, or rashes?" },
      { speaker: "Patient", start: 14.5, end: 18.9, text: "No chest pain. Just tired, and my body is aching all over." },
      { speaker: "Doctor", start: 19.3, end: 23.0, text: "Have you taken anything for it so far?" },
      { speaker: "Patient", start: 23.4, end: 29.8, text: "Some Panadol, two tablets, maybe four times a day. It helps a bit with the fever." },
      { speaker: "Doctor", start: 30.2, end: 34.6, text: "Any allergies to medicine that you know of?" },
      { speaker: "Patient", start: 35.0, end: 36.4, text: "No, none." },
      { speaker: "Doctor", start: 36.8, end: 44.9, text: "Let me check. Temperature is 38.4, throat is red with no pus on the tonsils, chest is clear, oxygen is 98 percent." },
      { speaker: "Patient", start: 45.3, end: 48.0, text: "Is it the flu? My colleague had it last week." },
      { speaker: "Doctor", start: 48.4, end: 58.7, text: "It looks like a viral upper respiratory infection, likely flu-like. Antibiotics won't help here. I'll give you paracetamol, lozenges and a cough syrup." },
      { speaker: "Patient", start: 59.1, end: 63.5, text: "Okay. Can I get an MC? I work at a warehouse and I can't really lift things like this." },
      { speaker: "Doctor", start: 63.9, end: 70.2, text: "Sure, I'll give you three days of medical leave. Drink plenty of water and rest." },
      { speaker: "Patient", start: 70.6, end: 72.8, text: "Thank you. My company uses Panel A, by the way." },
      { speaker: "Doctor", start: 73.2, end: 81.0, text: "Noted. If the fever goes above 39 or lasts beyond five days, or you get breathless, come back straight away." }
    ],
    soap: {
      S: "4-day history of fever, sore throat and nocturnal cough with myalgia and fatigue. No chest pain or shortness of breath. Self-medicating with paracetamol 1 g QID with partial relief. NKDA. Works in a warehouse (manual lifting).",
      O: "T 38.4 °C. Pharynx erythematous, no tonsillar exudate. Chest clear on auscultation. SpO2 98% on room air.",
      A: "Acute viral upper respiratory tract infection (influenza-like illness). No features of bacterial tonsillitis or lower respiratory involvement.",
      P: "Paracetamol 1 g QID PRN (max 4 g/day), lozenges, dextromethorphan cough syrup 10 ml TDS. No antibiotics indicated. Fluids and rest. MC issued. Return if T > 39 °C, symptoms > 5 days, or breathlessness."
    },
    mc: { days: 3, reason: "Acute viral URTI with fever; unable to perform manual lifting at work." },
    drug_alerts: [
      { drug: "Paracetamol", issue: "Patient already taking 1 g QID; total daily dose must stay ≤ 4 g. Advise not to combine with other paracetamol-containing products." }
    ],
    flags: [
      { issue: "MC exceeds Panel A limit (2 days)", fix: "Reduce MC to 2 days, or document a clinical reason for the extra day for panel review.", verified: true },
      { issue: "Cough syrup brand not on Panel A drug list", fix: "Use the generic dextromethorphan listed in the Panel A formulary.", verified: false }
    ],
    tool_calls: [
      { name: "get_panel_rules", args: { panel: "Panel A" }, result: { mc_limit_days: 2, referral_required_for: ["specialist", "imaging"], drug_caps: { paracetamol: "4 g/day" } } },
      { name: "check_drug", args: { name: "paracetamol", dose: "1 g QID" }, result: { ok: true, max_daily: "4 g", note: "At ceiling; avoid duplicates" } },
      { name: "check_drug", args: { name: "dextromethorphan", dose: "10 ml TDS" }, result: { ok: true, max_daily: "120 mg", interactions: ["SSRIs", "MAOIs"] } },
      { name: "draft_mc", args: { days: 3, reason: "Acute viral URTI with fever" }, result: { mc_id: "MC-0001", days: 3 } },
      { name: "flag", args: { issue: "MC exceeds Panel A limit (2 days)", fix: "Reduce to 2 days or add a reason" }, result: { recorded: true } },
      { name: "flag", args: { issue: "Cough syrup brand not on Panel A drug list", fix: "Use generic" }, result: { recorded: true } },
      { name: "write_soap_note", args: { subjective: "…", objective: "…", assessment: "…", plan: "…" }, result: { saved: true } }
    ],
    llm_tokens: 1840
  },
  {
    name: "followup_hypertension.wav",
    panel: "Panel B",
    audio_s: 128,
    transcript: [
      { speaker: "Doctor", start: 0.0, end: 3.1, text: "Hi, back for your blood pressure review?" },
      { speaker: "Patient", start: 3.5, end: 9.4, text: "Yes. I've been taking the amlodipine every morning, but my ankles are a bit swollen." },
      { speaker: "Doctor", start: 9.8, end: 12.6, text: "Any headaches, chest pain or dizziness?" },
      { speaker: "Patient", start: 13.0, end: 18.2, text: "No. But I started ibuprofen for my knee pain, maybe three times a day." },
      { speaker: "Doctor", start: 18.6, end: 26.4, text: "Your reading today is 152 over 94, so it's still high. The ibuprofen can push your pressure up." },
      { speaker: "Patient", start: 26.8, end: 28.9, text: "Oh, I didn't know that." },
      { speaker: "Doctor", start: 29.3, end: 38.0, text: "Let's stop the ibuprofen, use paracetamol for the knee, and add perindopril 4 milligrams. We'll check kidney function and recheck in four weeks." }
    ],
    soap: {
      S: "Hypertension review. Adherent to amlodipine 10 mg OM. New mild bilateral ankle swelling. Started ibuprofen ~400 mg TDS for knee pain. No headache, chest pain or dizziness.",
      O: "BP 152/94 mmHg. Mild pitting oedema both ankles.",
      A: "Suboptimally controlled essential hypertension, likely worsened by NSAID use. Amlodipine-related ankle oedema.",
      P: "Stop ibuprofen; paracetamol 1 g QID PRN for knee pain. Add perindopril 4 mg OD. Renal panel and electrolytes. Review in 4 weeks."
    },
    mc: null,
    drug_alerts: [
      { drug: "Ibuprofen", issue: "NSAID raises BP and reduces efficacy of antihypertensives; combined with ACE inhibitor increases renal risk." },
      { drug: "Perindopril", issue: "Check potassium and creatinine 1–2 weeks after starting." }
    ],
    flags: [],
    tool_calls: [
      { name: "get_panel_rules", args: { panel: "Panel B" }, result: { mc_limit_days: 3, referral_required_for: ["specialist"] } },
      { name: "check_drug", args: { name: "ibuprofen", dose: "400 mg TDS" }, result: { ok: true, interactions: ["ACE inhibitors", "antihypertensives"] } },
      { name: "check_drug", args: { name: "perindopril", dose: "4 mg OD" }, result: { ok: true, max_daily: "8 mg", monitoring: ["K+", "creatinine"] } },
      { name: "write_soap_note", args: { subjective: "…", objective: "…", assessment: "…", plan: "…" }, result: { saved: true } }
    ],
    llm_tokens: 1310
  },
  {
    name: "gastro_panelC.wav",
    panel: "Panel C",
    audio_s: 96,
    transcript: [
      { speaker: "Doctor", start: 0.0, end: 2.5, text: "What's the problem today?" },
      { speaker: "Patient", start: 2.9, end: 8.6, text: "Diarrhoea since last night, about six times. I ate at a night market." },
      { speaker: "Doctor", start: 9.0, end: 12.2, text: "Any blood in the stool, or vomiting?" },
      { speaker: "Patient", start: 12.6, end: 15.0, text: "No blood. I vomited once this morning." },
      { speaker: "Doctor", start: 15.4, end: 24.8, text: "You're mildly dehydrated. I'll give oral rehydration salts, an anti-nausea tablet, and one day of MC. I'm also referring you for an ultrasound just to be safe." }
    ],
    soap: {
      S: "Acute diarrhoea ×6 since last night after street food. One episode of vomiting. No haematochezia.",
      O: "Mild dehydration. Abdomen soft, mild diffuse tenderness, no guarding.",
      A: "Acute gastroenteritis, likely food-borne.",
      P: "ORS, domperidone 10 mg TDS PRN. 1 day MC. Ultrasound referral. Return if bloody stool or unable to keep fluids down."
    },
    mc: { days: 1, reason: "Acute gastroenteritis with dehydration." },
    drug_alerts: [],
    flags: [
      { issue: "Panel C requires pre-approval for imaging referrals", fix: "Submit pre-approval form or justify urgency in the referral letter.", verified: true }
    ],
    tool_calls: [
      { name: "get_panel_rules", args: { panel: "Panel C" }, result: { mc_limit_days: 2, preapproval_required_for: ["imaging"] } },
      { name: "check_drug", args: { name: "domperidone", dose: "10 mg TDS" }, result: { ok: true, max_daily: "30 mg" } },
      { name: "draft_mc", args: { days: 1, reason: "Acute gastroenteritis" }, result: { mc_id: "MC-0002", days: 1 } },
      { name: "flag", args: { issue: "Panel C requires pre-approval for imaging referrals", fix: "Submit pre-approval" }, result: { recorded: true } },
      { name: "write_soap_note", args: { subjective: "…", objective: "…", assessment: "…", plan: "…" }, result: { saved: true } }
    ],
    llm_tokens: 1120
  },
  {
    name: "backpain.wav",
    panel: "Panel A",
    audio_s: 74,
    transcript: [
      { speaker: "Doctor", start: 0.0, end: 2.4, text: "How can I help?" },
      { speaker: "Patient", start: 2.8, end: 7.9, text: "I hurt my lower back moving boxes on Saturday. It's stiff when I bend." },
      { speaker: "Doctor", start: 8.3, end: 11.0, text: "Any numbness or weakness in the legs?" },
      { speaker: "Patient", start: 11.4, end: 12.6, text: "No, nothing like that." },
      { speaker: "Doctor", start: 13.0, end: 21.5, text: "It's a muscle strain. Keep moving gently, take naproxen with food for five days, and here's two days of MC." }
    ],
    soap: {
      S: "Lower back pain after lifting boxes 2 days ago. Stiffness on flexion. No leg numbness, weakness or red flags.",
      O: "Paraspinal tenderness L4–L5. SLR negative bilaterally. Normal power and reflexes.",
      A: "Mechanical lower back strain.",
      P: "Naproxen 500 mg BD with food × 5 days. Stay active, heat packs. 2 days MC. Return if neuro symptoms."
    },
    mc: { days: 2, reason: "Acute mechanical lower back strain." },
    drug_alerts: [],
    flags: [],
    tool_calls: [
      { name: "get_panel_rules", args: { panel: "Panel A" }, result: { mc_limit_days: 2 } },
      { name: "check_drug", args: { name: "naproxen", dose: "500 mg BD" }, result: { ok: true, max_daily: "1000 mg" } },
      { name: "draft_mc", args: { days: 2, reason: "Mechanical lower back strain" }, result: { mc_id: "MC-0003", days: 2 } },
      { name: "write_soap_note", args: { subjective: "…", objective: "…", assessment: "…", plan: "…" }, result: { saved: true } }
    ],
    llm_tokens: 960
  }
];
