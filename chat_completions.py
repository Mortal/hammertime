import asyncio
import json
import os
import random
import sys
from collections.abc import Mapping
from typing import Any

import aiohttp


def llm_single_edit_round(
    prompt: str, input_files: Mapping[str, bytes]
) -> tuple[str, dict[str, bytes]]:
    """
    Let an LLM run a single round to edit files according to the given prompt.
    the prompt should make it clear that the agent must do a single round of parallel edits,
    e.g. by ending the prompt with:
    "Do not read any other files in the project, "
    "and do not try to run any shell commands (python/bash/...). "
    "Apply all your edits in parallel using a single round of tool calls."
    """
    base = os.environ["COPILOT_PROVIDER_BASE_URL"]
    # Work on a copy so the caller's mapping is never mutated.
    files = {**input_files}
    messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
    # Seed the conversation with a fake "view" tool call/response for each file so
    # the model sees the file contents as if it had read them itself.
    for path, contents in files.items():
        # Build a unique, plausible-looking tool-call id to pair the request and response.
        tool_call_nonce = "".join(random.choice("0123456789abcdef") for _ in range(16))
        tool_call_id = f"chatcmpl-tool-{tool_call_nonce}"
        messages += [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": tool_call_id,
                        "type": "function",
                        "function": {
                            "name": "view",
                            "arguments": json.dumps({"path": path}),
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": contents.decode(errors="replace"),
            },
        ]
    raw_output, tool_calls = asyncio.run(chat_completions(base, messages))
    # Apply each "edit" tool call against the in-memory files. Invalid or unsafe
    # calls are reported to stderr and skipped rather than aborting the whole round.
    for tool_call in tool_calls.values():
        if tool_call["type"] != "function":
            raise Exception("unsupported tool call type")
        if tool_call["function"]["name"] != "edit":
            sys.stderr.write(
                f'server error: ignore tool call "{tool_call["function"]["name"]}"\n'
            )
            continue
        fun_args = json.loads(tool_call["function"]["arguments"])
        if fun_args["path"] not in files:
            sys.stderr.write(f"server error: ignore edit of {fun_args['path']}\n")
            continue
        old_str = fun_args["old_str"].encode()
        new_str = fun_args["new_str"].encode()
        if old_str not in files[fun_args["path"]]:
            sys.stderr.write("server error: old_str not found in file\n")
            continue
        if files[fun_args["path"]].count(old_str) != 1:
            sys.stderr.write("server error: old_str not unique in file\n")
            continue
        files[fun_args["path"]] = files[fun_args["path"]].replace(old_str, new_str)
    return raw_output, files


# ANSI styling for the pretty-printed sections.
DIM = "\x1b[2;3m"  # dim italic for reasoning
BOLD_CYAN = "\x1b[1;36m"  # section headers
RESET = "\x1b[0m"


def _emit(text, style=""):
    """Write text to stdout, optionally wrapped in an ANSI style, and flush."""
    sys.stdout.write((style + text + RESET) if style else text)
    sys.stdout.flush()


def _header(name):
    """Print a fixed-width section header line, e.g. '─── reasoning ──────'."""
    rule = "─" * max(3, 60 - len(name) - 4)
    _emit("\n─── {} {}   \n".format(name, rule), BOLD_CYAN)


class StreamPrinter:
    """Pretty-prints interleaved reasoning/content deltas as labelled sections."""

    def __init__(self):
        self.section = None

    def _enter(self, name):
        """Start the named section (printing a header) if it isn't current already."""
        if self.section != name:
            if self.section is not None:
                _emit("\n")
            _header(name)
            self.section = name

    def reasoning(self, text):
        self._enter("reasoning")
        _emit(text, DIM)

    def content(self, text):
        self._enter("content")
        _emit(text)

    def finish(self):
        if self.section is not None:
            _emit("\n")


def _delta_reasoning(delta):
    # Different servers expose chain-of-thought under different keys.
    return (
        delta.get("reasoning_content")
        or delta.get("reasoning")
        or delta.get("thinking")
    )


