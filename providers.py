"""Lapisan model: GPT-OSS via Chat Completions, Claude via Anthropic Messages.

Keduanya lewat Amazon Bedrock Mantle (bedrock-mantle.<region>.api.aws),
diautentikasi dengan Bedrock API key sebagai Bearer token.
Setiap fungsi run_* adalah generator yang menghasilkan event untuk UI:
  {"type": "text", "delta": "..."}
  {"type": "tool_call", "id", "name", "args"}
  {"type": "tool_result", "id", "name", "result"}
"""
import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from anthropic import Anthropic
from openai import OpenAI

from tools import execute_tool, tool_specs

MAX_STEPS = 6      # batas putaran model -> tool -> model
MAX_TOKENS = 4096

MODELS = {
    "gpt-oss-120b": {
        "label": "GPT-OSS 120B",
        "provider": "openai",
        "id": os.getenv("MODEL_GPT_OSS_120B", "openai.gpt-oss-120b"),
    },
    "gpt-oss-20b": {
        "label": "GPT-OSS 20B",
        "provider": "openai",
        "id": os.getenv("MODEL_GPT_OSS_20B", "openai.gpt-oss-20b"),
    },
    "claude-sonnet-4-6": {
        "label": "Claude Sonnet 4.6",
        "provider": "anthropic",
        "id": os.getenv("MODEL_CLAUDE_SONNET", "anthropic.claude-sonnet-4-6"),
    },
    "claude-haiku-4-5": {
        "label": "Claude Haiku 4.5",
        "provider": "anthropic",
        "id": os.getenv("MODEL_CLAUDE_HAIKU", "anthropic.claude-haiku-4-5"),
    },
}


def system_prompt() -> str:
    now = datetime.now(ZoneInfo("Asia/Jakarta")).strftime("%A, %d %B %Y %H:%M WIB")
    return (
        "Kamu adalah asisten AI yang ringkas, jujur, dan membantu. "
        "Balas dalam bahasa yang dipakai pengguna. Gunakan format Markdown seperlunya. "
        "Pakai tool bila membantu dan jangan mengarang hasil tool. "
        "Untuk aksi yang mengubah sesuatu (mis. menjalankan workflow CI/CD), "
        "jelaskan dulu apa yang akan dilakukan, lalu laporkan hasilnya. "
        f"Waktu sekarang: {now}."
    )


def _base_url() -> str:
    return os.environ["BEDROCK_BASE_URL"].rstrip("/")


def _openai_client() -> OpenAI:
    return OpenAI(api_key=os.environ["BEDROCK_API_KEY"], base_url=_base_url(), timeout=55, max_retries=1)


def _anthropic_client() -> Anthropic:
    root = _base_url()
    if root.endswith("/v1"):
        root = root[:-3]
    # SDK menambahkan /v1/messages -> .../anthropic/v1/messages
    return Anthropic(
        auth_token=os.environ["BEDROCK_API_KEY"],
        base_url=f"{root}/anthropic",
        timeout=55,
        max_retries=1,
    )


def _parse_args(raw: str) -> dict:
    try:
        v = json.loads(raw or "{}")
        return v if isinstance(v, dict) else {}
    except json.JSONDecodeError:
        return {}


def _run_tool(call_id: str, name: str, args: dict, allow_actions: bool):
    """Generator kecil: yield event tool_call + tool_result, lalu return string hasil."""
    yield {"type": "tool_call", "id": call_id, "name": name, "args": args}
    result = execute_tool(name, args, allow_actions)
    yield {"type": "tool_result", "id": call_id, "name": name, "result": result[:3000]}
    return result


# --------------------------------------------------------------------------
# GPT-OSS (OpenAI-compatible Chat Completions)
# --------------------------------------------------------------------------
def run_openai(model_id: str, history: list[dict], allow_actions: bool):
    client = _openai_client()
    tools = [
        {"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["schema"]}}
        for t in tool_specs(allow_actions)
    ]
    msgs = [{"role": "system", "content": system_prompt()}] + history

    for _ in range(MAX_STEPS):
        kwargs = {"model": model_id, "messages": msgs, "stream": True}
        if tools:
            kwargs["tools"] = tools

        text, calls = "", {}
        for chunk in client.chat.completions.create(**kwargs):
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta.content:
                text += delta.content
                yield {"type": "text", "delta": delta.content}
            for tc in delta.tool_calls or []:
                slot = calls.setdefault(tc.index, {"id": "", "name": "", "args": ""})
                if tc.id:
                    slot["id"] = tc.id
                if tc.function:
                    if tc.function.name:
                        slot["name"] = tc.function.name
                    if tc.function.arguments:
                        slot["args"] += tc.function.arguments

        if not calls:
            return

        ordered = [calls[i] for i in sorted(calls)]
        msgs.append({
            "role": "assistant",
            "content": text or None,
            "tool_calls": [
                {"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": c["args"] or "{}"}}
                for c in ordered
            ],
        })
        for c in ordered:
            result = yield from _run_tool(c["id"], c["name"], _parse_args(c["args"]), allow_actions)
            msgs.append({"role": "tool", "tool_call_id": c["id"], "content": result})
        if text:
            yield {"type": "text", "delta": "\n\n"}

    yield {"type": "text", "delta": "\n\n_(Batas langkah tool tercapai.)_"}


# --------------------------------------------------------------------------
# Claude (Anthropic Messages API di Bedrock Mantle)
# --------------------------------------------------------------------------
def run_anthropic(model_id: str, history: list[dict], allow_actions: bool):
    client = _anthropic_client()
    tools = [
        {"name": t["name"], "description": t["description"], "input_schema": t["schema"]}
        for t in tool_specs(allow_actions)
    ]
    msgs = list(history)

    for _ in range(MAX_STEPS):
        kwargs = {"model": model_id, "max_tokens": MAX_TOKENS, "system": system_prompt(), "messages": msgs}
        if tools:
            kwargs["tools"] = tools

        with client.messages.stream(**kwargs) as stream:
            for piece in stream.text_stream:
                yield {"type": "text", "delta": piece}
            final = stream.get_final_message()

        if final.stop_reason != "tool_use":
            return

        assistant_blocks, results, had_text = [], [], False
        for b in final.content:
            if b.type == "text":
                had_text = had_text or bool(b.text)
                assistant_blocks.append({"type": "text", "text": b.text})
            elif b.type == "tool_use":
                assistant_blocks.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
        msgs.append({"role": "assistant", "content": assistant_blocks})

        for b in final.content:
            if b.type == "tool_use":
                result = yield from _run_tool(b.id, b.name, dict(b.input or {}), allow_actions)
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": result})
        msgs.append({"role": "user", "content": results})
        if had_text:
            yield {"type": "text", "delta": "\n\n"}

    yield {"type": "text", "delta": "\n\n_(Batas langkah tool tercapai.)_"}


def run_chat(model_key: str, history: list[dict], allow_actions: bool):
    cfg = MODELS[model_key]
    runner = run_anthropic if cfg["provider"] == "anthropic" else run_openai
    yield from runner(cfg["id"], history, allow_actions)
