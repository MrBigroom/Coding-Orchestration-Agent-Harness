"""Count Qwen-tokenizer tokens in the requests mock_server.py logged.

usage: python count.py logs/<harness> [logs/<harness> ...]

Per request: system prompt, tool schemas (as JSON), messages, total, number of tools and
number of `cache_control` markers. Uses the Qwen tokenizer bundled with the `dashscope`
package; DeepSeek/GLM tokenizers count roughly 10-15% differently.
"""

import glob
import json
import sys

from dashscope.tokenizers import get_tokenizer

TOK = get_tokenizer("qwen-turbo")


def ntok(x):
    if not x:
        return 0
    s = x if isinstance(x, str) else json.dumps(x, ensure_ascii=False)
    return len(TOK.encode(s))


def text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(c.get("text", "") if isinstance(c, dict) else str(c) for c in content)
    return json.dumps(content)


def analyse(body):
    # Anthropic: top-level `system`; OpenAI Responses: `instructions`/`input`.
    sys_t = ntok(text_of(body.get("system"))) + ntok(body.get("instructions"))
    msg_t = 0
    for m in body.get("messages") or body.get("input") or []:
        if not isinstance(m, dict):
            continue
        c = text_of(m.get("content", ""))
        if m.get("role") in ("system", "developer"):
            sys_t += ntok(c)
        else:
            msg_t += ntok(c)
    tools = body.get("tools") or []
    return {
        "system": sys_t,
        "tools": ntok(tools),
        "n_tools": len(tools),
        "messages": msg_t,
        "cache_control": json.dumps(body).count('"cache_control"'),
    }


if __name__ == "__main__":
    for d in sys.argv[1:]:
        print(f"## {d}")
        for f in sorted(glob.glob(f"{d}/*.json")):
            b = json.load(open(f))["body"]
            if not isinstance(b, dict) or "_raw" in b:
                continue
            a = analyse(b)
            total = a["system"] + a["tools"] + a["messages"]
            print(
                f"{f.split('/')[-1]:32s} sys={a['system']:6d} tools={a['tools']:6d} "
                f"({a['n_tools']:2d}) msgs={a['messages']:6d} total={total:6d} "
                f"cache_control={a['cache_control']}"
            )
