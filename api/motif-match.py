"""Vercel serverless endpoint for the Motif Finder web tool (index.html).

POST /api/motif-match  {"name": "...", "hint": "...", "code": "<access code>"}
 -> 200 {"person_summary", "motifs": [...], "rejected", "dossier_source",
         "searches_used", "cost", "model", "prompt_version"}
 -> 401 bad/missing access code   -> 400 bad input   -> 500 upstream failure

Secrets live in Vercel env vars, never in the repo:
  ANTHROPIC_API_KEY  — the deployer's Anthropic key (all usage billed to it)
  MOTIF_TOOL_CODE    — shared access code gating the tool

Research strategy: free Wikipedia first; the paid web_search tool is enabled only
when the dossier is missing/thin/disambiguous.
"""

import json
import os
import re
import sys
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))  # Vercel's runtime doesn't put api/ on sys.path

# Anything failing here would otherwise surface as an unreadable
# FUNCTION_INVOCATION_FAILED; capture it so GET /api/motif-match can report it.
STARTUP_ERROR = None
try:
    import requests
    import motif_core
    CATALOG = json.loads((HERE / "motif_catalog.json").read_text(encoding="utf-8"))
    FEWSHOT = json.loads((HERE / "fewshot_assignments.json").read_text(encoding="utf-8"))
except Exception:
    STARTUP_ERROR = traceback.format_exc()

WIKI_API = "https://en.wikipedia.org/w/api.php"
MIN_DOSSIER_CHARS = 400
MAX_DOSSIER_CHARS = 8000
MODEL = "claude-sonnet-5"
# Tightened 2026-07-18 (validated config was 3 searches / 4 rounds / 8000 tok):
# the web-search path must fit Vercel Hobby's hard 300 s maxDuration.
MAX_SEARCHES = 2
MAX_ROUNDS = 2
MAX_TOKENS = 5000


def wikipedia_dossier(name, hint=""):
    dossier = {"name": name, "hint": hint or None, "source": "none",
               "title": None, "extract": None, "url": None}
    try:
        s = requests.Session()
        s.headers["User-Agent"] = "vultology-motif-finder/1.0 (rivelli@kylerivelli.com)"
        titles = []
        for query in ([f"{name} {hint}".strip()] if hint else []) + [name]:
            r = s.get(WIKI_API, params={"action": "opensearch", "search": query,
                                        "limit": 1, "format": "json"}, timeout=10)
            r.raise_for_status()
            titles = r.json()[1]
            if titles:
                break
        if titles:
            r = s.get(WIKI_API, params={
                "action": "query", "prop": "extracts", "explaintext": 1,
                "redirects": 1, "titles": titles[0], "format": "json"}, timeout=15)
            r.raise_for_status()
            page = next(iter(r.json().get("query", {}).get("pages", {}).values()), {})
            extract = (page.get("extract") or "").strip()
            if extract:
                title = page.get("title", titles[0])
                dossier.update({
                    "source": "wikipedia", "title": title,
                    "extract": extract[:MAX_DOSSIER_CHARS],
                    "url": "https://en.wikipedia.org/wiki/"
                           + urllib.parse.quote(title.replace(" ", "_")),
                })
                if re.search(r"may (also )?refer to\s*:", extract[:300], re.I):
                    dossier["disambiguation"] = True
    except Exception:
        pass  # empty dossier -> the model searches instead
    return dossier


def run(name, hint):
    import time
    import anthropic
    t0 = time.monotonic()
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    dossier = wikipedia_dossier(name, hint)
    thin = (not dossier.get("extract")
            or len(dossier["extract"]) < MIN_DOSSIER_CHARS
            or dossier.get("disambiguation"))
    # build_system already returns the cache_control'd system block list
    system = motif_core.build_system(CATALOG, FEWSHOT)
    messages = motif_core.build_user(name, dossier)
    tools = [motif_core.web_search_tool(MODEL, MAX_SEARCHES)] if thin else []

    usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0,
             "cache_creation_input_tokens": 0, "web_search_requests": 0}
    resp = None
    for _ in range(MAX_ROUNDS):  # pause_turn continuations
        kwargs = dict(model=MODEL, max_tokens=MAX_TOKENS, system=system, messages=messages)
        if tools:
            kwargs["tools"] = tools
        resp = client.messages.create(**kwargs)
        u = resp.usage
        usage["input_tokens"] += u.input_tokens
        usage["output_tokens"] += u.output_tokens
        usage["cache_read_input_tokens"] += getattr(u, "cache_read_input_tokens", 0) or 0
        usage["cache_creation_input_tokens"] += getattr(u, "cache_creation_input_tokens", 0) or 0
        stu = getattr(u, "server_tool_use", None)
        if stu is not None:
            usage["web_search_requests"] += getattr(stu, "web_search_requests", 0) or 0
        if resp.stop_reason != "pause_turn":
            break
        messages = messages + [{"role": "assistant", "content": resp.content}]

    parsed, _ = motif_core.parse_response(resp.content)
    if parsed is None:
        raise ValueError(f"model response unparseable (stop_reason={resp.stop_reason}); "
                         "try again or fall back to a manual scan")
    accepted, rejected = motif_core.validate_motifs(parsed, CATALOG)
    inp, outp = motif_core.PRICE[MODEL]
    cost = (usage["input_tokens"] * inp
            + usage["cache_creation_input_tokens"] * inp * 1.25
            + usage["cache_read_input_tokens"] * inp * 0.1
            + usage["output_tokens"] * outp) / 1e6 \
        + usage["web_search_requests"] * motif_core.SEARCH_PRICE
    return {
        "person_summary": (parsed or {}).get("person_summary"),
        "motifs": accepted, "rejected": rejected,
        "dossier_source": dossier.get("source"),
        "dossier_url": dossier.get("url"),
        "searches_used": usage["web_search_requests"],
        "cost": round(cost, 4), "model": MODEL,
        "prompt_version": motif_core.PROMPT_VERSION,
        "elapsed_s": round(time.monotonic() - t0, 1),
    }


class handler(BaseHTTPRequestHandler):
    def _send(self, status, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        # Health check: no auth, no spend, no secrets — just "did the module load".
        if STARTUP_ERROR:
            return self._send(500, {"ok": False, "startup_error": STARTUP_ERROR})
        return self._send(200, {"ok": True, "model": MODEL,
                                "prompt_version": motif_core.PROMPT_VERSION})

    def do_POST(self):
        if STARTUP_ERROR:
            return self._send(500, {"error": "function failed to initialize",
                                    "startup_error": STARTUP_ERROR})
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return self._send(400, {"error": "invalid JSON body"})

        expected = os.environ.get("MOTIF_TOOL_CODE")
        if not expected or payload.get("code") != expected:
            return self._send(401, {"error": "invalid access code"})

        name = str(payload.get("name") or "").strip()[:120]
        hint = str(payload.get("hint") or "").strip()[:200]
        if not name:
            return self._send(400, {"error": "name is required"})

        try:
            return self._send(200, run(name, hint))
        except Exception as e:
            return self._send(500, {"error": f"{type(e).__name__}: {e}"})
