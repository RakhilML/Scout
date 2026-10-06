# Scout

Private, verifiable web research and monitoring with your own local LLM.

You ask a question. Scout searches the web, reads the pages, and has your model (LM Studio,
Ollama, llama.cpp, vLLM) pull out the facts. **Every fact must come with a quote copied from a
page, and Scout checks that quote against the page itself, numbers included.** A claim whose
quote is not on the page, or whose numbers differ from it, never counts as a finding.

Then you can **watch** a question: Scout re-runs it on a schedule and tells you what changed.
Because model output varies from run to run while pages may not, a change is only reported when
the page text changed. Rewordings and forgotten facts stay quiet.

Nothing leaves your machine except the searches and page downloads.

## What makes it different

- **No quote, no claim.** Quotes are matched against the full page, exactly or nearly (typography
  differs), but never with a different number. Values far from the others, accessory prices,
  facts the model itself doubts and news from pages dated long before the period asked about are
  set aside, with the reason shown.
- **Changes judged by evidence.** A fact the model reports for the first time, but that was
  already on the page last time, is *noticed*, not *new*. A price counts as changed only when its
  old evidence left the re-read page.
- **Alerts exactly once.** Events ("changed", "new", "drop 5%") alert once per change. Conditions
  ("price below 1800 USD", "in stock") alert when they start to hold and again only after they
  stopped. Alerts that could not be delivered are retried by the next run.
- **Learns as it goes.** It remembers every verified fact (`scout ask` answers from memory in
  milliseconds), learns which sites block it or whose quotes fail (`scout sites`), and takes your
  corrections (`scout rate`) into memory, site standing and test cases.
- **Cheap to watch.** When no page changed since the last run, the model is not asked at all.

## Quick start

```bash
pip install -e ".[notify,mcp]"   # Python 3.11+
cp .env.example .env             # then set LM_STUDIO_BASE_URL and LM_STUDIO_MODEL
scout check                      # tests the server and shows what the model can do
scout run "What are the new features in Python 3.13?"
```

`scout check` asks the server for the model's real context window and reasoning levels, and
tests whether schema-constrained output works. It remembers the answer for a day.

## Research

```
goal ─> plan ─> search ─> select ─> fetch ─> pack ─> extract ─> verify ─> report
       (model)  (ddgs or  (relevance,  (HTML,   (best     (claims    (quotes on the
                SearXNG)  then site    PDF,     passages  + verbatim  page, numbers,
                          standing)    JSON-LD) that fit)  quotes)    outliers)
```

- **Plan**: the model turns the goal into 2-4 queries, a kind (price, news, release, general)
  and how recent sources must be; a keyword heuristic takes over if it can't.
- **Select**: results that share little of the goal's distinctive terms are dropped; among the
  rest, sites whose quotes keep verifying rank a little higher, and sites that failed several
  times in a row are skipped for a week.
- **Fetch**: pages are decoded properly (gzip, brotli, zstd), PDFs are read as PDFs, binary junk
  is rejected, and pages are cached and revalidated with ETags. Prices that pages publish as
  schema.org data are read exactly, rental rates ("per hour") included. Pages that only render
  with JavaScript can be read in a headless browser (`SCOUT_RENDER=playwright`).
- **Pack**: the passages most relevant to the goal fill the model's actual context window.
- **Extract**: page text is fenced as untrusted data; instructions hidden in pages are ignored.

`scout run --deep` keeps going where the findings leave gaps: the model names follow-up searches,
only pages not read yet are read, their findings are verified the same way, and the final answer
is written from verified findings alone, citing them by number.

Every run is stored in `~/.scout/scout.db` and written as Markdown and JSON to
`~/.scout/reports/`. `scout export FOLDER` writes runs as notes with YAML front matter (Obsidian and
other Markdown vaults) or, with `--format html`, as self-contained web pages.

## Watches

```bash
scout watch add gpu "cheapest RTX 5090 price" --every 6h \
    --alert "price below 1800 USD" --alert "drop 5%" --notify ntfy://my-topic
scout watch run gpu        # run now; the first run is the baseline
scout daemon               # run every watch on its schedule
```

Or start from a pack: `scout pack list`, then
`scout pack add price product="RTX 5090" below="1800 USD"`. Packs are YAML recipes; add your own
to `~/.scout/packs/`.

| Alert rule | Fires |
|---|---|
| `new` | a fact appeared that was not on the page before |
| `changed` | a value changed by 1% or more, or a fact left its page |
| `price below 1800 USD`, `price above 2500` | when a price starts to hold the limit |
| `drop 5%`, `rise 10%` | a price moved that much between two runs |
| `mentions "free-threaded"` | a new fact contains the text |
| `in stock` | a published offer became available |

