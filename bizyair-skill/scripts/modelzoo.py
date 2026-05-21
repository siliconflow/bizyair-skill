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
