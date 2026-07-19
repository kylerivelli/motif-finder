"""Shared motif-matching logic: prompt build, response parse, catalog validation.

The same file serves offline harnesses and the deployed serverless endpoint — keep it
free of path assumptions beyond what callers pass in, and dependency-free (stdlib only).

PROMPT_VERSION is recorded with every run; bump it on ANY change to the instructions,
few-shot set, or output contract so runs stay comparable.
"""

import difflib
import json
import re

PROMPT_VERSION = "v3"  # v3: three rules targeting the reachable-zero motifs from the v2
                       # validation (sibling co-tagging, lifestyle/belief coverage,
                       # neutral-valence). Diagnosis: the model treats near-synonym
                       # catalog entries as mutually exclusive and picks one (Productivity
                       # / Existentialism / Law of Attraction were predicted ZERO times in
                       # 340 ledger rows), and euphemizes judgment-flavored motifs
                       # (Hedonism carriers got 'Addiction' instead).
                       # v2: added work-content guidance (genres/aesthetics/themes in the
                       # person's output count, not just biographical facts)

# (input $/MTok, output $/MTok); web search is $10 per 1,000 searches on top.
PRICE = {
    "claude-sonnet-5": (2, 10),      # intro pricing through 2026-08-31
    "claude-haiku-4-5": (1, 5),
    "claude-opus-4-8": (5, 25),
}
SEARCH_PRICE = 0.01  # $ per web search

# Models supporting the web_search_20260209 (dynamic filtering) variant.
SEARCH_20260209 = {"claude-sonnet-5", "claude-opus-4-8"}

# Fixed few-shot exemplars (slugs in db_motif_assignments.json). Chosen for spread:
# pop musician / physicist / spirituality YouTuber. Static so the cached prompt prefix
# is stable. These three are EXCLUDED from validation sampling (leakage).
FEWSHOT_SLUGS = ["aaliyah", "albert-einstein", "aaron-doughty"]


def web_search_tool(model, max_uses=3):
    tool_type = "web_search_20260209" if model in SEARCH_20260209 else "web_search_20250305"
    return {"type": tool_type, "name": "web_search", "max_uses": max_uses}


def _fewshot_text(assignments):
    lines = []
    for slug in FEWSHOT_SLUGS:
        a = assignments.get(slug)
        if not a:
            continue
        lines.append(f'- {a.get("name", slug)}: '
                     + "; ".join(m["name"] for m in a["motifs"]))
    if not lines:
        return ""
    return ("\n\nExamples of certified motif assignments (note the granularity and "
            "typical count per person):\n" + "\n".join(lines))


def build_system(catalog, assignments):
    """One cacheable system block: instructions + full motif list + few-shot."""
    motif_lines = "\n".join(
        f'- {m["name"]}' + (f' ({m["count"]} people)' if m.get("count") else "")
        for m in catalog["motifs"])
    text = (
        "You assist certified vultologists at vultology.com. Given a person's identity, "
        "you select which motifs from the site's fixed motif taxonomy fit that person. "
        "Motifs describe recurring themes in a person's public life and work: interests, "
        "values, occupations, artistic domains, psychological themes.\n\n"
        "Rules:\n"
        "- Choose ONLY motifs from the list below, copying names EXACTLY as written.\n"
        "- Suggest every motif that clearly fits (typically 8-20 for a well-documented "
        "person); do not pad with weak fits.\n"
        "- Base each pick on verifiable facts about the person, not on speculation about "
        "their inner life.\n"
        "- Motifs cover both the person's LIFE (occupations, interests, causes, "
        "biography) and the content of their WORK: genres they play in, aesthetics they "
        "channel, and recurring subject matter of their art (e.g. an artist whose albums "
        "and film scores center on science fiction gets 'Science Fiction and Dystopia'; "
        "a musician rooted in 1980s synth sounds gets 'New Wave' if that genre fits). "
        "Include both kinds.\n"
        "- Overlapping motifs are NOT mutually exclusive. The taxonomy deliberately "
        "contains near-synonym clusters, and certified assignments routinely carry "
        "several members of one cluster at once (see the examples below). When more "
        "than one overlapping motif fits, select EACH one that fits on its own — never "
        "pick the single best member and drop its siblings. E.g. a fitness-focused "
        "athlete known for diet and wellbeing gets BOTH 'Athletics, Exercise and Health "
        "Optimization' AND 'Health, Wellness, and Nutrition'; a prolific high-achiever "
        "can carry 'Strong Work Ethic and Ambition' AND 'Productivity and Achievement'; "
        "an artist of existential work gets 'Existential Themes' AND 'Existentialism'.\n"
        "- Cover documented LIFESTYLE and BELIEF facts, not just occupation and output: "
        "diet and wellness practices, self-improvement systems, spiritual or new-age "
        "belief systems (documented manifestation/energy talk fits 'Law of Attraction "
        "and Energy Beliefs'), and running an independent outlet, podcast, or "
        "self-owned platform ('Alternative Media and Independent Journalism', alongside "
        "any mainstream-media motifs).\n"
        "- Motif names are neutral descriptors, never accusations. When documented "
        "facts fit a judgment-flavored motif (e.g. a well-documented partying, excess, "
        "or pleasure-seeking lifestyle fits 'Hedonism and Self-Indulgence'), select it "
        "rather than substituting a softer or more clinical neighbor.\n"
        "- If the provided dossier is insufficient to identify the person confidently and "
        "a web search tool is available, search before answering. If you still cannot "
        "identify them, return an empty motif list and say so in person_summary.\n\n"
        "Motif taxonomy (name, with how many database entries currently carry it):\n"
        + motif_lines
        + _fewshot_text(assignments)
        + "\n\nReply with JSON only, no prose outside the JSON:\n"
        '{"person_summary": "<2-3 sentences: who this is>", '
        '"motifs": [{"name": "<exact motif name>", "confidence": "high|medium|low", '
        '"evidence": "<one short factual clause>"}]}'
    )
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]


