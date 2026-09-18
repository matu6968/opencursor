from __future__ import annotations

import inspect
import json
from typing import Any, Mapping

CUSTOM_USER_TOOLS_PROVIDER = "custom-user-tools"
_DEFAULT_INPUT_SCHEMA: dict[str, Any] = {"type": "object", "properties": {}, "additionalProperties": True}


def custom_tools_from_options(options: Any, send_options: Any | None = None) -> dict[str, Any]:
    local_send = getattr(send_options, "local", None) if send_options is not None else None
    if local_send is not None and getattr(local_send, "customTools", None) is not None:
        return dict(local_send.customTools or {})
    local = getattr(options, "local", None)
    if local is not None and getattr(local, "customTools", None):
        return dict(local.customTools)
    return {}


def custom_tools_configured(options: Any, send_options: Any | None = None) -> bool:
    return bool(custom_tools_from_options(options, send_options))


def custom_tool_definitions(tools: Mapping[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for name, tool in tools.items():
        schema = getattr(tool, "inputSchema", None)
        if schema is None and isinstance(tool, dict):
            schema = tool.get("inputSchema")
        if schema is None:
            schema = _DEFAULT_INPUT_SCHEMA
        description = getattr(tool, "description", None)
        if description is None and isinstance(tool, dict):
            description = tool.get("description")
        item: dict[str, Any] = {
            "name": f"{CUSTOM_USER_TOOLS_PROVIDER}-{name}",
            "provider_identifier": CUSTOM_USER_TOOLS_PROVIDER,
            "tool_name": str(name),
            "description": str(description or ""),
            "input_schema": schema,
            "input_schema_json": json.dumps(schema),
        }
        output_schema = getattr(tool, "outputSchema", None)
        if output_schema is None and isinstance(tool, dict):
            output_schema = tool.get("outputSchema")
        if output_schema is not None:
            item["output_schema_json"] = json.dumps(output_schema)
        annotations = getattr(tool, "annotations", None)
        if annotations is None and isinstance(tool, dict):
            annotations = tool.get("annotations")
        if annotations is not None:
            if hasattr(annotations, "model_dump"):
                annotations = annotations.model_dump(by_alias=True, exclude_none=True)
            item["annotations_json"] = json.dumps(annotations)
        out.append(item)
    return out


def _tool_execute(tool: Any) -> Any:
    if callable(getattr(tool, "execute", None)):
        return tool.execute
    if isinstance(tool, dict) and callable(tool.get("execute")):
        return tool["execute"]
    if callable(tool):
        return tool
    raise KeyError("execute")


async def execute_custom_tool(tools: Mapping[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    tool_name = str(args.get("tool_name") or args.get("name") or "")
    if tool_name.startswith(f"{CUSTOM_USER_TOOLS_PROVIDER}-"):
        tool_name = tool_name[len(CUSTOM_USER_TOOLS_PROVIDER) + 1 :]
    tool = tools.get(tool_name)
    if tool is None:
        return {
            "tool_not_found": {
                "name": tool_name,
                "available_tools": list(tools),
            }
        }
    try:
        execute = _tool_execute(tool)
    except KeyError:
        return {"error": {"error": f"custom tool {tool_name!r} has no execute callback"}}
    call_args = args.get("args") if isinstance(args.get("args"), dict) else {}
    context = {"toolCallId": str(args.get("tool_call_id") or "")}
    try:
        result = execute(call_args, context)
        if inspect.isawaitable(result):
            result = await result
    except Exception as exc:
        return {"error": {"error": str(exc)}}
    return {"success": _custom_tool_success(result)}


def _custom_tool_success(result: Any) -> dict[str, Any]:
    if isinstance(result, str):
        return {"content": [{"text": {"text": result}}], "is_error": False}
    if isinstance(result, dict) and "content" in result:
        items: list[dict[str, Any]] = []
        for part in result.get("content") or []:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "image":
                items.append(
                    {
                        "image": {
                            "data": part.get("data") or b"",
                            "mime_type": part.get("mimeType") or "image/png",
                        }
                    }
                )
            else:
                items.append({"text": {"text": str(part.get("text") or "")}})
        payload: dict[str, Any] = {"content": items, "is_error": bool(result.get("isError"))}
        if isinstance(result.get("structuredContent"), dict):
            payload["structured_content"] = result["structuredContent"]
        return payload
    if isinstance(result, dict):
        return {"content": [{"text": {"text": json.dumps(result)}}], "is_error": False}
    return {"content": [{"text": {"text": str(result)}}], "is_error": False}


def is_custom_user_tools_call(args: dict[str, Any]) -> bool:
    provider = str(args.get("provider_identifier") or args.get("server_identifier") or "")
    name = str(args.get("name") or "")
    return provider == CUSTOM_USER_TOOLS_PROVIDER or name.startswith(f"{CUSTOM_USER_TOOLS_PROVIDER}-")
