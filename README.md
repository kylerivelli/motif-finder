# Motif Finder

A small web tool that suggests likely **vultology motifs** — recurring themes of
expression catalogued at [vultology.com/motifs](https://vultology.com/motifs/) — for a
named public figure. Type a name, get a ranked list of candidate motifs with confidence
levels and evidence snippets.

**Output is AI-generated suggestion material for certified review — not certified motif
assignments.** The tool proposes; certified vultologists dispose.

## How it works

- `index.html` — the entire UI, a single self-contained static page (no build step).
- `api/motif-match.py` — a Vercel Python serverless function. On each request it:
  1. Pulls a free dossier on the person from the public Wikipedia API.
  2. If the dossier is missing, thin, or ambiguous, enables Anthropic's paid
     `web_search` tool as a fallback (capped at 2 searches).
  3. Calls `claude-sonnet-5` with the full motif catalog (426 motifs) and three
     few-shot examples, then parses/validates the response against the catalog.
- `api/motif_core.py` — prompt construction, response parsing, catalog validation
  (stdlib-only).
- `api/motif_catalog.json` — the motif catalog, built from the public catalog at
  vultology.com/motifs.
- `api/fewshot_assignments.json` — certified motif lists for three example subjects,
  used as few-shot anchors in the prompt.

No database, no persistence, no tracking. The only outbound calls are Wikipedia and the
Anthropic API. Every response reports its own API cost.

## Deploy your own

1. Fork/clone this repo and import it into [Vercel](https://vercel.com/new)
   (framework preset: **Other**, no build command, root directory = repo root).
2. Set two environment variables in the Vercel project:
   - `ANTHROPIC_API_KEY` — your [Anthropic API key](https://console.anthropic.com/).
     All model + web-search spend is billed to this key.
   - `MOTIF_TOOL_CODE` — an access code of your choosing. Every request must supply
     it; this is what keeps a public URL from spending your key.
3. Deploy. The tool is at `/`, the function at `/api/motif-match`
   (`GET` = no-auth health check, `POST {"name", "hint", "code"}` = a match run).

Notes:
- `vercel.json` pins `maxDuration: 300` on the function — the web-search path needs
  the full Hobby-plan cap.
- The site ships `X-Robots-Tag: noindex, nofollow` on every route by design; it is
  meant to be unlisted, not discovered.

## Cost

A typical run with a good Wikipedia dossier costs ~$0.01–0.03; runs that fall back to
web search cost more (search is billed per request on top of tokens). The exact cost of
each run is included in its response.

## License

[MIT](LICENSE). The motif taxonomy and the certified example assignments are the work
of the vultology.com community, included here with permission; the catalog data
reflects the public database at vultology.com.
