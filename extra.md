# Telemetry Report Generator

Turns telemetry/event-log JSON (the `logEntry.EventDetails[]` format) into a
human-readable report, using a **local** LLM (Llama-3.2-3B-Instruct) served by
**llama.cpp**. Nothing leaves the machine — no internet call, no external API.

This doc covers two things: **(1)** building/running llama.cpp itself as your
local "AI engine" (since Ollama isn't available on your machine), and
**(2)** running and calling the Python app that generates the reports.

---

## 1. How it works

```
your JSON file
      |
      v
[ models.py ]        validates the JSON against the expected schema
      |               (Pydantic — catches malformed/missing fields early)
      v
[ report_generator.py ]
      |-- deterministic analysis first (pure Python, no AI):
      |     for each event, works out whether the EventCondition and
      |     GuardCondition are currently TRUE/FALSE, and why actionSts
      |     is what it is (e.g. "condition met but guard is blocking it")
      |
      |-- then hands those already-correct facts to the LLM and asks it
      |     to just write them up in plain English
      |         |
      |         v
      |   llama-server (llama.cpp), running Llama-3.2-3B-Instruct
      |   -> POST http://127.0.0.1:8080/v1/chat/completions
      |
      v
final report = AI narrative + a verifiable "raw facts" section underneath
```

**Why the logic is done in Python and not left to the model:** a 3B quantized
model is small and can get boolean/arithmetic reasoning wrong. So the model
is only ever asked to *phrase* facts that Python already computed — never to
figure out the facts itself. If it did the reasoning, a wrong report could
look just as confident as a correct one, which is exactly what you don't want
from a monitoring tool.

**If llama.cpp isn't running or is unreachable**, the app doesn't fail — it
falls back to a plain, template-based English summary built from the same
facts, and says so in the report header ("auto-generated fallback"). You
always get a usable report either way.

---

## 2. Repo layout

| File | What it does |
|---|---|
| `models.py` | Pydantic schema for the incoming JSON (`logEntry.EventDetails[...]`) |
| `report_generator.py` | The actual logic: analyze events, build the AI prompt, call llama.cpp, assemble the final report (with fallback) |
| `scenarios.py` | Reference catalog of 10 common spacecraft operational scenarios + rule-based matching against the telemetry (see section 10) |
| `main.py` | FastAPI app — the HTTP endpoints described below |
| `sample_telemetry.json`, `test_case_1_full_capture.json`, `test_case_2_edge_cases.json` | Test files you can use immediately |
| `requirements.txt` | Python packages needed |
| `.env.example` | Copy to `.env` to configure the llama.cpp URL, timeout, etc. |

A note on file count: earlier versions of this kept everything to 2-3 files.
`scenarios.py` breaks that on purpose — it's a reference table (which PID
prefixes and keywords mean what) that you'll want to edit per spacecraft,
so it's kept separate from the report-assembly logic in `report_generator.py`
rather than mixed into it.

---

## 3. Part A — Build llama.cpp (this replaces Ollama)

Ollama is just a friendly wrapper around the same underlying engine
(`llama.cpp`/`ggml`) — building llama.cpp yourself gets you the same local
model-running capability without needing Ollama at all.

**You need:** `git`, `cmake`, and a C/C++ compiler. On Ubuntu/Debian:

```bash
sudo apt install build-essential cmake git
```

**Clone and build:**

```bash
git clone https://github.com/ggml-org/llama.cpp
cd llama.cpp
cmake -B build
cmake --build build --config Release -j 8
```

(`-j 8` = compile using 8 cores in parallel — set it to however many cores
you have, or drop it entirely.)

When it finishes, look inside `build/bin/`. You should see:

```
llama-cli        <- interactive/one-shot CLI (this is your "ollama run")
llama-server     <- HTTP server (this is what our Python app talks to)
llama-quantize
llama-bench
...
```

If your machine truly has zero internet access (not even for `git clone`),
you'll need to build this on a machine that does have access and then copy
the whole `llama.cpp/build/bin/` folder over — the compiled binaries don't
need internet to run, only to build.

---

## 4. Part B — Get the model

You asked for Llama 3.2 3B, Q4_K_M GGUF. A widely-used pre-quantized build is:

- Repo: `bartowski/Llama-3.2-3B-Instruct-GGUF`
- File: `Llama-3.2-3B-Instruct-Q4_K_M.gguf` (~2 GB)

If you have `huggingface-cli` and internet access somewhere:

