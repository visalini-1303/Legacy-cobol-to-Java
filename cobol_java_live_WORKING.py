#!/usr/bin/env python3
"""Live COBOL-to-Java three-agent UI. Requires Python 3.9+, a JDK (javac and java),
and GROQ_API_KEY in your environment. No Python packages required.

Run: python cobol_java_live.py
Open: http://127.0.0.1:8765
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
import webbrowser
from decimal import Decimal, ROUND_HALF_UP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Timer

MODEL = "openai/gpt-oss-120b"
PORT = 8765

SAMPLES = [
    {
        "title": "Savings interest and penalty",
        "cobol": """IDENTIFICATION DIVISION.
PROGRAM-ID. ACCOUNT-INTEREST.
DATA DIVISION.
WORKING-STORAGE SECTION.
01 WS-ACCOUNT-NUM PIC 9(10).
01 WS-BALANCE PIC 9(10)V99.
01 WS-INTEREST-RATE PIC 9V99 VALUE 0.06.
01 WS-INTEREST-AMOUNT PIC 9(10)V99.
01 WS-ACCOUNT-TYPE PIC X(10).
01 WS-MIN-BALANCE PIC 9(10)V99 VALUE 1000.00.
01 WS-PENALTY PIC 9(10)V99 VALUE 5.00.
PROCEDURE DIVISION.
MAIN-PARA.
    MOVE 1234567890 TO WS-ACCOUNT-NUM.
    MOVE 5000.00 TO WS-BALANCE.
    MOVE 'SAVINGS' TO WS-ACCOUNT-TYPE.
    PERFORM CALCULATE-INTEREST.
    PERFORM APPLY-PENALTY.
    PERFORM UPDATE-ACCOUNT.
    DISPLAY 'ACCOUNT: ' WS-ACCOUNT-NUM.
    DISPLAY 'FINAL BALANCE: ' WS-BALANCE.
    STOP RUN.
CALCULATE-INTEREST.
    COMPUTE WS-INTEREST-AMOUNT =
        WS-BALANCE * WS-INTEREST-RATE.
    ADD WS-INTEREST-AMOUNT TO WS-BALANCE.
APPLY-PENALTY.
    IF WS-BALANCE < WS-MIN-BALANCE
        SUBTRACT WS-PENALTY FROM WS-BALANCE
        DISPLAY 'PENALTY APPLIED'
    END-IF.
UPDATE-ACCOUNT.
    DISPLAY 'UPDATING ACCOUNT RECORD'.
    CONTINUE.""",
        "expected": lambda: [
            "UPDATING ACCOUNT RECORD", "ACCOUNT: 1234567890",
            "FINAL BALANCE: " + f"{(Decimal('5000.00') * Decimal('1.06')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):012.2f}",
        ],
        "notes": "Java main must accept args[0] account number, args[1] balance, args[2] account type. Use the supplied values 1234567890, 5000.00, SAVINGS. Use BigDecimal, HALF_UP to two decimals. Print account as ten digits, final balance as twelve characters zero-padded including decimal point. Print UPDATING ACCOUNT RECORD before account and balance. A penalty of 5.00 applies if the post-interest balance is below 1000.00.",
        "args": ["1234567890", "5000.00", "SAVINGS"],
    },
    {
        "title": "Currency conversion (ROUNDED)",
        "cobol": """IDENTIFICATION DIVISION.
PROGRAM-ID. CURRENCY-CONVERSION.
DATA DIVISION.
WORKING-STORAGE SECTION.
01 WS-USD PIC 9(8)V99 VALUE 10.15.
01 WS-RATE PIC 9(3)V99 VALUE 83.25.
01 WS-INR PIC 9(9)V99.
PROCEDURE DIVISION.
MAIN-PARA.
    COMPUTE WS-INR ROUNDED = WS-USD * WS-RATE.
    DISPLAY 'INR: ' WS-INR.
    STOP RUN.""",
        "expected": lambda: ["INR: " + f"{(Decimal('10.15') * Decimal('83.25')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):012.2f}"],
        "notes": "Use BigDecimal and HALF_UP because COMPUTE uses ROUNDED. Inputs are the COBOL literals 10.15 USD and 83.25 INR per USD. Output exactly INR: followed by nine zero-padded integral digits and two decimal places (PIC 9(9)V99, 12 characters total).", 
        "args": [],
    },
    {
        "title": "Loyalty points (drop the remainder)",
        "cobol": """IDENTIFICATION DIVISION.