Notifications go through [Apprise](https://github.com/caronc/apprise) URLs: `ntfy://topic`,
`tgram://bot/chat`, `mailto://...`, `discord://...`, or `json://host/path` as a webhook.
Every run also rewrites an Atom feed of the watch's alerts, `~/.scout/feeds/NAME.xml`
(`scout watch feed NAME` prints it).

| Command | What it does |
|---|---|
| `scout watch add/list/show/remove/pause/resume` | Manage watches (kept in `~/.scout/watches.yaml`) |
| `scout watch run NAME` | Run one now |
| `scout watch changes NAME` | Its alerts, with the page and quote behind each |
| `scout watch trend NAME [--csv FILE]` | How its prices and numbers moved, with sparklines |
| `scout daemon` | Run every watch on schedule; reloads the watches file when it changes, retries a failed run after 30 minutes |
| `scout service install` | Start the daemon at login (systemd user unit, LaunchAgent, or Windows Startup script; no admin rights). `--dry-run` shows what it would do |
| `scout logs` | The end of the daemon's log |
| `scout prune` | Drop old cached searches and page versions (the daemon does it daily) |

A watch has one run at a time: `scout watch run` is refused while the daemon is running it.
Editing a watch's goal starts a new baseline; its value history stays.

## Memory and feedback

| Command | What it does |
|---|---|
| `scout ask QUESTION` | What earlier runs verified about it: no web, no model |
| `scout rate RUN N good\|bad` | Judge finding N of a run (as its report numbers them) |
| `scout ratings [--export FILE.jsonl]` | The ratings, or a dataset of them |
| `scout sites [--forget SITE]` | What Scout learned about each site |

A trusted finding rated bad is forgotten (later runs that find it again do not bring it back;
rating it good does), counts against its site, and becomes something a test case must never
trust again.

## Use it from an AI assistant (MCP)

`scout mcp` serves Scout's tools over MCP (stdio): `research` (optionally deep), `recall`,
`report`, `watch_add`, `watch_list`, `watch_run`, `watch_trend`, `watch_changes`. Every result
carries the quotes and pages behind it.

```bash
claude mcp add scout -- scout mcp                      # Claude Code
```

```json
{ "mcpServers": { "scout": { "command": "scout", "args": ["mcp"] } } }
```

(the second is for Claude Desktop's `claude_desktop_config.json`).

## Any model, including an agent

`SCOUT_LLM` (or `--llm`) picks who answers:

- `openai`: an OpenAI-compatible server configured by `LM_STUDIO_*`. `SCOUT_PLANNER_MODEL` lets a
  small model plan searches while the main model reads; `SCOUT_MODEL_TTL` lets LM Studio unload a
  model it loaded on demand, so a daemon that runs every few hours leaves your GPU free between runs.
- `exchange:DIR`: each request is written to `DIR/requests/<key>.md`; whoever answers (a person,
  or an agent such as Claude Code) writes the reply to `DIR/responses/<key>.txt`. The command
  stops with exit code 75 while it waits; run it again to continue. The searches and pages it
  read are kept while it waits, so the run resumes with the same prompts however long the answer
  takes (up to a day).
- `record:DIR` uses the server and saves every exchange; `replay:DIR` answers only from saved
  exchanges. Together they turn a real session into an offline test fixture.

## Testing models and prompts

- `scout replay RUN` analyzes a stored run's exact pages again, with any model, and compares.
- `scout eval export RUN case.json` turns a run into a test case; its ratings become what the
  case expects.
- `scout eval run cases/ --save base.json`, then after changing a model or a prompt,
  `scout eval run cases/ --against base.json` fails if any expected fact was lost or any claim
  that must never be trusted is trusted now.

## Configuration

Environment variables, or lines in `.env` (see `.env.example`):

| Variable | Default | Meaning |
|---|---|---|
| `SCOUT_LLM` | `openai` | Backend, as above |
| `LM_STUDIO_BASE_URL` | `http://localhost:1234/v1` | Server URL |
| `LM_STUDIO_MODEL` | | Model id (`scout models`) |
| `LM_STUDIO_API_KEY` | | Token, if the server requires one |
| `SCOUT_PLANNER_MODEL` | the main model | A smaller model for planning searches |
| `SCOUT_MODEL_TTL` | | Seconds a model loaded on demand stays loaded (LM Studio) |
| `SCOUT_LLM_TIMEOUT` | `300` | Seconds per model reply |
| `SCOUT_CONTEXT_TOKENS` | from the server | Override the context window |
| `SCOUT_SEARCH` | `ddgs` | Or `searxng:http://localhost:8080` (JSON must be enabled there) |
| `SCOUT_RENDER` | off | `playwright` reads JavaScript-only pages (`pip install -e ".[render]"`, then `playwright install chromium`) |
| `SCOUT_MAX_RESULTS` | `6` | Pages per run |
| `SCOUT_MAX_PAGE_CHARS` | `6000` | Most characters of one page sent to the model |
| `SCOUT_REGION` | `us-en` | Search region (`in-en`, `uk-en`, `de-de`, `wt-wt`) |
| `SCOUT_REQUEST_TIMEOUT` | `12` | Seconds per page download |
| `SCOUT_FETCH_RETRIES` | `1` | Extra attempts for transient failures |
| `SCOUT_DATA_DIR` | `~/.scout` | Database, caches, watches, feeds, logs |
| `SCOUT_OUTPUT_DIR` | `<data dir>/reports` | Report files |

Optional extras: `notify` (Apprise), `mcp` (the MCP server), `render` (Playwright).

## Development

```bash
pip install -e ".[dev,notify,mcp]"
ruff check src tests && ruff format --check src tests
pytest                 # offline; `pytest -m live` also hits the network
```
