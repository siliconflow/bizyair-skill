"""modelzoo.py — BizyAir ModelZoo 任务模块。

覆盖：列表 / 详情 / 价格 / 创建任务 / 轮询状态 / 调用记录。
与 webapp（AI 应用）的区别：
  - create 始终异步（< 5s 返回 request_id），不需要 X-Bizyair-Task-Async header
  - field_name 直接是 payload key（不需要 contract 映射）
  - field_options 已经是 dict（不需要 JSON.parse）
  - field_type=images 必须传 list[str]
  - outputs 四种字段并存：{texts, images, videos, audios}
"""
from __future__ import annotations

import json
import sys
import time
from typing import Any

from common import API_BASE, POLL_INTERVAL, MAX_POLL_SECONDS

import api

X_BASE = f"{API_BASE}/x/v1"


def _coerce_param(field_type: str, value: Any) -> Any:
    """根据 field_type 强制转换参数类型，避免服务端 500。"""
    if value is None:
        return value
    ft = (field_type or "").lower()
    if ft in ("number", "slider", "slides"):
        try:
            return float(value) if "." in str(value) else int(value)
        except (ValueError, TypeError):
            return value
    if ft == "seed":
        try:
            return int(value)
        except (ValueError, TypeError):
            return -1
    if ft == "boolean":
        if isinstance(value, bool):
            return value
        return str(value).lower() in ("true", "1", "yes")
    if ft == "images":
        if isinstance(value, list):
            return value
        return [str(value)]  # 单张也包成 list
    return value


# 参数名别名映射。ModelZoo 不同 endpoint 的 field_name 命名不统一，
# 这里定义 field_name → 可能的别名列表。当 user_params 里没有精确匹配时，
# build_task_payload 会尝试从别名取值。
_FIELD_ALIASES: dict[str, list[str]] = {
    'ratio': ['aspect_ratio'],           # happyhorse/wan 用 ratio
    'aspect_ratio': ['ratio'],           # 反向
    'size': ['resolution', 'image_size'], # seedream 用 size
    'resolution': ['size', 'image_size'], # 反向
    'image_size': ['resolution', 'size'], # 部分 endpoint
}


def _resolve_user_param(field_name: str, user_params: dict[str, Any]) -> tuple[Any, bool]:
    """从 user_params 取值，支持别名回退。返回 (value, found)。"""
    if field_name in user_params:
        return user_params[field_name], True
    for alias in _FIELD_ALIASES.get(field_name, []):
        if alias in user_params:
            return user_params[alias], True
    return None, False


def _strip_default_media(value: Any, default_urls: set[str]) -> Any:
    """从用户传的媒体值里剥离混入的默认示例 URL。

    场景：Agent 看到 detail 里的 field_value 是 ["demo.jpg"]，
    用户又提供了自己的图片，Agent 可能错误地组装成 ["user.jpg", "demo.jpg"]。
    本函数把属于默认值的 URL 剔除，只保留用户自己的。
    如果剔除后为空（说明用户传的全是默认值），则返回原值不做处理。
    """
    if isinstance(value, list):
        filtered = [u for u in value if str(u) not in default_urls]
        return filtered if filtered else value
    if isinstance(value, str) and value in default_urls:
        return value  # 单值且就是默认 → 不剥（否则字段会空）
    return value


def list_endpoints(
    api_key: str,
    *,
    keyword: str = "",
    tags: list[str] | None = None,
    categories: list[str] | None = None,
    sort: str = "Auto",
    page: int = 1,
    page_size: int = 20,
    show_deprecated: bool = False,
) -> dict[str, Any]:
    """搜索 modelzoo endpoint 列表。"""

    url = f"{X_BASE}/modelzoo/list?current={page}&page_size={page_size}"
    if keyword:
        url += f"&keyword={keyword}"
    if sort:
        url += f"&sort={sort}"
    body = {
        "tags": tags or [],
        "categories": categories or [],
        "show_deprecated": show_deprecated,
    }
    return api.safe_request_json("POST", url, api_key, payload=body)