PROGRAM-ID. LOYALTY-POINTS.
DATA DIVISION.
WORKING-STORAGE SECTION.
01 WS-SPENT PIC 9(8)V99 VALUE 4599.00.
01 WS-PTS PIC 9(4).
PROCEDURE DIVISION.
MAIN-PARA.
    DIVIDE WS-SPENT BY 100 GIVING WS-PTS.
    DISPLAY 'POINTS: ' WS-PTS.
    STOP RUN.""",
        "expected": lambda: ["POINTS: " + f"{int(Decimal('4599.00') // Decimal('100')):04d}"],
        "notes": "Divide 4599.00 by 100; PIC 9(4) discards fractional points. Print exactly POINTS: 0045. No args required.",
        "args": [],
    },
]

def clean_text(text):
    text = re.sub(r"```(?:[a-zA-Z]+)?\s*|```", "", text)
    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text, flags=re.S)
    text = re.sub(r"(?m)^\s*#{1,6}\s*", "", text)
    return text.strip()


def ask_groq(messages):
    key = os.environ.get("GROQ_API_KEY", "").strip()
    if not key:
        raise RuntimeError("GROQ_API_KEY is not set. Set it in your environment, then restart this program.")
    data = json.dumps({"model": MODEL, "messages": messages, "temperature": 0.1}).encode()
    request = urllib.request.Request(
        "https://api.groq.com/openai/v1/chat/completions", data=data,
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json", "User-Agent": "COBOL-Java-Modernizer/1.0", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        details = exc.read(4096).decode("utf-8", "replace")
        print("\n===== GROQ ERROR DETAILS =====")
        print("HTTP status:", exc.code)
        print(details)
        print("==============================\n")
        try:
            error_data = json.loads(details)
            message = error_data.get("error", {}).get("message", details or "Request failed")
        except (ValueError, AttributeError):
            message = details or "Request failed"
        raise RuntimeError(f"Groq returned HTTP {exc.code}: {message}") from None
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not reach Groq: {exc.reason}") from None
    try:
        content = result["choices"][0]["message"]["content"]
        if not content:
            raise ValueError("empty response")
        return content
    except (KeyError, IndexError, TypeError, ValueError):
        raise RuntimeError("Groq returned no usable answer") from None


def validate_cobol(code):
    declared = re.findall(r"(?mi)^\s*\d{2}\s+(WS-[A-Z0-9-]+)\s+PIC\s+", code)
    if "PROCEDURE DIVISION." not in code or not declared:
        raise ValueError("COBOL is missing a PROCEDURE DIVISION or PIC declarations")
    if len(set(declared)) != len(declared):
        raise ValueError("COBOL has duplicate variable declarations")
    references = set(re.findall(r"\bWS-[A-Z0-9-]+\b", code.split("PROCEDURE DIVISION.", 1)[1]))
    unknown = references - set(declared)
    if unknown:
        raise ValueError("Undefined COBOL variables: " + ", ".join(sorted(unknown)))
    return len(declared)


def extract_java(answer):
    match = re.search(r"```(?:java)?\s*([\s\S]*?)```", answer, re.I)
    code = match.group(1) if match else answer
    return code.strip().lstrip("\ufeff")


def run_java(code, args):
    if not shutil.which("javac") or not shutil.which("java"):
        return False, "Java JDK not found. Install a JDK (17 or newer) and put javac and java on PATH."
    main = re.search(r"\bpublic\s+(?:final\s+)?class\s+(\w+)", code)
    if not main:
        main = re.search(r"\bclass\s+(\w+)", code)
    if not main:
        return False, "No Java class found. Include one public class and a main method."
    class_name = main.group(1)
    with tempfile.TemporaryDirectory(prefix="cobol_java_") as directory:
        path = Path(directory) / (class_name + ".java")
        path.write_text(code, encoding="utf-8")
        try:
            compiled = subprocess.run(["javac", "-encoding", "UTF-8", "-d", directory, str(path)],
                                      capture_output=True, text=True, timeout=60)
            if compiled.returncode:
                return False, "Compilation failed:\n" + compiled.stderr[-3500:]
            executed = subprocess.run(["java", "-cp", directory, class_name, *args],
                                      capture_output=True, text=True, timeout=30)
            if executed.returncode:
                return False, "Java failed:\n" + executed.stderr[-3500:]
            return True, executed.stdout
        except subprocess.TimeoutExpired:
            return False, "Java compilation or execution timed out"
        except OSError as exc:
            return False, "Could not run Java: " + str(exc)


def compare(expected, output):
    actual = output.splitlines()
    if actual == expected:
        return True, "COBOL: " + "\nCOBOL: ".join(expected) + "\nJava:  " + "\nJava:  ".join(actual), ""
    differences = []
    for i in range(max(len(expected), len(actual))):
        a = expected[i] if i < len(expected) else "<no line>"
        b = actual[i] if i < len(actual) else "<no line>"
        if a != b:
            differences.append(f"Line {i+1}: COBOL expected: {a!r}; Java produced: {b!r}")
    feedback = "\n".join(differences)
    return False, "COBOL expected:\n" + "\n".join(expected) + "\nJava produced:\n" + (output or "<no output>") + "\n\n" + feedback, feedback


HTML = '<!DOCTYPE html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n<title>COBOL to Java: Extractor, Modernizer, Critic</title>\n<link rel="preconnect" href="https://fonts.googleapis.com">\n<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;700&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">\n<style>\n:root{--ink:#0d3b2c;--mut:#4f8a72;--gold:#d4a017;--a1:#e59a00;--a2:#00a0b0;--a3:#00b26b;--bad:#d0344a;--code:#f1fdf7;--rad:12px;\nbox-sizing:border-box;padding-top:env(safe-area-inset-top,0px);padding-bottom:env(safe-area-inset-bottom,0px)}\nhtml{scroll-padding-top:env(safe-area-inset-top,0px)}\n*{box-sizing:border-box}\nbody{margin:0;min-height:100vh;color:var(--ink);font:15px/1.5 "Space Grotesk",system-ui,sans-serif;\nbackground:linear-gradient(rgba(0,150,100,.14) 1px,transparent 1px) 0 0/26px 26px,linear-gradient(90deg,rgba(0,150,100,.14) 1px,transparent 1px) 0 0/26px 26px,#e6fbf1}\nmain{max-width:1100px;margin:0 auto;padding:22px 16px 40px}\nh1{font-size:28px;margin:0 0 6px;line-height:1.2}\n.sub{color:var(--mut);margin:0 0 14px}\n.bar{display:flex;flex-wrap:wrap;gap:10px;align-items:center}\nbutton{font:inherit;font-weight:700;color:var(--ink);background:#fff;border:2px solid var(--gold);border-radius:var(--rad);padding:9px 16px;cursor:pointer;box-shadow:0 2px 10px rgba(212,160,23,.25)}\nbutton:focus-visible{outline:2px solid var(--a2);outline-offset:2px}\n.which{color:var(--mut);font-size:13px}\n.src,.out{background:#fff;border:2px solid var(--gold);border-radius:var(--rad);box-shadow:0 2px 12px rgba(212,160,23,.22)}\n.src{padding:12px 14px;margin:12px 0 8px}\n.src b{font-size:13px;color:var(--mut)}\npre{margin:6px 0 0;font:12.5px/1.55 "JetBrains Mono",ui-monospace,monospace;white-space:pre-wrap;word-break:break-word}\n.status{color:var(--mut);font-size:13px;margin:6px 2px 14px;min-height:22px}\n.agents{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:18px}\n.col{display:flex;flex-direction:column;align-items:center}\n.circ{position:relative;width:132px;height:132px;transition:opacity .3s}\n.circ svg{width:100%;height:100%;overflow:visible}\n.base{fill:#fff;stroke:var(--gold);stroke-width:3}\n.ring{fill:none;stroke-width:5;stroke-linecap:round;opacity:0;transform-origin:60px 60px}\n.on .ring{opacity:1;stroke-dasharray:110 180;animation:spin 1.1s linear infinite}\n.done .ring{opacity:1;stroke-dasharray:none;animation:none}\n.idle{opacity:.5}\n.on{filter:drop-shadow(0 0 8px var(--c))}\n@keyframes spin{to{transform:rotate(360deg)}}\n@media (prefers-reduced-motion:reduce){.on .ring{animation-duration:5s}}\n.face{position:absolute;inset:0;display:grid;place-items:center;font-size:40px}\n.name{font-weight:700;font-size:18px;margin:10px 0 10px}\n.out{width:100%;min-height:150px;background:var(--code);padding:10px 12px;overflow-x:auto}\n.t{font-weight:700;font-size:12px;margin-top:8px}\n.t:first-child{margin-top:0}\n.ok{color:var(--a3)}.no{color:var(--bad)}\n.foot{margin-top:16px;color:var(--mut);font-size:13px}\n</style>\n</head>\n<body>\n<main>\n<h1>COBOL to Java: the three-agent pipeline</h1>\n<p class="sub">Live COBOL extraction, Java modernization, and output validation.</p>\n<div class="bar">\n<button id="run">Run pipeline</button>\n<button id="next">Try another COBOL code</button>\n<span class="which" id="which"></span>\n</div>\n<div class="src"><b id="stitle"></b><pre id="cob"></pre></div>\n<div class="status" id="st"></div>\n<div class="agents">\n<div class="col"><div class="circ idle" id="c0" style="--c:var(--a1)"><svg viewBox="0 0 120 120"><circle class="base" cx="60" cy="60" r="52"/><circle class="ring" cx="60" cy="60" r="52" stroke="var(--a1)"/></svg><div class="face">🔎</div></div><div class="name">Extractor</div><div class="out" id="o0"></div></div>\n<div class="col"><div class="circ idle" id="c1" style="--c:var(--a2)"><svg viewBox="0 0 120 120"><circle class="base" cx="60" cy="60" r="52"/><circle class="ring" cx="60" cy="60" r="52" stroke="var(--a2)"/></svg><div class="face">☕</div></div><div class="name">Modernizer</div><div class="out" id="o1"></div></div>\n<div class="col"><div class="circ idle" id="c2" style="--c:var(--a3)"><svg viewBox="0 0 120 120"><circle class="base" cx="60" cy="60" r="52"/><circle class="ring" cx="60" cy="60" r="52" stroke="var(--a3)"/></svg><div class="face">✅</div></div><div class="name">Critic</div><div class="out" id="o2"></div></div>\n</div>\n<p class="foot">Generated Java is compiled and checked against the COBOL reference on your computer.</p>\n</main>\n<script>\nconst $ = id => document.getElementById(id);\nlet samples = [], idx = 0, controller = null, generation = 0;\nfunction state(active, completed = []) {\n  for (let i = 0; i < 3; i++) {\n    $("c" + i).className = "circ " + (active === i ? "on" : completed.includes(i) ? "done" : "idle");\n  }\n}\nfunction heading(id, text, kind = "") {\n  const el = document.createElement("div"); el.className = "t " + kind;\n  el.textContent = text; $(id).appendChild(el);\n}\nfunction block(id, text) {\n  const el = document.createElement("pre"); el.textContent = text;\n  $(id).appendChild(el);\n}\nfunction reset() {\n  generation++;\n  if (controller) controller.abort(); controller = null;\n  ["o0","o1","o2"].forEach(id => $(id).replaceChildren());\n  state(-1); $("st").textContent = "Ready. Press Run pipeline.";\n  if (samples.length) {\n    $("stitle").textContent = "COBOL input: " + samples[idx].title;\n    $("cob").textContent = samples[idx].cobol;\n    $("which").textContent = "Sample " + (idx + 1) + " of " + samples.length;\n  }\n}\nfunction receive(e) {\n  if (e.kind === "stage") {\n    state(e.agent, Array.from({length:e.agent}, (_, i) => i));\n    $("st").textContent = e.message;\n  } else if (e.kind === "logic") {\n    heading("o0", "Extracted business logic"); block("o0", e.text);\n  } else if (e.kind === "java") {\n    heading("o1", "Attempt " + e.attempt); block("o1", e.text);\n  } else if (e.kind === "critique") {\n    heading("o2", "Attempt " + e.attempt + " comparison");\n    block("o2", e.text);\n    heading("o2", e.approved ? "APPROVED: outputs match exactly" : "REJECTED: feedback sent to Modernizer", e.approved ? "ok" : "no");\n  } else if (e.kind === "done") {\n    state(-1, e.approved ? [0,1,2] : [0,1]);\n    $("st").textContent = e.approved ? "Done. Java matches the COBOL golden reference." : "Stopped after three attempts. See the Critic results.";\n  } else if (e.kind === "error") {\n    state(-1); $("st").textContent = e.message;\n    heading("o2", "ERROR", "no"); block("o2", e.message);\n  }\n}\nasync function run() {\n  if (!samples.length) return;\n  reset(); const my = generation;\n  controller = new AbortController(); const signal = controller.signal;\n  $("run").disabled = true;\n  try {\n    const response = await fetch("/run", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({sample:idx}), signal});\n    if (!response.ok || !response.body) throw new Error("Pipeline unavailable (HTTP " + response.status + ").");\n    const reader = response.body.getReader(), decoder = new TextDecoder();\n    let pending = "";\n    while (true) {\n      const {value, done} = await reader.read();\n      if (done) break;\n      pending += decoder.decode(value, {stream:true});\n      let newline;\n      while ((newline = pending.indexOf("\\n")) !== -1) {\n        const line = pending.slice(0, newline); pending = pending.slice(newline + 1);\n        if (line && my === generation) receive(JSON.parse(line));\n      }\n    }\n    if (pending.trim() && my === generation) receive(JSON.parse(pending));\n  } catch (error) {\n    if (error.name !== "AbortError" && my === generation) receive({kind:"error", message:error.message});\n  } finally {\n    if (my === generation) { controller = null; $("run").disabled = false; }\n  }\n}\n$("run").onclick = run;\n$("next").onclick = () => { if (samples.length) { idx = (idx + 1) % samples.length; reset(); $("run").disabled = false; run(); } };\nfetch("/samples").then(r => {if (!r.ok) throw new Error("Could not load samples"); return r.json()})\n  .then(data => { samples = data; reset(); })\n  .catch(e => { $("st").textContent = e.message; });\n</script>\n</body>\n</html>'

class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # Avoid logging requests, especially if the browser disconnects.

    def send_bytes(self, body, mime):
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/":
            self.send_bytes(HTML.encode("utf-8"), "text/html; charset=utf-8")
        elif self.path == "/samples":
            data = [{"title": s["title"], "cobol": s["cobol"]} for s in SAMPLES]
            self.send_bytes(json.dumps(data).encode(), "application/json; charset=utf-8")
        else:
            self.send_error(404)

    def emit(self, **event):
        self.wfile.write((json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8"))
        self.wfile.flush()

    def do_POST(self):
        if self.path != "/run":
            self.send_error(404); return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if size < 1 or size > 4096:
                self.send_error(400); return
            payload = json.loads(self.rfile.read(size))
            index = payload["sample"]
            if type(index) is not int or not (0 <= index < len(SAMPLES)):
                self.send_error(400); return
        except (ValueError, KeyError, TypeError):
            self.send_error(400); return
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        sample = SAMPLES[index]
        try:
            count = validate_cobol(sample["cobol"])
            self.emit(kind="stage", agent=0, message=f"Pre-flight passed: {count} COBOL variables declared. Extractor is reading the code...")
            logic = clean_text(ask_groq([
                {"role": "system", "content": "Extract the business logic faithfully from this COBOL program as numbered plain English rules. Include literals, output order, rounding and formatting. No markdown, no asterisks, no Java."},
                {"role": "user", "content": sample["cobol"]},
            ]))
            self.emit(kind="logic", text=logic)
            feedback = ""
            approved = False
            for attempt in range(1, 4):
                self.emit(kind="stage", agent=1, message=f"Modernizer is writing Java (attempt {attempt})...")
                prompt = ("Convert the COBOL and extracted business logic below into a COMPLETE single-file Java program. "
                          "Return ONLY compilable Java code inside one java code fence. No prose. "
                          "Use BigDecimal for money, not double or float. No external dependencies. "
                          "Match COBOL DISPLAY output exactly, including zeros, spaces, order, and newline. "
                          "Use a public class with public static void main(String[] args). "
                          "Program-specific requirements: " + sample["notes"] +
                          "\nCOBOL:\n" + sample["cobol"] + "\nExtracted logic:\n" + logic +
                          ("\nFix ALL these issues in your previous Java version:\n" + feedback if feedback else ""))
                java = extract_java(ask_groq([{"role": "user", "content": prompt}]))
                self.emit(kind="java", attempt=attempt, text=java)
                self.emit(kind="stage", agent=2, message="Critic is compiling Java and comparing its real output against COBOL...")
                ok, result = run_java(java, sample["args"])
                if ok:
                    approved, detail, feedback = compare(sample["expected"](), result)
                else:
                    approved, detail, feedback = False, result, result
                self.emit(kind="critique", attempt=attempt, approved=approved, text=detail)
                if approved or feedback.startswith("Java JDK not found"):
                    break
            self.emit(kind="done", approved=approved)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:
            try:
                self.emit(kind="error", message=str(exc))
            except (BrokenPipeError, ConnectionResetError):
                pass


def main():
    address = f"http://127.0.0.1:{PORT}"
    print(f"COBOL to Java live pipeline: {address}")
    if not os.environ.get("GROQ_API_KEY"):
        print("Set GROQ_API_KEY in your environment before running the pipeline.")
    if not shutil.which("javac") or not shutil.which("java"):
        print("Install a JDK (17+) and put javac and java on PATH for real validation.")
    try:
        with ThreadingHTTPServer(("127.0.0.1", PORT), Handler) as server:
            Timer(0.5, lambda: webbrowser.open(address)).start()
            server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")

if __name__ == "__main__":
    main()