def build_user(name, dossier):
    subject = f"Subject: {name}"
    if dossier and dossier.get("hint"):
        subject += f"  ({dossier['hint']})"
    if dossier and dossier.get("extract"):
        note = (" NOTE: this dossier is a disambiguation page — identify the intended "
                "person (use the hint; search if a tool is available) before assigning "
                "motifs." if dossier.get("disambiguation") else "")
        body = (f"{subject}\n\nDossier (from {dossier.get('source', 'research')}, "
                f"\"{dossier.get('title', name)}\"):{note}\n{dossier['extract']}")
    else:
        body = (f"{subject}\n\nNo dossier could be assembled for this person from "
                f"free sources. Identify them yourself (searching if a tool is available), "
                f"then assign motifs.")
    return [{"role": "user", "content": body}]


def parse_response(resp_content):
    """Concatenate text blocks, slice the outermost JSON object, parse defensively."""
    text = "".join(b.text for b in resp_content if getattr(b, "type", None) == "text")
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None, text
    try:
        return json.loads(text[start:end + 1]), text
    except json.JSONDecodeError:
        return None, text


def validate_motifs(parsed, catalog):
    """Map suggested names onto real catalog entries; reject hallucinations.

    Returns (accepted, rejected): accepted rows gain 'slug' and 'count'.
    """
    by_norm = {}
    for m in catalog["motifs"]:
        by_norm[_norm(m["name"])] = m
    accepted, rejected = [], []
    seen = set()
    for row in (parsed or {}).get("motifs", []):
        name = str(row.get("name", "")).strip()
        hit = by_norm.get(_norm(name))
        if hit is None:
            close = difflib.get_close_matches(_norm(name), list(by_norm), n=1, cutoff=0.85)
            hit = by_norm[close[0]] if close else None
        if hit is None:
            rejected.append(name)
            continue
        if hit["slug"] in seen:
            continue
        seen.add(hit["slug"])
        accepted.append({
            "name": hit["name"], "slug": hit["slug"], "count": hit.get("count"),
            "confidence": row.get("confidence"), "evidence": row.get("evidence"),
        })
    order = {"high": 0, "medium": 1, "low": 2}
    accepted.sort(key=lambda r: order.get(str(r.get("confidence")).lower(), 3))
    return accepted, rejected


def _norm(s):
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def estimate_cost(model, system_blocks, n_subjects=1, searches_per=1,
                  out_tokens=1200, dossier_tokens=1500):
    """Rough $ estimate. System prefix ~chars/4 tokens; assumes cache hit after first."""
    inp, outp = PRICE.get(model, PRICE["claude-sonnet-5"])
    sys_tokens = sum(len(b["text"]) for b in system_blocks) // 4
    first = (sys_tokens * 1.25 + dossier_tokens) * inp / 1e6 + out_tokens * outp / 1e6
    rest = (sys_tokens * 0.1 + dossier_tokens) * inp / 1e6 + out_tokens * outp / 1e6
    search_cost = n_subjects * searches_per * SEARCH_PRICE
    return first + max(0, n_subjects - 1) * rest + search_cost
