
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

> BizyAir Skill 是适配主流 AI Agent 的 AIGC 技能包，依托 BizyAir 云端算力，无需复杂配置与调参<br>**自然语言即可一键跑通从创意到图片、视频成片的全流程创作，让 AI Agent 轻松拥有专业级 AIGC 能力**

</div>


<a href="https://skills.sh"><img src="https://img.shields.io/badge/skills.sh-Compatible-green?style=for-the-badge" alt="Skills"></a>
<a href="https://agentskills.io"><img src="https://img.shields.io/badge/AgentSkills-Standard-blue?style=for-the-badge" alt="AgentSkills"></a>
<a href="https://claude.ai/code"><img src="https://img.shields.io/badge/Claude%20Code-Skill-blueviolet?style=for-the-badge" alt="Claude Code"></a>


[安装方式](#-安装方式) · [使用方式](#-怎么跟它说话) · [效果示例](#-它会怎么和你交互) · [CLI命令](#-开发者与调试命令)



**[中文](./README.md) · [English](./README.en.md)**
</div>

****



## ✨ 它可以帮你做什么

| 核心能力 | 覆盖场景 | 交互特性 |
| :--- | :--- | :--- |
| **图片生成** | 文生图、图生图、图片编辑、扩图 | **5 精选模型** + ModelZoo 搜索 + AI 应用检索 |
| **视频生成** | 文生视频、图生视频、对口型等 | **5 精选模型** + ModelZoo 搜索 + AI 应用检索 |
| **AI 应用与 ModelZoo** | 解析并运行各类 BizyAir 应用与底层模型 | 支持链接直达、信息解析、双轨搜索 |
| **智能参数引擎** | 意图补全、提示词润色 | "预填确认卡"，拒绝空白问卷 |

---

## ⭐️ 前置条件

- **Python 3.9+**
- 已准备 BizyAir API key（登录账号后，将鼠标悬停在右上角头像上，点击[「API 密钥」](https://bizyair.cn/user/api-key)即可创建）
- 账户内有可用算力余额

---

## 🚀 安装方式

在 Agent 对话中发送：

> 帮我从` https://github.com/siliconflow/bizyair-skill `安装 BizyAir 技能


或者直接执行代码：

```bash
gh skill install siliconflow/bizyair-skill
```

---

###  配置文件说明（如何配置API Key）

**方式 A：直接用环境变量**

```bash
export BIZYAIR_API_KEY="你的 BizyAir API Key"
```

**方式 B：写进配置文件**

本 skill 根目录下的 `config.json`，用来放置 BizyAir API key ，和并发的相关配置。

你可以手动改，也可以直接把 API KEY 给到助手，让它帮你写进去。**（推荐）**

```json
{
  "credentials": {
    "api_key": "BizyAir API Key 放在这"
  },
  "batch": {
    "max_concurrency": 3,
    "max_tasks": 5
  }
}
```

> **说明**：`max_concurrency` 最大并发数量，默认 3，上限 5；`max_tasks` 单批最大可执行任务量，默认 5，上限 10。

---

## 💬 怎么跟它说话

配置完成后，抛开代码，直接用自然语言跟助手提需求（可用此句式："帮我用 bizyair 去 xxx....."）：

**🎨 玩转图片：**
* "帮我用 bizyair 画一张暖棕色调的咖啡广告海报。"
* "用 bizyair 做一张二次元动漫头像，要唯美一点的。"
* "我想出图，你先给我看看 bizyair 常用菜单。"

**🎬 玩转视频：**
* "用 bizyair 帮我做一个雨夜霓虹街头的短视频，10秒钟。"
* "让这张人物图动起来，用 bizyair 。"
* "给这张人物图配这段音频，用 bizyair 做个对口型视频。"

**🧩 玩转 AI 应用 / Workflow：**
* "跑一下这个 bizyAir 应用：`https://bizyair.cn/...`"
* "帮我看看这个bizyair  workflow ID 是干嘛的，需要传什么素材？"
* "帮我找几个适合做电商主图精修的 bizyAir workflow。"

**🛠️ 查状态与工具：**
* "检查一下我的 bizyAir API key。"
* "帮我查一下 bizyAir 的算力余额。"

---

## 🤖 它会怎么和你交互

### 1. 视频 / 图片精选菜单
当你只给出模糊需求（例如"帮我做个视频"）时，助手不会擅自替你决定，而是会弹出精选模型菜单。

比如，你可以说：
> "我要做视频，你先给我 BizyAir 的常用视频入口。"

助手会这样回：
> "先挑个模型 👇
>
> 【⭐ 精选模型 · 5 个常用视频模型】
> 1. 🎬 通用视频V.3.1.Pro：电影感、镜头感、压场质感。
> 2. 🐎 HappyHorse：出片快、性价比舒服。
> 3. 🐉 可灵3.0.Pro：动作、运动、冲击力更敢打，原生 4K。
> 4. 🌊 万相2.7：中文理解强、音频同步、多镜头编排。
> 5. 💃 Seedance 2.0：人物动作、舞蹈、角色一致性更强。
>
> 【🧪 ModelZoo 搜索 · ✨ NEW 推荐】
>
> 6. 直接调底层模型 API，参数明确，按次扣费稳定。
>
> 【🔎 AI 应用检索】
>
> 7. 找现成的工作流模板，复杂多步流程一键跑。
>
> 我建议你优先看看 4 号～ 你这类还没把风格卡太死，那先看 4 号万相2.7，一般会更稳。
> 回 1-7 就行；你明确说模型名、编号，也可以直接按那个走。"

### 2. 链接诊断
如果你丢来一个陌生的 App 链接，助手会先去底层抓取化验单（`info`），然后用自然语言告诉你：

* 这个应用是做什么的。
* 它需要你提供什么素材（图片/音频/文本）。
* 当前是否支持直接代跑，或者需要你前往网页端操作。

比如，你可以说：
> "帮我看看这个 BizyAir 应用：https://bizyair.cn/community/app/46086"

助手会这样回：
> "底层化验单出来了，这套流程很健康～
> 我刚去后台帮你把这个链接的底牌看穿了，情况是这样的：
>
> - **它是干嘛的**：底层挂的是 **Wai-Illustrious 二次元文生图工作流**，附带高清放大及手部脸部修复。
> - **你要准备啥**：不需要垫图，只要给我一句到位的提示词就行。
> - **出什么货**：直接出一组完整的图片成品（含高清放大版）。
> - **执行判定**：✅ 接口完全畅通，支持我直接替你代跑～
>
> 你看，咱们是直接开始构思画面，还是我先帮你把尺寸、比例的参数卡拉出来过个目？"

> **注意**：comfy-ui 链接（`bizyair.cn/comfy-ui?id=xxx`）目前不支持直接运行，助手会提示你去找对应的 AI 应用版本。

### 3. 智能参数确认卡
告别繁琐的参数填空题！选定模型后，助手会吸收你的自然语言和已有素材，为你生成一张 **"预填确认卡"**。

比如，你可以说：
> "我用 4 号通用图片O.2。做一张中文咖啡海报，横版。"

助手会这样回：
> "## 🎨 通用图片O.2 参数确认
>
> **提示词**：中文咖啡海报，横版
> **比例**：3:2 （可选：1:1 / 2:3 / 3:2 / 4:5 / 5:4 / 3:4 / 4:3 / 16:9 / ...）
> **分辨率**：2K （可选：1K / 2K / 4K）
>
> 确认参数后，回我"开跑"就立即生成，或者你想改比例/分辨率也可以直接说～"

再比如，你可以说：
> "就用通用图片B.Pro 跑吧。做一个未来城市夜景海报，蓝紫色，带雨和霓虹，酷一点，像电影宣传图。"

助手会这样回：
> "## 📷 通用图片B.Pro 参数确认
>
> **比例**：5:4 （可选：1:1 / 16:9 / 9:16 / 4:3 / 3:4 / 3:2 / 2:3 / 5:4 / ...）
> **分辨率**：2K （可选：1K / 2K / 4K）
> **提示词**：未来城市夜景海报，蓝紫色，带雨和霓虹，酷一点，像电影宣传图
>
> 没问题的话直接回我"开跑" / "直接跑" / "确认执行"就行；如果你还想改参数，也可以继续说～"

### 4. 双轨搜索推荐
精选模型不够用？除了 5 个固定入口，助手还能根据你的需求走两条搜索路线：

- **6 号 ModelZoo 搜索**：直接调底层模型 API，参数明确、按次扣费稳定。适合「我要 Kling 视频」「我想直接调某个 endpoint」这种已经知道大致路线的场景。
- **7 号 AI 应用检索**：BizyAir 站内的工作流模板，复杂多步流程一键跑。适合「我要个能改背景的工作流」「找现成的电商主图模板」这种偏组合方案的场景。

#### 例 1：找 ModelZoo 底层 endpoint

比如，你可以说：
> "我想直接调底层视频模型 API，搜个 kling 的。"

助手会这样回：
> 🎯 **从 ModelZoo 给你捞了几个视频侧 endpoint**
>
> ModelZoo 走的是底层模型 API，参数明确、按次扣费稳定。下面这几个看下哪个对路：
>
> 1️⃣ **可灵O3.Pro-首尾帧-渠道版**
>
> - **简介**：可灵O3.Pro渠道版首尾帧生视频模型，依托新一代O3架构，支持首尾帧约束生成，可精准把控画面运动轨迹。
> - **分类**：FLF to Video
> - **能否直接执行**：✅ 支持，参数确认后即可开跑
> - **endpoint**：`kling-o3-pro-base/flf-to-video`
> - **下一步**：`cli.py modelzoo-detail kling-o3-pro-base/flf-to-video` 看参数 / `cli.py modelzoo-price kling-o3-pro-base/flf-to-video` 看价格
>
> 2️⃣ **可灵O3.Pro-文生视频-渠道版**
>
> - **简介**：可灵O3.Pro渠道版文生视频模型，基于O3架构，支持文本直接生成视频。画面主体清晰、运动流畅、光影自然。
> - **分类**：Text to Video
> - **能否直接执行**：✅ 支持，参数确认后即可开跑
> - **endpoint**：`kling-o3-pro-base/text-to-video`
> - **下一步**：`cli.py modelzoo-detail kling-o3-pro-base/text-to-video` 看参数 / `cli.py modelzoo-price kling-o3-pro-base/text-to-video` 看价格
>
> 告诉我编号或 endpoint 名，我接着往下帮你出参数卡。

#### 例 2：找 AI 应用 / 工作流

比如，你可以说：
> "我想生成个建筑的效果图，你看看 bizyair 上有啥合适的工作流吗？"

助手会这样回：
> 🎯 **给你捞了几个更对路的 BizyAir 对象**
>
> 我先把明显不贴题的过滤掉了，下面这几个更值得看：
>
> 1️⃣ **FLUX建筑效果图高清修复**
>
> - **最适合做什么**：底层路线更偏 Flux.1 D，适合作为这轮候选继续往下看。
> - **能否直接执行**：✅ 支持直接开跑
> - **链接**：https://bizyair.cn/community/app/44479
> - **ID**：`44479`
>
> 2️⃣ **建筑效果图放大**
>
> - **最适合做什么**：底层路线更偏 Flux.2 Klein，适合作为这轮候选继续往下看。
> - **能否直接执行**：✅ 支持直接开跑
> - **链接**：https://bizyair.cn/community/app/44396
> - **ID**：`44396`
>
> 你看中哪个？直接告诉我编号或者 ID，我就接着往下帮你跑。

---

## 👨‍💻 开发者与调试命令 


**图片 / 视频基础流：**
```bash
# 查看图片/视频常用入口菜单
python3 scripts/cli.py image-menu
python3 scripts/cli.py video-menu

# 走固定搜索入口（AI 应用候选）
python3 scripts/cli.py pick-image "<关键词>" --remote
python3 scripts/cli.py pick-video "<关键词>" --remote

# ModelZoo 底层 endpoint 候选搜索（菜单 6 号 / v6）
python3 scripts/cli.py pick-modelzoo-image "<关键词>"
python3 scripts/cli.py pick-modelzoo-video "<关键词>"

# 基于用户需求生成预填确认卡（固定入口）
python3 scripts/dispatch.py --model <ROUTE_ID> --prefill-card --prompt "<需求>"

# 通用 BizyAir 平台搜索（AI 应用 / Workflow / 作品 / MCP）
python3 scripts/cli.py search "<关键词>" --remote

# 浏览站内应用列表（可按类型/排序筛选）
python3 scripts/cli.py browse --remote-source community --modality Application --sort "Most Used"

# 多任务 / 并行请求先出批量参数卡，不直接开跑
python3 scripts/cli.py batch-prefill --model <ROUTE_ID> --batch-json '<JSON任务数组>'
# batch-json 格式示例（数组中每项可以是字符串或对象）：
# 简写：'["提示词A", "提示词B", "提示词C"]'
# 完整：'[{"prompt":"提示词A","aspect_ratio":"16:9"}, {"prompt":"提示词B"}]'
# 也支持带并发控制的对象格式：'{"concurrency":2,"tasks":["提示词A","提示词B"]}'

# 批量确认执行（prefill 确认后再跑这条）
python3 scripts/cli.py batch-run --model <ROUTE_ID> --batch-json '<JSON任务数组>' --confirm-run

# 用户明确确认后，再执行固定入口（单任务）
python3 scripts/dispatch.py --model <ROUTE_ID> --prompt "<提示词>" [按需参数] --confirm-run
```

**AI 应用 / Workflow 进阶流：**
```bash
# 从链接或 ID 获取对象详细信息与执行支持情况
python3 scripts/cli.py info "<链接或ID>"

# 给远端 app 生成预填确认卡
python3 scripts/cli.py prefill "<链接或ID>" --prompt "<需求>"

# 提交执行具体的 App
python3 scripts/cli.py run <APP_ID> --prompt "<提示词>"
```

**ModelZoo 模型服务：**
```bash
# 搜索 ModelZoo endpoint
python3 scripts/cli.py modelzoo-list --keyword "<关键词>"

# 查看 endpoint 详情与价格
python3 scripts/cli.py modelzoo-detail <endpoint>
python3 scripts/cli.py modelzoo-price <endpoint>

# 执行 ModelZoo 任务
python3 scripts/cli.py modelzoo-run <endpoint> --param prompt="<提示词>"

# 查询 ModelZoo 异步任务状态
python3 scripts/cli.py modelzoo-status <request_id>
```

**账户工具：**
```bash
# 验证 API Key 与查询余额
python3 scripts/cli.py check
python3 scripts/cli.py wallet
```