def get_detail(api_key: str, endpoint: str) -> dict[str, Any]:
    """获取 endpoint 详情（input_params + outputs_example）。"""

    return api.safe_request_json("GET", f"{X_BASE}/modelzoo/detail/{endpoint}", api_key)


def get_price(api_key: str, endpoint: str) -> dict[str, Any]:
    """获取 endpoint 价格表。优先用 simple_price_text 展示。"""

    return api.safe_request_json("GET", f"{X_BASE}/modelzoo/price_table/{endpoint}", api_key)


def build_task_payload(
    detail_data: dict[str, Any],
    user_params: dict[str, Any],
    *,
    media_overrides: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """根据 detail 的 input_params + 用户参数构建 create payload。

    规则（优先级从高到低）：
    1. media_overrides：按 field_type 匹配（images/audios/videos），
       整体替换默认值，不与默认值合并。用于 --image/--audio/--video 上传的文件。
    2. user_params：用户通过 --param key=value 显式传的参数
    3. input_params 的 field_value（default）：兜底默认值
    - 按 field_type 做类型强转

    media_overrides 格式：{"images": ["url1", ...], "audios": ["url1"], ...}
    """
    input_params = detail_data.get("input_params") or []
    payload: dict[str, Any] = {}
    overrides = media_overrides or {}
    consumed_media_types: set[str] = set()

    # 收集所有媒体字段的默认 URL，用于兜底过滤
    _default_media_urls: set[str] = set()
    for param in input_params:
        ft = (param.get("field_type") or "").lower()
        if ft in ("images", "audios", "videos"):
            dv = param.get("field_value")
            if isinstance(dv, list):
                _default_media_urls.update(str(u) for u in dv)
            elif isinstance(dv, str) and dv:
                _default_media_urls.add(dv)

    for param in input_params:
        field_name = param.get("field_name")
        field_type = param.get("field_type") or ""
        default_value = param.get("field_value")
        ft = field_type.lower()

        if not field_name:
            continue

        # media_overrides 最高优先：匹配 field_type，整体替换默认值
        if ft in overrides and ft not in consumed_media_types:
            payload[field_name] = _coerce_param(field_type, overrides[ft])
            consumed_media_types.add(ft)
            continue

        # 用户通过 --param 显式传的参数（支持别名回退）
        user_value, user_found = _resolve_user_param(field_name, user_params)
        raw_value = user_value if user_found else default_value

        # 兜底：媒体字段如果用户显式传了值，自动剥离混入的默认示例 URL
        if ft in ("images", "audios", "videos") and user_found and _default_media_urls:
            raw_value = _strip_default_media(raw_value, _default_media_urls)

        # 跳过 None 且非必填的
        if raw_value is None and not param.get("required"):
            continue

        payload[field_name] = _coerce_param(field_type, raw_value)

    return payload


def create_task(api_key: str, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
    """创建 modelzoo 任务。始终异步，< 5s 返回 request_id。"""

    return api.safe_request_json(
        "POST",
        f"{X_BASE}/modelzoo/tasks/openapi/{endpoint}",
        api_key,
        payload=payload,
    )


def query_task(api_key: str, request_id: str) -> dict[str, Any]:
    """查询 modelzoo 任务状态。"""

    return api.safe_request_json(
        "GET",
        f"{X_BASE}/modelzoo/tasks/openapi/{request_id}",
        api_key,
    )


def poll_until_done(api_key: str, request_id: str) -> dict[str, Any]:
    """轮询直到任务完成或失败。返回最终 data。"""

    start = time.time()
    last_status = None

    while time.time() - start < MAX_POLL_SECONDS:
        result = query_task(api_key, request_id)
        data = (result.get("data") or {}).get("data") or result.get("data") or {}
        status = data.get("status")

        if status != last_status:
            print(f"STATUS:{status}", file=sys.stderr)
            last_status = status

        if status == "Success":
            return data
        if status == "Failed":
            message = data.get("message") or "Unknown error"
            print(
                json.dumps(
                    {"error": "MODELZOO_TASK_FAILED", "status": status, "message": message},
                    ensure_ascii=False,
                ),
                file=sys.stderr,
            )
            return data

        time.sleep(POLL_INTERVAL)

    print(
        json.dumps({"error": "TIMEOUT", "message": "Polling timed out", "request_id": request_id}, ensure_ascii=False),
        file=sys.stderr,
    )
    return {"status": "Timeout", "request_id": request_id}


def get_mycalls(
    api_key: str,
    *,
    call_type: str = "trd_api_record",
    endpoint: str | None = None,
    status: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> dict[str, Any]:
    """查询调用记录列表。"""

    body: dict[str, Any] = {"call_type": call_type}
    if endpoint:
        body["endpoint"] = endpoint
    if status:
        body["status"] = status

    return api.safe_request_json(
        "POST",
        f"{X_BASE}/modelzoo/mycalls?current={page}&page_size={page_size}",
        api_key,
        payload=body,
    )


def get_mycall_detail(api_key: str, call_type: str, request_id: str) -> dict[str, Any]:
    """查询单条调用详情（含 usage.charge_amount）。"""

    return api.safe_request_json(
        "GET",
        f"{X_BASE}/modelzoo/mycalls/{call_type}/{request_id}",
        api_key,
    )


# ---- ModelZoo endpoint 候选搜索（菜单 6 号 / v6 入口）----

# Heuristic 视频信号词。命中即判定为视频侧 endpoint。
# 关键词来自 ModelZoo 现有视频类 endpoint 的 display_name / category / endpoint slug。
_MODELZOO_VIDEO_SIGNALS = (
    "video",
    "视频",
    "t2v",
    "i2v",
    "image-to-video",
    "text-to-video",
    "lipsync",
    "lip-sync",
    "口型",
    "kling",
    "wan",
    "seedance",
    "veo",
    "vidu",
    "ltx",
    "hunyuan",
    "happyhorse",
)


def _modelzoo_item_text_blob(item: dict[str, Any]) -> str:
    """把一个 modelzoo list 项的几个文本字段合并成一坨小写 blob，用于关键词命中。"""
    parts = [
        str(item.get("display_name") or ""),
        str(item.get("category") or ""),
        str(item.get("endpoint") or ""),
        str(item.get("description") or ""),
        str(item.get("introduction") or ""),
    ]
    return " ".join(parts).lower()


def is_video_modelzoo_endpoint(item: dict[str, Any]) -> bool:
    """根据 display_name / category / endpoint 文本判断是否视频侧 endpoint。"""
    blob = _modelzoo_item_text_blob(item)
    return any(signal in blob for signal in _MODELZOO_VIDEO_SIGNALS)


def summarize_modelzoo_candidate(item: dict[str, Any]) -> dict[str, Any]:
    """挑出展示给用户的几个字段，避免把整个 raw item 透传出去。"""
    endpoint = item.get("endpoint")
    return {
        "endpoint": endpoint,
        "display_name": item.get("display_name"),
        "category": item.get("category"),
        "description": item.get("description") or item.get("introduction"),
        "cover_image": item.get("cover_url") or item.get("icon") or item.get("cover"),
        "tags": item.get("tags") or [],
    }


def _modelzoo_number_badge(index: int) -> str:
    badges = {1: "1️⃣", 2: "2️⃣", 3: "3️⃣", 4: "4️⃣", 5: "5️⃣"}
    return badges.get(index, f"{index}.")


def build_modelzoo_reply_markdown(candidates: list[dict[str, Any]], *, modality: str) -> str:
    """构建给用户看的 reply_markdown。和 search.py 保持同样的 6 字段卡片结构（标题/简介/封面/能否直接执行/链接/ID）。"""
    if not candidates:
        return (
            "📭 **这轮 ModelZoo 没找到合适的 endpoint**\n"
            "我换词试了几轮，当前还是没有特别贴题的结果。\n\n"
            "你可以换个更短一点的关键词，或者去 7 号 AI 应用检索看看现成的工作流模板～"
        )
    heading = "🎯 **从 ModelZoo 给你捞了几个底层 endpoint**" if modality != "video" else "🎯 **从 ModelZoo 给你捞了几个视频侧 endpoint**"
    intro = "ModelZoo 走的是底层模型 API，参数明确、按次扣费稳定。下面这几个看下哪个对路："
    lines = [heading, intro, ""]
    for index, item in enumerate(candidates, start=1):
        title = item.get("display_name") or item.get("endpoint") or f"endpoint {index}"
        lines.append(f"{_modelzoo_number_badge(index)} **{title}**")
        description = item.get("description")
        if description:
            text = str(description).strip().replace("\n", " ")
            if len(text) > 120:
                text = text[:120] + "..."
            lines.append(f"- **简介**：{text}")
        if item.get("category"):
            lines.append(f"- **分类**：{item['category']}")
        cover = item.get("cover_image")
        if cover:
            lines.append(f"- **封面**：![{title}]({cover})")
        lines.append("- **能否直接执行**：✅ 支持，参数确认后即可开跑")
        if item.get("endpoint"):
            lines.append(f"- **endpoint**：`{item['endpoint']}`")
            lines.append(
                f"- **下一步**：`cli.py modelzoo-detail {item['endpoint']}` 看参数 / "
                f"`cli.py modelzoo-price {item['endpoint']}` 看价格"
            )
        lines.append("")
    lines.append("告诉我编号或 endpoint 名，我接着往下帮你出参数卡。")
    return "\n".join(lines).strip()


def pick_endpoint_candidates(
    api_key: str,
    query: str,
    modality: str,
    *,
    limit: int = 10,
    page_size: int = 30,
    max_pages: int = 3,
) -> dict[str, Any]:
    """搜 ModelZoo endpoint 并按模态过滤，返回 reply_markdown + 结构化 candidates。

    最简版：复用 list_endpoints 的 keyword 搜索，按服务端默认排序（sort=Auto）取前 N，
    然后用 is_video_modelzoo_endpoint 做客户端模态分流。复杂打分（命中度 + 价格 +
    used_count 加权）留给后续迭代。
    """

    keyword = (query or "").strip()
    is_video = (modality or "").strip().lower() == "video"
    seen_endpoints: set[str] = set()
    matched: list[dict[str, Any]] = []

    for current_page in range(1, max_pages + 1):
        result = list_endpoints(
            api_key,
            keyword=keyword,
            page=current_page,
            page_size=page_size,
            sort="Auto",
        )
        data = (result.get("data") or {}).get("data") or result.get("data") or {}
        items = data.get("list") or []
        if not items:
            break
        for item in items:
            endpoint = str(item.get("endpoint") or "").strip()
            if not endpoint or endpoint in seen_endpoints:
                continue
            seen_endpoints.add(endpoint)
            item_is_video = is_video_modelzoo_endpoint(item)
            if is_video and not item_is_video:
                continue
            if not is_video and item_is_video:
                continue
            matched.append(item)
            if len(matched) >= limit:
                break
        if len(matched) >= limit:
            break

    candidates = [summarize_modelzoo_candidate(it) for it in matched[:limit]]
    return {
        "source": "modelzoo-pick",
        "modality": "video" if is_video else "image",
        "query": keyword,
        "candidates": candidates,
        "reply_markdown": build_modelzoo_reply_markdown(candidates, modality="video" if is_video else "image"),
    }
