<p align="center">
  <img src="assets/hero.svg" alt="BizyAir AI Skill — Make it. Just ask." width="100%">
</p>

<h1 align="center">BizyAir AI Skill</h1>

<p align="center"><strong>Make pictures, videos, and voiceovers by talking to your agent.</strong></p>
<p align="center">One sentence in. One confirmation. A finished file back.</p>

---

BizyAir turns your agent into a small creative studio. Describe what you want. It picks the right model,
shows you what will run and what it costs when pricing is available, and starts only after you say yes.
Then it waits for the result, saves the file, and brings it back.

No model shopping. No prompt engineering. No surprise reruns.

## What's inside

| Area | What you can do |
|---|---|
| Images | Text-to-image, image-to-image, editing, background removal, canvas expansion, watermark removal |
| Video | Text-to-video, image-to-video, first/last frame, audio-driven video, multi-shot sequences |
| Audio | Voiceover, music and other audio through ModelZoo endpoints |
| AI apps & workflows | Search community apps and workflows, run by link or ID, export public workflows as ComfyUI JSON |
| ModelZoo | Search underlying model endpoints, inspect details and prices, run with explicit parameters |
| Works gallery | Browse, search, view details, like, favorite, and find works by app or model |
| Model hub | Browse and search LoRA, VAE, UNet and other assets; publish local models or repost from other sites |
| Personal center & membership | Balance, API key, published / forked / generated records, notifications, credits, tier and profile |
| Batch | Run many tasks together, with per-task status and resumable results |

No endpoint counts or model rankings here — the catalog changes, and the agent picks from what is live.

## What you can ask for

| You say | You get |
|---|---|
| “Remove the background. Keep only the sofa.” | A clean cutout, ready for a listing or a deck. |
| “Expand this photo to 16:9 without changing the room.” | A wider shot with matched edges. |
| “Give this product photo a clean studio look.” | A brighter, retouched e-commerce image. |
| “Turn this still into a five-second cinematic clip.” | A short video. |
| “Read this script in a warm, natural voice.” | A voiceover file. |
| “Make a cyberpunk cat poster.” | An image from scratch. |
| “What can I use for image style transfer?” | Model options first — then an offer to check AI apps and workflows. |

You are not locked into one model. Choose from a wide range of image and video models — or reach
further into ModelZoo APIs, community AI apps, and workflows. Name the one you want, or describe the
outcome and let the agent pick.

## Many models, one skill

<p align="center">
  <img src="assets/model-shelf.svg" alt="Seedream 5.0 Flash, FLUX 3, Nano Banana 2.1, GPT Image 2.5 Sunburst, Kling 3.0 Pro, and Wan 2.7." width="100%">
</p>

We have many models to choose from, including **Seedream 5.0 Flash**, **FLUX 3**, **Nano Banana 2.1**,
**GPT Image 2.5 Sunburst**, **Kling 3.0 Pro**, **Wan 2.7**, **Qwen Image 2.1**, **Seedance 2.0**, and
**Happy Horse 1.1**. There are many more ModelZoo endpoints, AI apps, and workflows to choose from.

Pick a name, or describe the outcome and let the agent choose.

## See it in action

<p align="center">
  <img src="assets/how-it-works.svg" alt="You ask, the agent picks, you confirm, and the result lands." width="100%">
</p>

The only decision that costs money is the final yes. You stay in control of it.

## Why it feels different

**It chooses for you.**  
A vague request is enough. The agent maps it to a capability, chooses a model or app, and fills in the
parameters. You can still ask for a different direction at any time.

**It shows the plan before it spends.**  
You see a confirmation card with what will run and the parameters. For ModelZoo, the price is shown
before submission. Nothing is charged while you are still deciding.

**One yes means one run.**  
The agent does not quietly rerun a paid task because it thinks the result could be better. If you want
another version, ask for it — you will see a new confirmation first.

**It brings back the file.**  
The agent polls the task, downloads the output, and saves it locally. If the session is interrupted,
the task can be resumed by request ID instead of being paid for twice.

**It asks before widening the search.**  
When you are exploring rather than ordering — “what services can do this?” — the agent checks ModelZoo
first, then asks whether to look at AI apps and workflows too. Fast first answer, no missed options.

## Batch mode, without the babysitting

Need a set, not a single image? Give one brief and a list of variations — same app or model, different prompts
or images. The skill turns it into a real batch instead of a copy-paste loop.

- **Batch across routes** — run multiple tasks at once with ModelZoo APIs or AI apps. BizyAir members
  can enjoy higher concurrency.
- **Long lists welcome** — keep the task list in a JSON file and pass `--batch-json-file` instead of
  escaping a long inline payload.
