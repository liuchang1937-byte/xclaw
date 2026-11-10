"""恢复纯 DSML 工具消息；不把说明文字、代码块或未知工具当作可执行调用。"""

from __future__ import annotations

import json
import re
from uuid import uuid4

from jsonschema.validators import validator_for
from referencing import Registry

from xclaw.ai.types import ChatResponse, FunctionCall, ToolCall, ToolDefinition


class InvalidToolCallResponseError(ValueError):
    """模型返回了无法安全恢复的工具协议，调用方应纠正重试。"""


_PREFIX = r"<\s*([/]?)\s*[|｜]\s*DSML\s*[|｜]\s*"


def normalize_tool_response(
    response: ChatResponse,
    tools: list[ToolDefinition] | None,
) -> ChatResponse:
    content = response.content or ""
    if not re.search(_PREFIX, content, re.IGNORECASE):
        return response
    if response.tool_calls:
        return response.model_copy(update={"content": None})
    # 只允许整条回复是完整调用。混合自然语言/代码示例必须让模型重新输出。
    normalized = re.sub(_PREFIX, lambda match: "<" + match.group(1), content).strip()
    block = re.fullmatch(r"<tool_calls>\s*(.*?)\s*</tool_calls>", normalized, re.DOTALL)
    catalog = {tool.name: tool for tool in tools or []}
    if block is None or not catalog:
        raise InvalidToolCallResponseError("模型返回了异常工具调用格式，请使用标准 tool_calls")
    calls = []
    body = block.group(1)
    invocation = re.compile(r'<invoke name="([A-Za-z0-9_-]+)">\s*(.*?)</invoke>', re.DOTALL)
    parameter = re.compile(
        r'<parameter name="([^"<>]+)" string="(true|false)">(.*?)</parameter>',
        re.DOTALL,
    )
    try:
        while body.strip():
            match = invocation.match(body.lstrip())
            if match is None or match.group(1) not in catalog:
                raise ValueError("调用了未知工具或格式不完整")
            name, params = match.groups()
            arguments = {}
            while params.strip():
                arg = parameter.match(params.lstrip())
                if arg is None or arg.group(1) in arguments:
                    raise ValueError("参数格式错误或重复")
                key, is_string, value = arg.groups()
                arguments[key] = value if is_string == "true" else json.loads(value)
                params = params.lstrip()[arg.end() :]
            schema = catalog[name].parameters
            validator_for(schema)(schema, registry=Registry()).validate(arguments)
            calls.append(
                ToolCall(
                    id=f"call_{uuid4().hex}",
                    function=FunctionCall(
                        name=name,
                        arguments=json.dumps(arguments, ensure_ascii=False),
                    ),
                )
            )
            body = body.lstrip()[match.end() :]
        if not calls:
            raise ValueError("工具调用为空")
    except Exception as exc:
        raise InvalidToolCallResponseError(
            "模型工具调用无效，请使用已提供的工具和参数重试"
        ) from exc
    return response.model_copy(
        update={"content": None, "tool_calls": calls, "finish_reason": "tool_calls"}
    )
