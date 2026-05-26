
  <h1>BizyAir Skill</h1>


<div align="center">
  <a href="https://bizyair.cn" target="_blank" rel="noopener noreferrer">
    <img
      src="https://plugin.bizyair.com/public/logo_lightmode.png"
      alt="BizyAir"
      width="370"
    />

  </a>

---
<div align="center">

> BizyAir Skill is an AIGC skill pack designed for mainstream AI agents. Powered by BizyAir cloud compute, it removes complex setup and parameter tuning.<br>**With natural language alone, go from idea to finished image and video output in one smooth flow, giving your AI agent professional-grade AIGC capability.**

</div>


<a href="https://skills.sh"><img src="https://img.shields.io/badge/skills.sh-Compatible-green?style=for-the-badge" alt="Skills"></a>
<a href="https://agentskills.io"><img src="https://img.shields.io/badge/AgentSkills-Standard-blue?style=for-the-badge" alt="AgentSkills"></a>
<a href="https://claude.ai/code"><img src="https://img.shields.io/badge/Claude%20Code-Skill-blueviolet?style=for-the-badge" alt="Claude Code"></a>


[Installation](#-installation) · [Usage](#-how-to-talk-to-it) · [Examples](#-how-it-interacts-with-you) · [CLI Commands](#-developer--debug-commands)



**[中文](./README.md) · [English](./README.en.md)**
</div>

****



## ✨ What it can do

| Core capability | Typical scenarios | Interaction style |
| :--- | :--- | :--- |
| **Image Generation** | Text-to-image, image-to-image, editing, outpainting | **5 curated models** + ModelZoo search + AI app search |
| **Video Generation** | Text-to-video, image-to-video, lip sync, etc. | **5 curated models** + ModelZoo search + AI app search |
| **AI Apps & ModelZoo** | Inspect and run BizyAir apps and underlying ModelZoo endpoints | Direct links, object inspection, dual-track search |
| **Smart Parameter Engine** | Intent completion, prompt refinement | "Prefill confirmation card" instead of blank forms |

---

## ⭐️ Prerequisites

- **Python 3.9+**
- A BizyAir API key ready (sign in, hover over your avatar in the top-right corner, then click ["API Keys"](https://bizyair.cn/user/api-key) to create one)
- Sufficient BizyAir balance in your account

---

## 🚀 Installation

Send this in your agent chat:

> Help me install the BizyAir skill from` https://github.com/siliconflow/bizyair-skill `


Or run directly:

```bash
gh skill install siliconflow/bizyair-skill
```

---

### Config file notes (How to configure the API key)

**Option A: use an environment variable**

```bash
export BIZYAIR_API_KEY="your BizyAir API Key"
```

**Option B: write it into the config file**

The `config.json` in the skill root stores the BizyAir API key and batch-related settings.

You can edit it manually, or just give the API key to the assistant and let it write the file for you. **(Recommended)**

```json
{
  "credentials": {
    "api_key": "Put your BizyAir API Key here"
  },
  "batch": {
    "max_concurrency": 3,
    "max_tasks": 5
  }
}
```

> **Notes**: `max_concurrency` — maximum concurrent tasks, default 3, hard cap 5; `max_tasks` — maximum tasks in a single batch, default 5, hard cap 10.

---

## 💬 How to talk to it

Once configured, just talk to the assistant in natural language (pattern: "Use bizyair to ..."):

**🎨 Image tasks:**
* "Use bizyair to create a warm brown coffee advertising poster."
* "Use bizyair to make an elegant anime-style avatar."
* "I want to generate an image. Show me the common BizyAir menu first."

**🎬 Video tasks:**
* "Use bizyair to make a 10-second rainy neon street short video."
* "Animate this portrait image with bizyair."
* "Use bizyair to turn this portrait and this audio clip into a lip-sync video."

**🧩 AI App / Workflow tasks:**
* "Run this BizyAir app: `https://bizyair.cn/...`"
* "Help me understand what this BizyAir workflow ID does and what assets it needs."
* "Find a few BizyAir workflows suitable for e-commerce key visual retouching."

**🛠️ Status and utility tasks:**
* "Check my BizyAir API key."
* "Check my BizyAir balance."

---

## 🤖 How it interacts with you

### 1. Curated video / image menus
When your request is vague (e.g. "help me make a video"), the assistant shows a curated model menu first.

For example, you can say:
> "I want to make a video. Show me the common BizyAir video entry points first."

The assistant will respond like:
> "Pick a model 👇
>
> 【⭐ Curated · 5 common video models】
> 1. 🎬 V.3.1.Pro: cinematic mood, lens feel, stronger visual impact.
> 2. 🐎 HappyHorse: fast output, comfortable price-performance.
> 3. 🐉 Kling 3.0 Pro: stronger for action, movement, and impact, native 4K.
> 4. 🌊 Wanxiang 2.7: strong Chinese understanding, audio sync, multi-shot editing.
> 5. 💃 Seedance 2.0: character motion, dance, and role consistency.
>
> 【🧪 ModelZoo Search · ✨ NEW】
> 6. Direct calls to underlying model APIs — explicit params, predictable per-call billing.
>
> 【🔎 AI App Search】
> 7. Find ready-made workflow templates for complex multi-step pipelines.
>
> I'd suggest starting with option 4 — your request hasn't locked down a specific style yet, so Wanxiang 2.7 tends to be more stable.
> Just reply 1–7; if you already know the model name or number, we can use that directly."

### 2. Link diagnosis
If you send an unfamiliar App link, the assistant will fetch the underlying inspection result (`info`) and explain in natural language:

* What the workflow does.
* What input assets it needs (image / audio / text).
* Whether it can be run directly or requires the web interface.

For example, you can say:
> "Help me inspect this BizyAir app: https://bizyair.cn/community/app/46086"

The assistant will respond like:
> "The inspection report is ready — this workflow looks healthy.
>
> - **What it does**: it's a **Wai-Illustrious anime text-to-image workflow**, with HD upscaling and hand/face repair.
> - **What you need to provide**: no base image needed, just a solid prompt.
> - **What it outputs**: a complete set of finished images (including HD upscaled versions).
> - **Execution status**: ✅ the interface is fully reachable, I can run it for you directly.
>
> So — do you want to start shaping the prompt now, or should I first lay out the size and ratio parameter card for review?"

> **Note**: comfy-ui links (`bizyair.cn/comfy-ui?id=xxx`) are not currently supported for direct execution. The assistant will prompt you to find the corresponding AI App version.

### 3. Smart parameter confirmation card
Once a model is chosen, the assistant absorbs your natural-language request and generates a **prefill confirmation card**.

For example, you can say:
> "I'll use option 4, O.2. Make a Chinese coffee poster, landscape layout."

The assistant will respond like:
> "## 🎨 O.2 Parameter Confirmation
>
> **Prompt**: Chinese coffee poster, landscape layout
> **Ratio**: 3:2 (options: 1:1 / 2:3 / 3:2 / 4:5 / 5:4 / 3:4 / 4:3 / 16:9 / ...)
> **Resolution**: 2K (options: 1K / 2K / 4K)
>
> Once confirmed, reply "run" to generate immediately, or just tell me if you want to change the ratio or resolution."

### 4. Dual-track search
Beyond the 5 curated models, the assistant can take two different search routes depending on what you need:

- **Slot 6 — ModelZoo Search**: direct calls to underlying model APIs, with explicit parameters and predictable per-call billing. Good when you already know the rough route ("I want a Kling video", "find me a specific endpoint").
- **Slot 7 — AI App Search**: BizyAir's workflow templates that bundle complex multi-step pipelines into one click. Good when you want a pre-made solution ("a workflow that swaps backgrounds", "an e-commerce key-visual template").

#### Example 1: find a ModelZoo endpoint

For example, you can say:
> "I want to call an underlying video model API directly. Search me a few kling endpoints."

The assistant will respond like:
> 🎯 **Picked a few video-side ModelZoo endpoints for you**
>
> ModelZoo gives you direct API access — explicit params, predictable per-call billing. Here's what looks closest:
>
> 1️⃣ **Kling O3.Pro · FLF to Video (channel edition)**
>
> - **Summary**: O3-architecture first/last-frame video model, supports first/last-frame constraints with smooth motion control.
> - **Category**: FLF to Video
> - **Runnable directly**: ✅ Yes, after parameter confirmation
> - **endpoint**: `kling-o3-pro-base/flf-to-video`
>
> 2️⃣ **Kling O3.Pro · text-to-video (channel edition)**
>
> - **Summary**: O3-architecture text-to-video model, sharp subjects, smooth motion, natural lighting.
> - **Category**: Text to Video
> - **Runnable directly**: ✅ Yes, after parameter confirmation
> - **endpoint**: `kling-o3-pro-base/text-to-video`
>
> Tell me a number or endpoint name and I'll move on to the parameter card.

#### Example 2: find an AI app / workflow

For example, you can say:
> "I want to generate an architectural render. Can you check what BizyAir workflows would suit it?"

The assistant will respond like:
> 🎯 **Found some BizyAir objects that fit your needs better**
>
> I filtered out the weak matches. Here are the ones worth looking at:
>
> 1️⃣ **FLUX建筑效果图高清修复**
>
> - **Best for**: Flux.1 D based route, suitable as a candidate for this round.
> - **Runnable directly**: ✅ Yes
> - **Link**: https://bizyair.cn/community/app/44479
> - **ID**: `44479`
>
> 2️⃣ **建筑效果图放大**
>
> - **Best for**: Flux.2 Klein based route, suitable as a candidate for this round.
> - **Runnable directly**: ✅ Yes
> - **Link**: https://bizyair.cn/community/app/44396
> - **ID**: `44396`
>
> Which one do you like? Just tell me the number or ID and I'll proceed.

---

## 👨‍💻 Developer & Debug Commands


**Image / Video base flow:**
```bash
# Show common image/video route menus
python3 scripts/cli.py image-menu
python3 scripts/cli.py video-menu

# Fixed search routes (AI app candidates)
python3 scripts/cli.py pick-image "<keyword>" --remote
python3 scripts/cli.py pick-video "<keyword>" --remote

# ModelZoo endpoint candidates (menu slot 6 / v6)
python3 scripts/cli.py pick-modelzoo-image "<keyword>"
python3 scripts/cli.py pick-modelzoo-video "<keyword>"

# Generate a prefilled confirmation card (fixed routes)
python3 scripts/dispatch.py --model <ROUTE_ID> --prefill-card --prompt "<request>"

# General BizyAir platform search (apps / workflows / creations / MCP)
python3 scripts/cli.py search "<keyword>" --remote

# Browse in-site app listings (filterable by type/sort)
python3 scripts/cli.py browse --remote-source community --modality Application --sort "Most Used"

# Multi-task / parallel — generate batch parameter cards first
python3 scripts/cli.py batch-prefill --model <ROUTE_ID> --batch-json '<JSON task array>'
# batch-json format examples (each item can be a string or object):
# Short: '["promptA", "promptB", "promptC"]'
# Full: '[{"prompt":"promptA","aspect_ratio":"16:9"}, {"prompt":"promptB"}]'
# With concurrency: '{"concurrency":2,"tasks":["promptA","promptB"]}'

# Batch confirmed execution (run after prefill confirmation)
python3 scripts/cli.py batch-run --model <ROUTE_ID> --batch-json '<JSON task array>' --confirm-run

# Execute a fixed route after explicit confirmation (single task)
python3 scripts/dispatch.py --model <ROUTE_ID> --prompt "<prompt>" [other params] --confirm-run
```

**AI App / Workflow advanced flow:**
```bash
# Inspect object details and execution support from a link or ID
python3 scripts/cli.py info "<link-or-id>"

# Generate a prefilled confirmation card for a remote app
python3 scripts/cli.py prefill "<link-or-id>" --prompt "<request>"

# Submit execution for a specific App
python3 scripts/cli.py run <APP_ID> --prompt "<prompt>"
```

**ModelZoo model services:**
```bash
# Search ModelZoo endpoints
python3 scripts/cli.py modelzoo-list --keyword "<keyword>"

# View endpoint details and pricing
python3 scripts/cli.py modelzoo-detail <endpoint>
python3 scripts/cli.py modelzoo-price <endpoint>

# Execute a ModelZoo task
python3 scripts/cli.py modelzoo-run <endpoint> --param prompt="<prompt>"

# Query ModelZoo async task status
python3 scripts/cli.py modelzoo-status <request_id>
```

**Account utilities:**
```bash
# Validate API key and check balance
python3 scripts/cli.py check
python3 scripts/cli.py wallet
```
