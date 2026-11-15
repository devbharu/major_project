"""LangChain tools the review agent can call. Each tool is bound to one ReviewContext (one PR)."""
import re
from collections import Counter

from langchain_core.tools import tool

from ..services.safety import safe_repo_path
from ..services.sandbox import quote

TOOL_LIMIT = 12
MAX_OUT = 4000


def _clip(text: str) -> str:
    return text if len(text) <= MAX_OUT else text[:MAX_OUT] + f"\n... [truncated {len(text) - MAX_OUT} chars]"


def build_tools(ctx) -> list:
    """`ctx` is a ReviewContext: file_texts, vectors, repo, sandbox (optional), gh."""
    calls: Counter = Counter()

    def guard(name: str) -> str | None:
        calls[name] += 1
        return f"Tool budget for {name} exhausted ({TOOL_LIMIT}). Produce your final JSON answer." if calls[name] > TOOL_LIMIT else None

    @tool
    def read_file(path: str, start_line: int = 1, end_line: int = 250) -> str:
        """Read lines start_line..end_line of a file at the PR head commit. Use to see code around a changed line."""
        if msg := guard("read_file"):
            return msg
        try:
            path = safe_repo_path(path)
        except ValueError as exc:
            return str(exc)
        text = ctx.file_texts.get(path)
        if text is None and ctx.sandbox:
            r = ctx.sandbox.repo_run(f"cat {quote(path)}")
            text = r.stdout if r.ok else None
        if text is None:
            try:
                text, _ = ctx.gh.file(ctx.source_repo, path, ctx.pr["head_sha"])
            except Exception as exc:
                return f"cannot read {path}: {exc}"
        lines = text.splitlines()[max(start_line - 1, 0):end_line]
        return _clip("\n".join(f"{i}: {l}" for i, l in enumerate(lines, start=max(start_line, 1))))

    @tool
    def search_code(query: str) -> str:
        """Semantic search over the indexed repository. Use natural language, e.g. 'where are sessions created'."""
        if msg := guard("search_code"):
            return msg
        hits = ctx.vectors.search_code(ctx.repo["id"], query, k=5)
        return _clip("\n\n".join(f"{h['path']}:{h['start_line']}-{h['end_line']}\n{h['text']}" for h in hits)) or "no results"

    @tool
    def grep(pattern: str) -> str:
        """Regex search across the repository. Returns path:line: text. Use to find callers/usages of a symbol."""
        if msg := guard("grep"):
            return msg
        if ctx.sandbox:
            r = ctx.sandbox.repo_run(f"git grep -n -E -I -- {quote(pattern)} | head -60")
            return _clip(r.output or "no matches")
        try:
            rx = re.compile(pattern)
        except re.error as exc:
            return f"bad regex: {exc}"
        out = [f"{p}:{i}: {line.strip()[:200]}" for p, t in ctx.file_texts.items()
               for i, line in enumerate(t.splitlines(), 1) if rx.search(line)]
        return _clip("\n".join(out[:60]) or "no matches (indexed files only)")

    tools = [read_file, search_code, grep]

    if ctx.sandbox:
        @tool
        def run_shell(command: str) -> str:
            """Run a shell command inside the isolated sandbox at the repository root (PR head checked out).
            Use for ast-grep/cat/ls, small python or node scripts that verify a suspected bug, or running a focused test."""
            if msg := guard("run_shell"):
                return msg
            r = ctx.sandbox.repo_run(command, timeout=60)
            return _clip(f"exit={r.exit_code}\n{r.output}")

        tools.append(run_shell)
    return tools


def run_agent(llm, tools, messages: list, max_steps: int):
    """Minimal ReAct loop with hard stop rules: model picks a tool -> run it -> feed result back, until
    it answers or `max_steps` is reached (then it is forced to answer)."""
    from langchain_core.messages import HumanMessage, ToolMessage

    by_name = {t.name: t for t in tools}
    try:
        bound = llm.bind_tools(tools)
    except Exception:
        bound = llm
    for _ in range(max_steps):
        try:
            ai = bound.invoke(messages)
        except Exception:
            if bound is llm:
                raise
            bound = llm  # gateway/model without tool calling: continue without tools
            ai = bound.invoke(messages)
        messages.append(ai)
        calls = getattr(ai, "tool_calls", None)
        if not calls:
            return ai
        for call in calls:
            try:
                result = by_name[call["name"]].invoke(call["args"])
            except Exception as exc:
                result = f"tool error: {exc}"
            messages.append(ToolMessage(content=str(result), tool_call_id=call["id"]))
    messages.append(HumanMessage(content="Step budget exhausted. Output your final JSON answer now, with no more tool calls."))
    return llm.invoke(messages)