async def _print_non_streaming(body, printer) -> str:
    """Fallback if the server ignores "stream" and answers with plain JSON."""
    result = ""
    for choice in body.get("choices") or []:
        message = choice.get("message") or {}
        reasoning = _delta_reasoning(message)
        if reasoning:
            printer.reasoning(reasoning)
        if message.get("content"):
            printer.content(message["content"])
            result += message["content"]
    return result


VIEW_TOOL = {
    "type": "function",
    "function": {
        "name": "view",
        "description": "Read a text file at a given absolute path",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Full absolute path to file to view. File MUST exist.",
                },
            },
            "required": ["path"],
        },
    },
}

EDIT_TOOL = {
    "type": "function",
    "function": {
        "name": "edit",
        "description": "Tool for making string replacements in files. Replaces exactly one occurrence of old_str with new_str. Must match exactly one occurrence; if old_str is not unique the replacement is not performed. Path MUST be absolute.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Full absolute path to file to edit. File MUST exist to edit.",
                },
                "old_str": {
                    "type": "string",
                    "description": "The string in the file to replace. Leading and ending whitespaces from file content should be preserved!",
                },
                "new_str": {
                    "type": "string",
                    "description": "The new string to replace old_str with.",
                },
            },
            "required": ["path", "old_str", "new_str"],
        },
    },
}


async def chat_completions(
    base: str, messages: list[dict[str, Any]]
) -> tuple[str, dict[Any, dict[str, Any]]]:
    """
    Note, despite being an async function, this function is NOT suitable
    for running several prompts in parallel, as this function emits output
    on stdout and stderr while the model is running.
    """
    payload = {"stream": True, "messages": messages, "tools": [VIEW_TOOL, EDIT_TOOL]}
    url = f"{base}/chat/completions"
    # No overall timeout; allow long model "thinking" between received bytes.
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=600)
    printer = StreamPrinter()
    result = ""  # raw JSON of every streamed chunk, one per line (for debugging/replay)
    tools = {}  # tool-call id-less accumulator keyed by tool-call index
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload) as resp:
                if resp.status >= 400:
                    sys.stderr.write(await resp.text())
                resp.raise_for_status()
                if "text/event-stream" not in resp.headers.get("Content-Type", ""):
                    resp_json = await resp.json()
                    sys.stderr.write("server error: no streaming response\n")
                    _print_non_streaming(resp_json, printer)
                    return json.dumps(resp_json) + "\n", tools

                # Parse the SSE stream: "data: {chunk}" lines terminated by "data: [DONE]".
                async for raw in resp.content:
                    line = raw.decode("utf-8", "replace").rstrip("\r\n")
                    if not line or line.startswith(":") or not line.startswith("data:"):
                        continue
                    data = line[len("data:") :].lstrip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        sys.stderr.write("server error: not valid JSON\n")
                        continue
                    result += json.dumps(chunk) + "\n"
                    if chunk.get("error"):
                        sys.stderr.write("server error: {}\n".format(chunk["error"]))
                        continue
                    choices = chunk.get("choices") or []
                    if not choices:
                        sys.stderr.write("server error: empty 'choices'\n")
                        continue
                    delta = choices[0].get("delta") or {}
                    reasoning = _delta_reasoning(delta)
                    # Accumulate streamed tool-call fragments, keyed by their index.
                    # The stream sends pieces incrementally: an id/type/name once, then
                    # the `arguments` string arriving in many small chunks to be appended.
                    for tool_call in delta.get("tool_calls") or []:
                        upd = tools.setdefault(
                            tool_call["index"], {"function": {"arguments": ""}}
                        )
                        if "id" in tool_call:
                            upd["id"] = tool_call["id"]
                        if "type" in tool_call:
                            upd["type"] = tool_call["type"]
                        if "function" in tool_call:
                            if "name" in tool_call["function"]:
                                upd["function"]["name"] = tool_call["function"]["name"]
                            if "arguments" in tool_call["function"]:
                                upd["function"]["arguments"] += tool_call["function"][
                                    "arguments"
                                ]
                    if reasoning:
                        printer.reasoning(reasoning)
                    if delta.get("content"):
                        printer.content(delta["content"])
    except aiohttp.ClientError as exc:
        raise Exception("request to {} failed: {}".format(url, exc))
    finally:
        printer.finish()
    return result, tools
