#!/usr/bin/env python3
"""or_draft.py - ask an OpenRouter model for a code draft and write it STRAIGHT TO DISK.

The point: the draft never passes through the calling agent's output tokens. The agent only
reads/reviews the file afterwards.

Usage:
  python or_draft.py --out bridge_part1.py --prompt-file spec1.md [--context docs/x.md ...]
                     [--model deepseek/deepseek-v3.2] [--max-tokens 16000] [--extract python|none]
Exit codes: 0 ok, 2 truncated (finish_reason=length), 3 API/transport error, 4 empty result.
Key: OPENROUTER_API_KEY from the environment, else ~/.config/vibes-hacker/env (KEY=value lines).
"""
import argparse, json, os, re, sys, time, urllib.request, urllib.error
from pathlib import Path

API = "https://openrouter.ai/api/v1/chat/completions"


def load_key():
    k = os.environ.get("OPENROUTER_API_KEY")
    if k:
        return k
    p = Path.home() / ".config" / "vibes-hacker" / "env"
    if p.exists():
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[7:]
            name, _, val = line.partition("=")
            if name.strip() == "OPENROUTER_API_KEY":
                return val.strip().strip('"').strip("'")
    sys.exit("no OPENROUTER_API_KEY in env or ~/.config/vibes-hacker/env")


def read(p):
    return Path(p).read_text(encoding="utf-8", errors="replace")


def extract_code(text, lang):
    if lang == "none":
        return text
    fences = re.findall(r"```(?:%s|py)?[ \t]*\r?\n(.*?)```" % re.escape(lang), text, flags=re.S | re.I)
    if fences:
        return max(fences, key=len)
    fences = re.findall(r"```[a-zA-Z0-9_-]*[ \t]*\r?\n(.*?)```", text, flags=re.S)
    if fences:
        return max(fences, key=len)
    return text


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--prompt-file")
    ap.add_argument("--prompt")
    ap.add_argument("--system-file")
    ap.add_argument("--context", nargs="*", default=[], help="files pasted into the prompt as context")
    ap.add_argument("--model", default="deepseek/deepseek-v3.2")
    ap.add_argument("--max-tokens", type=int, default=16000)
    ap.add_argument("--temperature", type=float, default=0.2)
    ap.add_argument("--extract", default="python", help="python|none")
    ap.add_argument("--append", action="store_true")
    ap.add_argument("--timeout", type=int, default=900)
    a = ap.parse_args()
    if not (a.prompt or a.prompt_file):
        ap.error("--prompt or --prompt-file required")

    prompt = a.prompt or read(a.prompt_file)
    ctx = []
    for c in a.context:
        ctx.append("\n\n===== CONTEXT FILE: %s =====\n%s" % (c, read(c)))
    system = read(a.system_file) if a.system_file else (
        "You are a meticulous senior Python engineer. Output ONLY one fenced ```python code block "
        "containing complete, runnable code for exactly the section requested. No prose outside the "
        "block. Python 3.10+, standard library only. Follow the spec literally.")
    body = {
        "model": a.model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": prompt + "".join(ctx)}],
        "max_tokens": a.max_tokens,
        "temperature": a.temperature,
        "stream": False,
        "usage": {"include": True},
    }
    req = urllib.request.Request(API, data=json.dumps(body).encode("utf-8"), method="POST", headers={
        "Authorization": "Bearer " + load_key(),
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/rolo/claude-bridge",
        "X-Title": "claude-bridge drafting",
    })
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=a.timeout) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        print("HTTP %s: %s" % (e.code, e.read().decode("utf-8", "replace")[:800]), file=sys.stderr)
        sys.exit(3)
    except Exception as e:  # noqa
        print("transport error: %r" % (e,), file=sys.stderr)
        sys.exit(3)
    if "error" in data:
        print("API error: %s" % json.dumps(data["error"])[:800], file=sys.stderr)
        sys.exit(3)
    choice = data["choices"][0]
    content = choice["message"].get("content") or ""
    finish = choice.get("finish_reason")
    code = extract_code(content, a.extract)
    if not code.strip():
        print("empty result; raw content head: %r" % content[:300], file=sys.stderr)
        sys.exit(4)
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    mode = "a" if a.append else "w"
    with open(out, mode, encoding="utf-8", newline="\n") as f:
        f.write(code.rstrip("\n") + "\n")
    usage = data.get("usage", {})
    summary = {
        "out": str(out), "lines": code.count("\n") + 1, "finish_reason": finish,
        "model": data.get("model", a.model), "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"), "cost_usd": usage.get("cost"),
        "seconds": round(time.time() - t0, 1),
    }
    print(json.dumps(summary))
    if finish == "length":
        print("WARNING: truncated (finish_reason=length) - raise --max-tokens or split the section", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