```bash
huggingface-cli download bartowski/Llama-3.2-3B-Instruct-GGUF \
  --include "Llama-3.2-3B-Instruct-Q4_K_M.gguf" \
  --local-dir ./models
```

Otherwise, download the single `.gguf` file from the Hugging Face page in a
browser and copy it onto your machine — it's one file, no extraction needed.
Put it somewhere like `llama.cpp/models/Llama-3.2-3B-Instruct-Q4_K_M.gguf`.

---

## 5. Part C — Quick CLI test (the "ollama run" equivalent)

Before wiring anything up, confirm the model actually runs, straight from
the terminal:

```bash
# one-shot prompt
./build/bin/llama-cli -m models/Llama-3.2-3B-Instruct-Q4_K_M.gguf \
  -c 4096 -p "Explain what a GGUF file is in two sentences." -n 128

# interactive chat session (keeps context between turns)
./build/bin/llama-cli -m models/Llama-3.2-3B-Instruct-Q4_K_M.gguf -c 4096 -cnv
```

**Always pass `-c` (context size) explicitly.** If you don't, llama.cpp defaults
to the model's full training context — 131,072 tokens for Llama 3.2 — and the
KV cache for that alone can need 10+ GB of RAM, which will get the process
killed by the OOM killer on most machines. `-c 4096` is plenty for this use
case and keeps memory use well under 1 GB for the KV cache.

If that gives you a sensible reply, the model and build are good, and
everything from here on is just about wiring it up to your Python app.

---

## 6. Part D — Start the llama.cpp server

The Python app talks over HTTP, so instead of `llama-cli`, keep this
running in a terminal (or as a background service):

```bash
./build/bin/llama-server \
  -m models/Llama-3.2-3B-Instruct-Q4_K_M.gguf \
  -c 4096 \
  --port 8080
```

Sanity check it in another terminal:

```bash
curl http://127.0.0.1:8080/health

curl http://127.0.0.1:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"local","messages":[{"role":"user","content":"Hello"}]}'
```

Leave this running — it's the "AI engine" the Python app calls into.

---

## 7. Part E — Install Python deps & configure

```bash
pip install -r requirements.txt
cp .env.example .env
```

Open `.env` if you need to change anything (defaults assume llama-server is
on `http://127.0.0.1:8080`, which matches Part D above):

```
LLAMA_SERVER_URL=http://127.0.0.1:8080
REQUEST_TIMEOUT_S=120     # CPU inference can be slow — raise this if you see timeouts
MAX_TOKENS=700
TEMPERATURE=0.3
```

---

## 8. Part F — Run the app

```bash
python main.py
```

or

```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```

You should see `Uvicorn running on http://0.0.0.0:8000`. Interactive API
docs are auto-generated at `http://localhost:8000/docs`.

---

## 9. Sending JSON / using the API

### Check everything is wired up

```bash
curl http://localhost:8000/health
```

```json
{
  "service": "ok",
  "llama_cpp_server": "http://127.0.0.1:8080",
  "llama_cpp_reachable": true
}
```

If `llama_cpp_reachable` is `false`, Part D's server isn't running or isn't
reachable at that URL — the app will still work, just with the fallback
narrative instead of the AI one.

### Option 1 — point it at a JSON file already on disk

Use this when your telemetry system writes a log file somewhere and you
want a report for that file:

```bash
curl -X POST "http://localhost:8000/analyze" \
  -H "Content-Type: application/json" \
  -d '{"path": "sample_telemetry.json", "source": "PLC line 3 log"}'
```

- `path` — file path, absolute or relative to where you ran `main.py`
- `source` *(optional)* — a label for where the data came from; shown at
  the top of the report. Defaults to the file path if omitted.

### Option 2 — send the JSON directly in the request body

Use this when another system (or a script) already has the JSON in memory
and wants a report back without writing a file first:

```bash
curl -X POST "http://localhost:8000/analyze/json?source=live-feed" \
  -H "Content-Type: application/json" \
  --data @sample_telemetry.json
```

### Getting plain text instead of JSON

Both endpoints default to returning a JSON envelope
(`{"report": "...", "source": ..., "events_analyzed": ..., "llm_used": ...}`).
Add `?plain=true` to get just the report text back, ready to save to a
`.txt` file or paste into an email:

```bash
curl -X POST "http://localhost:8000/analyze?plain=true" \
  -H "Content-Type: application/json" \
  -d '{"path": "sample_telemetry.json"}'
```

### What the report looks like

