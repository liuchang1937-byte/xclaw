"""可用于验证 XClaw MCP 接入的本地工具服务。"""

import argparse

from mcp.server.fastmcp import FastMCP

parser = argparse.ArgumentParser()
parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
parser.add_argument("--port", type=int, default=8001)


def build_server(port=8001):
    server = FastMCP("XClaw Demo", host="127.0.0.1", port=port, log_level="ERROR")

    @server.tool()
    def add(a: int, b: int) -> int:
        """计算两个整数之和。"""
        return a + b

    @server.tool()
    def echo(message: str, tags: list[str] | None = None) -> dict:
        """返回文本和标签，用于检查字符串与数组参数的传递。"""
        return {"message": message, "tags": tags or []}

    return server


if __name__ == "__main__":
    args = parser.parse_args()
    build_server(args.port).run(transport=args.transport)
