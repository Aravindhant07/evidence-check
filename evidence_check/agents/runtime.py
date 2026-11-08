"""A small tool-using agent loop over MCP, for any chat model (a local open model by default).

The model answers each turn with one JSON object: {"tool": ..., "arguments": {...}} or {"answer": ...}.
Small open models follow this format reliably; the same loop passed 10/10 tasks in legacy-bridge with Qwen2.5-3B.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

LLM_ID, LLM_REVISION = "Qwen/Qwen2.5-3B-Instruct", "aa8e72537993ba99e69dfaafa59ed015b17504d1"


class ChatModel(Protocol):
    name: str

    def chat(self, messages: list[dict[str, str]]) -> str: ...


class LocalChatModel:
    """An open model from Hugging Face, run locally on CPU, pinned to a commit."""

    def __init__(self, model_id: str = LLM_ID, revision: str = LLM_REVISION, max_new_tokens: int = 400):
        self.name = f"local:{model_id}@{revision[:8]}"
        self.model_id, self.revision, self.max_new_tokens = model_id, revision, max_new_tokens
        self._tok = self._model = None

    def chat(self, messages: list[dict[str, str]]) -> str:
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        if self._model is None:
            self._tok = AutoTokenizer.from_pretrained(self.model_id, revision=self.revision)
            self._model = AutoModelForCausalLM.from_pretrained(self.model_id, revision=self.revision, torch_dtype="auto",
                                                               use_safetensors=True, trust_remote_code=False).eval()
        prompt = self._tok.apply_chat_template(messages, add_generation_prompt=True, tokenize=False)
        inputs = self._tok(prompt, return_tensors="pt")
        with torch.inference_mode():
            out = self._model.generate(**inputs, max_new_tokens=self.max_new_tokens, do_sample=False,
                                       pad_token_id=self._tok.eos_token_id)
        return self._tok.decode(out[0][inputs["input_ids"].shape[1]:], skip_special_tokens=True)


def parse_action(text: str) -> dict[str, Any] | None:
    """The first JSON object in the reply (models sometimes wrap it in prose or code fences)."""
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    start = text.find("{")
    while start >= 0:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                esc = (ch == "\\") and not esc
                if ch == '"' and not esc:
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            depth += {"{": 1, "}": -1}.get(ch, 0)
            if depth == 0:
                try:
                    obj = json.loads(text[start:i + 1])
                    if isinstance(obj, dict):
                        return obj
                except json.JSONDecodeError:
                    pass
                break
        start = text.find("{", start + 1)
    return None


def describe_tools(tools) -> str:
    lines = []
    for t in tools:
        props = t.input_schema.get("properties", {})
        args = ", ".join(f"{k}: {v.get('type', 'any')}" for k, v in props.items())
        lines.append(f"- {t.name}({args}): {(t.description or '').strip().splitlines()[0]}")
    return "\n".join(lines)


@dataclass
class Trace:
    task: str
    answer: Any = None
    calls: list[dict[str, Any]] = field(default_factory=list)
    turns: int = 0
    seconds: float = 0.0
    error: str | None = None


async def run_agent(server, model: ChatModel, system: str, task: str, max_turns: int = 8,
                    allowed: set[str] | None = None) -> Trace:
    """Run the loop against an MCP server. `allowed` narrows the tools the model may call (default: read-only ones)."""
    from mcp import Client

    trace = Trace(task)
    t0 = time.perf_counter()
    async with Client(server) as c:
        tools = [t for t in (await c.list_tools()).tools
                 if (allowed is None and t.annotations and t.annotations.read_only_hint) or (allowed and t.name in allowed)]
        names = {t.name for t in tools}
        messages = [{"role": "system", "content": system.replace("{tools}", describe_tools(tools))},
                    {"role": "user", "content": task}]
        for _ in range(max_turns):
            trace.turns += 1
            reply = model.chat(messages)
            messages.append({"role": "assistant", "content": reply})
            action = parse_action(reply)
            if action is None:
                messages.append({"role": "user", "content": "Reply with one JSON object only, as instructed."})
                continue
            if "answer" in action:
                trace.answer = action["answer"]
                break
            name, args = action.get("tool"), action.get("arguments") or {}
            if name not in names:
                messages.append({"role": "user", "content": f"Tool '{name}' is not available. Available: {', '.join(sorted(names))}."})
                continue
            r = await c.call_tool(name, args)
            result = r.content[0].text if r.is_error else json.dumps(r.structured_content, default=str)
            trace.calls.append({"tool": name, "arguments": args, "error": r.is_error, "result": result[:800]})
            messages.append({"role": "user", "content": f"Tool result ({'ERROR' if r.is_error else 'ok'}): {result[:3500]}"})
        else:
            trace.error = "no answer within the turn limit"
    trace.seconds = round(time.perf_counter() - t0, 1)
    return trace