```
TELEMETRY REPORT
=================
Source           : sample_telemetry.json
Process          : EventBG
Log timestamp    : 2025-10-06 03:54:50.413
Generated at     : 2026-07-21 06:38:43 UTC
Narrative source : AI-generated (Llama 3.2 3B via llama.cpp)

AT A GLANCE
-----------
Events analyzed             : 3
Conditions currently TRUE   : 2
Guards currently blocking   : 1
Actions currently active    : 0
Monitoring health issues    : 0
Incomplete sample sets      : 2
Scenarios flagged           : 3

SITUATION SUMMARY
-----------------
<one AI-written paragraph tying together the counts above, any
attention items, and any matched scenarios -- the quick-read part>

ATTENTION ITEMS
---------------
- EV003 ("Event 3"): ready to trigger but waiting on samples (5/7)

MATCHED SCENARIOS
------------------
EPS (Electrical Power System) anomaly
    EV001 ("Event 1") - matched on PID "PWR05013"  <-- currently active
    EV002 ("Event 2") - matched on PID "PWR05001", PID "PWR05013"
    ...

EVENT DETAILS (raw facts, for verification)
---------------------------------------------
Event EV001 - "Event 1" (Test Event 1)
  Action script       : Event1test.tst
  ...

REFERENCE: SCENARIO CATALOG (10 common spacecraft ops scenarios)
-------------------------------------------------------------------
1. EPS (Electrical Power System) anomaly - ...
2. Battery / eclipse power management issue - ...
...
```

**Reading order, top to bottom:** AT A GLANCE for a 5-second read, SITUATION
SUMMARY for the AI's plain-English take, ATTENTION ITEMS for anything that
specifically needs eyes on it, MATCHED SCENARIOS for which of the 10
reference scenarios this snapshot resembles, EVENT DETAILS for the full
verifiable numbers behind everything above, and the CATALOG as a
self-contained reference so the report doesn't depend on external docs.

---

## 10. Scenario matching

`scenarios.py` ships a reference catalog of **10 common spacecraft
operational scenarios** — the standard subsystem/ops categories that apply
broadly across spacecraft: EPS (power), battery/eclipse, AOCS/ADCS
(attitude & orbit control), thermal, TT&C/comms, propulsion, payload, OBC/
data-handling, safe-mode entry, and ground-segment/command-link.

For every event in the telemetry, the code looks at the PID tags referenced
in its `EventCondition`/`GuardCondition` (e.g. `PID("PWR05013")` → prefix
`PWR`) and a keyword fallback on its name/description, and matches them
against that catalog. This is **rule-based, not AI** — the same reasoning as
everywhere else in this project: a small model guessing at a fault
classification can sound confident and be wrong, so the matching happens in
plain Python from things that are actually in the data, and the LLM is only
ever allowed to *mention* what already matched, never invent a new one.

**The one thing you'll need to edit for your spacecraft:** `PID_PREFIX_MAP`
in `scenarios.py`. It's currently a best guess based on the PID prefixes
seen in your sample data (`PWR`→EPS, `AOC`→AOCS, `TMP`→thermal, `TCP`→
comms — that last one is a genuine guess, worth checking against your ICD).
Add or correct prefixes there to match your actual PID naming convention;
nothing else in the file needs to change. `SCENARIOS` itself (the 10-item
list) and `KEYWORD_MAP` are also just plain Python data if you want to
add an 11th scenario or reword anything.

---

## 11. Troubleshooting

| Symptom | Likely cause |
|---|---|
| `llama_cpp_reachable: false` on `/health` | `llama-server` (Part D) isn't running, or is on a different port than `LLAMA_SERVER_URL` in `.env` |
| Request to `/analyze` hangs a long time then times out | Normal for CPU-only 3B inference on first load — raise `REQUEST_TIMEOUT_S` in `.env` |
| `422` response from `/analyze` or `/analyze/json` | The JSON doesn't match the expected schema — the response body lists exactly which field failed |
| `404 File not found` from `/analyze` | Check the `path` is correct relative to wherever you launched `python main.py` from |
| Report says "auto-generated fallback" | llama.cpp server wasn't reachable at the time — the facts are still correct, you just don't get the AI-written prose |

---

## 12. If your real JSON differs from `sample_telemetry.json`

`models.py` is intentionally lenient (`extra="allow"`) — unknown fields
are kept, not rejected. If a *required* field is missing or a type doesn't
match (e.g. `noOfSamples` isn't a number), you'll get a `422` with the exact
field name, so it's easy to adjust `models.py` to match your real data.
