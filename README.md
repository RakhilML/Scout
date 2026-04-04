# Scout

AI-powered web research CLI. Runs on your machine. No API keys.

You give it a question. It searches the web, reads the actual pages, runs everything through your local LLM, and saves a clean report. You can run it once or put it on a schedule — every few hours, daily, whatever. Reports come out as markdown and JSON.

No cloud. No subscriptions. Everything goes through LM Studio on your machine.

---

## Quick start

```bash
git clone <repo>
cd Scouts
pip install -e .
cp .env.example .env
```

Open `.env` and set your LM Studio details:

```env
LM_STUDIO_BASE_URL=http://localhost:1234/v1
LM_STUDIO_MODEL=openai/gpt-oss-20b
```

Check everything's connected:

```bash
scout check
```

---

## Usage

**One-shot run:**

```bash
scout run "latest benchmarks for RTX 5090"
scout run "what changed in Python 3.13" --output ./research --max-results 8
scout run "BTC price today" --no-save
```

**Schedule a recurring job:**

```bash
# every 6 hours
scout schedule "AI news digest" --every 6h --output ./reports

# every day at 9am
scout schedule "morning tech news" --cron "0 9 * * *" --output ./reports
```

Trigger formats: `30m`, `2h`, `6h`, `1d` — or any cron expression.

**Manage jobs:**

```bash
scout list                    # see all scheduled jobs
scout stop <job-id>           # remove a job
scout pause <job-id>          # pause without removing
scout resume <job-id>         # resume a paused job
```

**Other:**

```bash
scout models                  # list models available in LM Studio
scout history ./reports       # list saved reports in a folder
```

---

## How it works

```
                        YOU
                         |
             "latest RTX 5090 benchmarks"
                         |
                    +----v----+
                    |  Scout  |
                    +----+----+
                         |
              +----------+----------+
              |                     |
       +------v------+     +--------v-------+
       | DuckDuckGo  |     |  LM Studio     |
       |   Search    |     |  (local LLM)   |
       +------+------+     +--------+-------+
              |                     ^
         [N result URLs]            |
              |                     |
       +------v-----------+         |
       |  Parallel Fetch  |         |
       |  (4 workers)     |         |
       +------+-----------+         |
              |                     |
       +------v-----------+         |
       |  Text Extraction |         |
       |  trafilatura     |         |
       |  -> BS4          |         |
       |  -> DDG snippet  |         |
       +------+-----------+         |
              |                     |
       [clean page text] ---------->+
                                    |
                          +---------v--------+
                          | DSPy + Instructor|
                          | (structured out) |
                          |  -> raw fallback |
                          +---------+--------+
                                    |
                           +--------v--------+
                           |  Save Report    |
                           |  .md  +  .json  |
                           +-----------------+
```

**One-shot run** executes this pipeline once and exits. **Scheduled run** repeats it on your chosen interval, saving a new report each time.

---

## Reports

Every run saves two files:

```
reports/
  2025-01-15_09-30_latest-benchmarks-for-rtx-5090.md
  2025-01-15_09-30_latest-benchmarks-for-rtx-5090.json
```

The markdown has a summary, key findings with sources, and a confidence level based on how much real content was actually fetched. The JSON has the same data plus page-level fetch stats — useful if you want to process it further.

---

## Configuration

All settings in `.env`:

| Variable | Default | What it does |
|---|---|---|
| `LM_STUDIO_BASE_URL` | — | LM Studio server URL (required) |
| `LM_STUDIO_MODEL` | — | Model to use, e.g. `openai/gpt-oss-20b` (required) |
| `SCOUT_LLM_TIMEOUT` | `300` | Seconds to wait for LLM response — set this high for big models |
| `SCOUT_MAX_RESULTS` | `6` | Search results to fetch per run |
| `SCOUT_MAX_PAGE_CHARS` | `4000` | Max characters per page sent to the LLM |
| `SCOUT_REQUEST_TIMEOUT` | `12` | HTTP timeout when fetching pages |
| `SCOUT_FETCH_RETRIES` | `2` | Retry count on failed page fetches |
| `SCOUT_OUTPUT_DIR` | `./reports` | Default output folder |
| `SCOUT_DATA_DIR` | `~/.scout` | Where the scheduler job database lives |

---

## Model notes

Reasoning models (gpt-oss-120b, gpt-oss-20b, gemma-3-27b) are handled automatically — Scout knows they need `max_tokens=-1` and waits for the reasoning field before reading the response.

`gpt-oss-20b` is a good default. `gpt-oss-120b` gives better results but takes a few minutes per run.

---

## Requirements

- Python 3.12+
- [LM Studio](https://lmstudio.ai) with a model loaded and the server running