- **Every task has its own receipt** — request ID, status, `run.json`, and output folder.
- **One failure does not erase the rest** — completed tasks stay completed; `batch-status` shows what is
  queued, running, done, or failed.
- **No double-paying for finished work** — inspect a batch and its files without rerunning what already
  succeeded.
- **Crash-aware state** — batch status is written atomically, with a watchdog for worker failures.

You still see one batch confirmation card before anything runs. One approval covers the set; every task
inside it remains individually visible.

## Install in one sentence

Copy this to your agent:

> Install the BizyAir skill in this folder into my agent's skills directory, ask me for my BizyAir API
> key, save it as `config.json`, and run `scripts/cli.py check` to verify.

Your agent handles the folder, the key, and the check. It will ask you for an API key if one is not
already configured. Keep the key in the local `config.json`; do not paste it into a public chat.

### Then just ask

Attach a photo and say:

> Make the background a warm beige studio, keep the product exactly as it is.

Or start from nothing:

> Make a quiet editorial photo of a ceramic cup on a linen table, morning light, no text.

## Ask first, run second

“What can I use for…?” is a question, not an order.

The agent returns ModelZoo options first, then asks:

> AI apps and Workflows may also support this. Want me to check them?

You can reply **check apps**, **check workflows**, or **check both**. Nothing is searched in the
background, and nothing is generated until you confirm.

“Make me…” starts the execution path. Even then, the agent shows the plan before spending. “You decide”
or “use the default” is permission to fill the card, not permission to run it.

## Good to know

- Works with BizyAir: [bizyair.ai](https://www.bizyair.ai).
- You need a BizyAir API key, and generation uses your BizyAir credits.
- Python 3.9+ is supported; 3.10+ is recommended. There is nothing to `pip install`.
- It works with agents that can load a skill folder and run local commands.
- Outputs are saved locally. Returned URLs expire, so keep the downloaded file.
- The public API has no cancel endpoint. If a submit response is unclear, the agent checks the task
  before trying again.
- **No key:** menus, `browse`, `models`, `model-types`, `search`, `search-workflows`, `pick-image` / `pick-video`, `batch-status`.
- **Key required:** task runs (`run`, `prefill`, `modelzoo-run`, batch), account reads/writes, `upload`, `status` / `outputs`, `download-workflow`.
- **Public but skill-gated:** `gallery*` reads, `modelzoo-list/detail/price`, `pick-modelzoo-*`, `info`, `model-detail`, `app-md`, `modelzoo-md`. The platform serves these anonymously; the skill asks for a key as a safety default.
- All skill-generated command output, menus, reports, and link labels are in English by default. Remote-provided names and labels are shown as the platform returns them. The agent may translate its own prose only when the user explicitly asks for another language.

## For the technically curious

<details>
<summary>Commands, configuration, and the rules behind the agent</summary>

### A few commands

```bash
# Validate the install
python3 scripts/cli.py check

# Structured menu for automation (no conversational prompt in stdout)
python3 scripts/cli.py image-menu --json

# Find an endpoint, preview the card, then run after confirmation
python3 scripts/cli.py pick-modelzoo-image "remove background"
python3 scripts/cli.py modelzoo-run <endpoint> --param image_urls="https://..."
python3 scripts/cli.py modelzoo-run <endpoint> --param image_urls="https://..." --confirm-run

# Inspect, prefill, and run an AI app
python3 scripts/cli.py info <id-or-url> --with-workflow
python3 scripts/cli.py prefill <id-or-url> --image ./input.jpg
python3 scripts/cli.py run <id-or-url> --image ./input.jpg

# Resume or download a task
python3 scripts/cli.py status <request_id>
python3 scripts/cli.py outputs <request_id> -o ./results
```

### Configuration

`config.json` in the skill root:

```json
{
  "credentials": {
    "api_key": "sk-..."
  },
  "client": {
    "lang": "en"
  }
}
```

Environment overrides include `BIZYAIR_API_KEY`, `BIZYAIR_OUTPUT_DIR`, and `BIZYAIR_STATE_DIR`.
The full host and batch settings are documented in `SKILL.md`.

### The rules that matter

- One confirmation authorizes one submission, not a loop of paid attempts.
- Automatic app-submit retry is disabled (`WEBAPP_RETRY_LIMIT=0`).
- If a response is missing, check `status`, `credits`, or `modelzoo-status` before resubmitting.
- AI-app search and workflow search are capped at one round per source.
- Paid runs are never used as tests.

For the complete command map and API notes, read [`SKILL.md`](SKILL.md) and the
[`references/`](references/) folder.

</details>

## License

MIT

