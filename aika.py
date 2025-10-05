#!/usr/bin/env python3
import os
import sys
import json
import time
import shutil
import textwrap
import subprocess
from typing import Any, Dict, List, Tuple

import requests
from groq import Groq

# Optional deps for research tools
try:
    from duckduckgo_search import DDGS  # pip install duckduckgo-search
    _HAS_DDG = True
except Exception:
    _HAS_DDG = False

try:
    from bs4 import BeautifulSoup  # pip install beautifulsoup4
    _HAS_BS4 = True
except Exception:
    _HAS_BS4 = False

# Clipboard support
try:
    import pyperclip  # pip install pyperclip
    _HAS_PYPERCLIP = True
except Exception:
    _HAS_PYPERCLIP = False

# Pretty terminal UI
try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.history import FileHistory
    from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
    from prompt_toolkit.styles import Style
    _HAS_PTK = True
except Exception:
    _HAS_PTK = False

try:
    from rich.console import Console, Group
    from rich.panel import Panel
    from rich.syntax import Syntax
    from rich.text import Text
    _HAS_RICH = True
    console = Console()
except Exception:
    _HAS_RICH = False

AIKA_PRETTY = os.getenv("AIKA_PRETTY", "1") != "0"
AIKA_ALWAYS_SHOW_SOURCES = os.getenv("AIKA_ALWAYS_SHOW_SOURCES", "1") != "0"
AIKA_SOURCES_LIMIT = int(os.getenv("AIKA_SOURCES_LIMIT", "6"))

# Tool budgets per user request (env-configurable)
WEB_SEARCH_LIMIT = int(os.getenv("AIKA_WEB_SEARCH_LIMIT", "2"))
FETCH_URL_LIMIT = int(os.getenv("AIKA_FETCH_URL_LIMIT", "3"))

# Simple in-memory caches to reduce duplicate calls
_SEARCH_CACHE: Dict[Tuple[str, int], str] = {}
_FETCH_CACHE: Dict[Tuple[str, int], str] = {}

# --- Configuration & Client Initialization ---
def get_groq_client() -> Groq:
    api_key = "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"
    try:
        return Groq(api_key=api_key)
    except Exception as e:
        print(f"Error initializing Groq client: {e}")
        sys.exit(1)

client = get_groq_client()
MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")

# --- ASCII Art Title (dark/green vibe) ---
TITLE = r"""
      _    ___  _  __     
     / \  / _ \| |/ /___  
    / _ \| | | | ' // _ \ 
   / ___ \ |_| | . \  __/ 
  /_/   \_\___/|_|\_\___|  AIKA
"""

def clear_screen():
    os.system('cls' if os.name == 'nt' else 'clear')

# --- UI helpers (tgpt-ish) ---
def _term_width(default=100):
    try:
        return shutil.get_terminal_size((default, 20)).columns
    except Exception:
        return default

PTK_STYLE = None
SESSION = None
if _HAS_PTK:
    PTK_STYLE = Style.from_dict({
        "prompt": "ansigreen bold",
        "": "ansibrightwhite",
    })
    history_file = os.path.expanduser("~/.aika_history")
    SESSION = PromptSession(
        history=FileHistory(history_file),
        auto_suggest=AutoSuggestFromHistory(),
        style=PTK_STYLE
    )

def prompt_input() -> str:
    if _HAS_PTK and AIKA_PRETTY:
        try:
            return SESSION.prompt([("class:prompt", "> ")])
        except KeyboardInterrupt:
            return ""
    else:
        return input("\n> ").strip()

def print_header():
    if _HAS_RICH and AIKA_PRETTY:
        console.print(Text(TITLE, style="green"))
        console.print(Text("Terminal AI assistant.", style="bright_black"))
    else:
        print(TITLE)
        print("Terminal AI assistant.")

def print_tool_status(name: str):
    line = f"⚙️  Executing tool: {name} ..."
    if _HAS_RICH and AIKA_PRETTY:
        console.print(f"[green]{line}[/green]")
    else:
        print(line)

def _split_text_into_blocks(text: str) -> List[Tuple[str, str, str]]:
    """
    Split plain text into blocks: ("text", content, ""), ("code", content, language)
    using delimiters:
      BEGIN CODE (language)
      ...lines...
      END CODE
    """
    blocks: List[Tuple[str, str, str]] = []
    lines = (text or "").splitlines()
    buf: List[str] = []
    in_code = False
    code_lang = "text"
    code_buf: List[str] = []

    def flush_text():
        nonlocal buf
        if buf:
            blocks.append(("text", "\n".join(buf), ""))
            buf = []

    for line in lines:
        stripped = line.strip()
        if not in_code and stripped.startswith("BEGIN CODE"):
            flush_text()
            # Parse language inside parentheses
            lang = "text"
            if "(" in stripped and ")" in stripped:
                try:
                    lang = stripped.split("(", 1)[1].split(")", 1)[0].strip() or "text"
                except Exception:
                    lang = "text"
            in_code = True
            code_lang = lang
            code_buf = []
            continue

        if in_code and stripped == "END CODE":
            blocks.append(("code", "\n".join(code_buf), code_lang))
            in_code = False
            code_lang = "text"
            code_buf = []
            continue

        if in_code:
            code_buf.append(line)
        else:
            buf.append(line)

    if in_code:
        # Unterminated code fence; treat collected as code anyway
        blocks.append(("code", "\n".join(code_buf), code_lang))
    else:
        flush_text()

    return blocks

def _render_blocks(blocks: List[Tuple[str, str, str]]):
    if not _HAS_RICH or not AIKA_PRETTY:
        # Plain fallback
        for kind, content, lang in blocks:
            if kind == "code":
                print("BEGIN CODE (" + lang + ")")
                print(content)
                print("END CODE")
            else:
                width = max(60, _term_width())
                print("\n".join(textwrap.fill(l, width=width) for l in content.splitlines()))
        return

    renderables = []
    for kind, content, lang in blocks:
        if kind == "code":
            try:
                syntax = Syntax(content or "", language=lang or "text", theme="monokai", line_numbers=False)
            except Exception:
                syntax = Syntax(content or "", language="text", theme="monokai", line_numbers=False)
            renderables.append(syntax)
        else:
            renderables.append(Text(content or "", style="white"))

    group = Group(*renderables)
    panel = Panel(group, title="AIKA", border_style="green", padding=(1, 2))
    console.print(panel)

def print_assistant(text: str) -> List[str]:
    """
    Print assistant text in a tgpt-like panel with green accents.
    Returns list of code blocks (strings) found in the output (for copy command).
    """
    blocks = _split_text_into_blocks(text or "")
    if _HAS_RICH and AIKA_PRETTY:
        _render_blocks(blocks)
    else:
        # Simple fallback with wrapping
        for kind, content, lang in blocks:
            if kind == "code":
                print(f"BEGIN CODE ({lang})")
                print(content)
                print("END CODE")
            else:
                width = max(60, _term_width())
                print("\n".join(textwrap.fill(l, width=width) for l in content.splitlines()))
    # Return only code contents (in order)
    return [content for kind, content, _ in blocks if kind == "code"]

# --- Tools: Python functions the model can call ---
def create_file(filename: str, content: str) -> str:
    try:
        if os.path.isdir(filename):
            return f"Error: '{filename}' is a directory."
        with open(filename, 'w', encoding='utf-8') as f:
            f.write(content)
        return f"Successfully created the file '{filename}'."
    except Exception as e:
        return f"Error creating file: {e}"

def web_search(query: str, max_results: int = 5) -> str:
    key = (query, int(max_results))
    if key in _SEARCH_CACHE:
        return _SEARCH_CACHE[key]

    results: List[Dict[str, str]] = []
    source = None

    if _HAS_DDG:
        try:
            source = "duckduckgo_search"
            with DDGS() as ddgs:
                for item in ddgs.text(query, max_results=max_results):
                    results.append({
                        "title": item.get("title") or "",
                        "url": item.get("href") or "",
                        "snippet": item.get("body") or "",
                    })
        except Exception as e:
            results.append({"title": "", "url": "", "snippet": f"duckduckgo_search error: {e}"})

    if not results:
        source = "duckduckgo_instant_answer_api"
        try:
            resp = requests.get(
                "https://api.duckduckgo.com/",
                params={"q": query, "format": "json", "no_redirect": "1", "no_html": "1"},
                timeout=15,
            )
            data = resp.json()
            if data.get("AbstractURL") or data.get("AbstractText"):
                results.append({
                    "title": data.get("Heading") or "",
                    "url": data.get("AbstractURL") or "",
                    "snippet": data.get("AbstractText") or "",
                })
            related = data.get("RelatedTopics") or []
            for rt in related:
                if isinstance(rt, dict) and rt.get("FirstURL") and rt.get("Text"):
                    results.append({
                        "title": rt.get("Text")[:120],
                        "url": rt.get("FirstURL"),
                        "snippet": rt.get("Text"),
                    })
                if isinstance(rt, dict) and "Topics" in rt:
                    for sub in rt["Topics"]:
                        if sub.get("FirstURL") and sub.get("Text"):
                            results.append({
                                "title": sub.get("Text")[:120],
                                "url": sub.get("FirstURL"),
                                "snippet": sub.get("Text"),
                            })
            results = results[:max_results]
        except Exception as e:
            results.append({"title": "", "url": "", "snippet": f"DuckDuckGo fallback error: {e}"})

    payload = {"query": query, "source": source, "results": results}
    payload_str = json.dumps(payload, ensure_ascii=False)
    _SEARCH_CACHE[key] = payload_str
    return payload_str

def fetch_url(url: str, max_chars: int = 4000) -> str:
    key = (url, int(max_chars))
    if key in _FETCH_CACHE:
        return _FETCH_CACHE[key]

    headers = {"User-Agent": "Mozilla/5.0 (compatible; AIKA/1.0; +https://example.com/bot)"}
    out: Dict[str, Any] = {"url": url, "status": "ok", "content": ""}

    try:
        resp = requests.get(url, timeout=20, headers=headers)
        resp.raise_for_status()
        text = resp.text or ""

        if _HAS_BS4:
            soup = BeautifulSoup(text, "html.parser")
            for tag in soup(["script", "style", "noscript"]):
                tag.extract()
            content = soup.get_text(separator="\n")
        else:
            content = text

        content = "\n".join(line.strip() for line in content.splitlines() if line.strip())
        if len(content) > max_chars:
            content = content[:max_chars] + "\n...[truncated]"

        out["content"] = content
        payload = json.dumps(out, ensure_ascii=False)
        _FETCH_CACHE[key] = payload
        return payload
    except Exception as e:
        out["status"] = "error"
        out["error"] = str(e)
        payload = json.dumps(out, ensure_ascii=False)
        _FETCH_CACHE[key] = payload
        return payload

# --- Tool schemas for the AI ---
tools = [
    {
        "type": "function",
        "function": {
            "name": "create_file",
            "description": "Create a new file and write content to it. Use when the user explicitly asks to save or create a file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "description": "The filename to create, e.g., 'main.py', 'notes.txt'."},
                    "content": {"type": "string", "description": "The complete content to write into the file."}
                },
                "required": ["filename", "content"]
            }
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web for up-to-date information. Returns JSON with a list of results [{title, url, snippet}]. Use this when the user asks to research or when you need current info.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query string"},
                    "max_results": {"type": "integer", "description": "Max number of results (1-10)", "default": 5}
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_url",
            "description": "Fetch a web page and extract readable text content for analysis and summarization.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "HTTP/HTTPS URL to fetch"},
                    "max_chars": {"type": "integer", "description": "Max characters to return", "default": 4000}
                },
                "required": ["url"]
            }
        }
    }
]

# Map function names to callables
AVAILABLE_FUNCTIONS = {
    "create_file": create_file,
    "web_search": web_search,
    "fetch_url": fetch_url,
}

def to_assistant_message_dict(message_obj) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "role": getattr(message_obj, "role", "assistant"),
        "content": getattr(message_obj, "content", None),
    }
    tool_calls = getattr(message_obj, "tool_calls", None)
    if tool_calls:
        out["tool_calls"] = []
        for tc in tool_calls:
            out["tool_calls"].append({
                "id": tc.id,
                "type": tc.type,
                "function": {
                    "name": tc.function.name,
                    "arguments": tc.function.arguments,
                }
            })
    return out

def _add_source(url: str, lst: List[str], seen: set):
    if not url:
        return
    if not (url.startswith("http://") or url.startswith("https://")):
        return
    if url in seen:
        return
    seen.add(url)
    lst.append(url)

def run_tool_call(
    tool_call,
    counts: Dict[str, int],
    budgets: Dict[str, int],
    sources_list: List[str],
    sources_seen: set
) -> Dict[str, Any]:
    function_name = tool_call.function.name
    raw_args = tool_call.function.arguments or "{}"
    try:
        function_args = json.loads(raw_args)
    except Exception:
        function_args = {}

    limit = budgets.get(function_name)
    if limit is not None:
        current = counts.get(function_name, 0)
        if current >= limit:
            result = f"Budget exceeded for {function_name} (limit {limit}). Provide the best answer with existing info."
            return {
                "tool_call_id": tool_call.id,
                "role": "tool",
                "name": function_name,
                "content": result,
            }

    fn = AVAILABLE_FUNCTIONS.get(function_name)
    if not fn:
        result = f"Error: unknown tool '{function_name}'."
    else:
        try:
            result = fn(**function_args)
            counts[function_name] = counts.get(function_name, 0) + 1
        except TypeError as e:
            result = f"Error calling '{function_name}': {e}"
        except Exception as e:
            result = f"Error in '{function_name}': {e}"

    try:
        if function_name == "web_search":
            data = json.loads(result)
            for item in data.get("results", []):
                _add_source(item.get("url"), sources_list, sources_seen)
                if len(sources_list) >= AIKA_SOURCES_LIMIT:
                    break
        elif function_name == "fetch_url":
            data = json.loads(result)
            _add_source(data.get("url"), sources_list, sources_seen)
    except Exception:
        pass

    return {
        "tool_call_id": tool_call.id,
        "role": "tool",
        "name": function_name,
        "content": result,
    }

def append_sources_to_text(text: str, sources: List[str]) -> str:
    if not sources:
        return text or ""
    out = (text or "").rstrip()
    out += "\n\nSources:\n"
    for url in sources[:AIKA_SOURCES_LIMIT]:
        out += f"- {url}\n"
    return out

def print_final_answer(messages: List[Dict[str, Any]], sources: List[str]) -> Tuple[str, List[str]]:
    """
    Produce the final answer with tools disabled and tgpt-like rendering.
    Returns (answer_text, code_blocks_list).
    """
    safe_messages = messages + [{
        "role": "system",
        "content": (
            "Final answer only. Do not call any tools in this turn.\n"
            "Output must be plain text. Do not use Markdown, backticks, or any markup.\n"
            "Use code delimiters 'BEGIN CODE (language)' and 'END CODE' for code.\n"
            "If web research was used earlier, append a 'Sources:' section with plain URLs.\n"
            "Keep it concise unless the user explicitly asked for more detail."
        )
    }]

    try:
        resp = client.chat.completions.create(
            model=MODEL,
            messages=safe_messages,
            temperature=0.3,
        )
        text = resp.choices[0].message.content or ""
        if AIKA_ALWAYS_SHOW_SOURCES and sources:
            text = append_sources_to_text(text, sources)
        code_blocks = print_assistant(text)
        return text, code_blocks
    except Exception as e:
        text = f"Could not render final answer: {e}"
        code_blocks = print_assistant(text)
        return text, code_blocks

def copy_to_clipboard(text: str) -> str:
    if not text:
        return "Nothing to copy."
    if _HAS_PYPERCLIP:
        try:
            pyperclip.copy(text)
            return "Copied to clipboard."
        except Exception as e:
            pass
    # Try common CLI tools
    try:
        if shutil.which("wl-copy"):
            p = subprocess.Popen(["wl-copy"], stdin=subprocess.PIPE)
            p.communicate(input=text.encode("utf-8"))
            return "Copied to clipboard (wl-copy)."
        if shutil.which("xclip"):
            p = subprocess.Popen(["xclip", "-selection", "clipboard"], stdin=subprocess.PIPE)
            p.communicate(input=text.encode("utf-8"))
            return "Copied to clipboard (xclip)."
        if shutil.which("pbcopy"):
            p = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE)
            p.communicate(input=text.encode("utf-8"))
            return "Copied to clipboard (pbcopy)."
    except Exception:
        pass
    return "Could not copy (no clipboard tool found)."

def save_text_to_file(text: str, filename: str = "") -> str:
    if not text or not text.strip():
        return "Nothing to save yet. Ask a question first."
    if not filename:
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        filename = f"aika_{timestamp}.txt"
    try:
        msg = create_file(filename, text)
        return msg
    except Exception as e:
        return f"Error saving file: {e}"

def help_text() -> str:
    return (
        "Commands:\n"
        "- s or :w                 Save last answer (prompt for filename)\n"
        "- save <filename>         Save last answer to a specific file\n"
        "- copy                    Copy last code block to clipboard\n"
        "- copy <n>                Copy nth code block from last answer (1-based)\n"
        "- sources on/off/status   Toggle auto-append Sources section\n"
        "- clear                   Clear the screen (history kept)\n"
        "- help                    Show this help\n"
        "- quit / exit / bye       Exit\n"
    )

def main():
    clear_screen()
    print_header()
    print("Try: Research Python 3.13 changes and save a summary")

    conversation_history: List[Dict[str, Any]] = [
        {
            "role": "system",
            "content": (
                "You are AIKA, a concise, helpful assistant with access to tools: create_file, web_search, fetch_url.\n"
                "Output format:\n"
                "- Plain text only. Do not use Markdown, backticks, or any markup.\n"
                "- For lists, use simple hyphens or numbered items (1), 2), 3)).\n"
                "- For code, use delimiters:\n"
                "  BEGIN CODE (language)\n"
                "  ...code...\n"
                "  END CODE\n"
                "Tool use policy:\n"
                f"- Use web_search when needed; at most {WEB_SEARCH_LIMIT} calls per request.\n"
                f"- After searching, use fetch_url on up to {FETCH_URL_LIMIT} promising results.\n"
                "- If a tool fails, briefly state the failure and proceed.\n"
                "- When you used web research, append a Sources section with plain URLs (one per line).\n"
                "File creation policy:\n"
                "- Call create_file only if the user explicitly asks to save or create a file.\n"
                "Style:\n"
                "- Be direct, friendly, and concise by default."
            )
        }
    ]

    last_answer_text = ""
    last_code_blocks: List[str] = []
    last_answer_sources: List[str] = []

    while True:
        try:
            user_prompt = prompt_input().strip()
            if not user_prompt:
                continue

            lower = user_prompt.lower()
            if lower in ["exit", "quit", "bye"]:
                if _HAS_RICH and AIKA_PRETTY:
                    console.print(Panel("Goodbye!", border_style="green"))
                else:
                    print("Goodbye!")
                break

            if lower == "clear":
                clear_screen()
                print_header()
                continue

            if lower in ["help", "?"]:
                print_assistant(help_text())
                continue

            # Saving
            if lower in ["s", ":w"]:
                default_name = f"aika_{time.strftime('%Y%m%d_%H%M%S')}.txt"
                try:
                    if _HAS_PTK and AIKA_PRETTY:
                        fname = SESSION.prompt([("class:prompt", f"filename [{default_name}]: ")])
                    else:
                        fname = input(f"filename [{default_name}]: ").strip()
                except KeyboardInterrupt:
                    print_assistant("Save canceled.")
                    continue
                if not fname:
                    fname = default_name
                print_assistant(save_text_to_file(last_answer_text, fname))
                continue

            if lower.startswith("save "):
                parts = user_prompt.split(maxsplit=1)
                if len(parts) == 2 and parts[1].strip():
                    fname = parts[1].strip()
                    print_assistant(save_text_to_file(last_answer_text, fname))
                else:
                    print_assistant("Usage: save <filename>")
                continue

            # Copy code blocks
            if lower == "copy" or lower.startswith("copy "):
                idx = None
                parts = user_prompt.split()
                if len(parts) == 2 and parts[1].isdigit():
                    idx = int(parts[1]) - 1
                if not last_code_blocks:
                    print_assistant("No code blocks in last answer.")
                    continue
                if idx is None:
                    code = last_code_blocks[-1]
                else:
                    if not (0 <= idx < len(last_code_blocks)):
                        print_assistant(f"Index out of range. There are {len(last_code_blocks)} code blocks.")
                        continue
                    code = last_code_blocks[idx]
                print_assistant(copy_to_clipboard(code))
                continue

            # Sources toggles
            global AIKA_ALWAYS_SHOW_SOURCES
            if lower in ["sources on", "sources off", "sources status"]:
                if lower == "sources on":
                    AIKA_ALWAYS_SHOW_SOURCES = True
                    print_assistant(f"Sources: ON (limit {AIKA_SOURCES_LIMIT})")
                elif lower == "sources off":
                    AIKA_ALWAYS_SHOW_SOURCES = False
                    print_assistant("Sources: OFF")
                else:
                    status = "ON" if AIKA_ALWAYS_SHOW_SOURCES else "OFF"
                    print_assistant(f"Sources: {status} (limit {AIKA_SOURCES_LIMIT})")
                continue

            # Regular user message
            conversation_history.append({"role": "user", "content": user_prompt})

            # Per-request tool counts and sources
            tool_counts: Dict[str, int] = {"web_search": 0, "fetch_url": 0, "create_file": 0}
            collected_sources: List[str] = []
            collected_seen: set = set()

            step_count = 0
            while True:
                response = client.chat.completions.create(
                    model=MODEL,
                    messages=conversation_history,
                    tools=tools,
                    tool_choice="auto",
                    temperature=0.3,
                )
                message = response.choices[0].message
                assistant_message = to_assistant_message_dict(message)
                conversation_history.append(assistant_message)

                tool_calls = assistant_message.get("tool_calls", [])
                if tool_calls:
                    for tc in message.tool_calls:
                        print_tool_status(tc.function.name)
                        tool_msg = run_tool_call(
                            tc,
                            counts=tool_counts,
                            budgets={"web_search": WEB_SEARCH_LIMIT, "fetch_url": FETCH_URL_LIMIT},
                            sources_list=collected_sources,
                            sources_seen=collected_seen
                        )
                        conversation_history.append(tool_msg)

                    step_count += 1
                    if step_count > 6:
                        conversation_history.append({
                            "role": "system",
                            "content": "Tool loop exceeded 6 steps; provide your best answer now with available info. Do not call tools."
                        })
                        break
                    continue  

                if assistant_message.get("content"):
                    text = assistant_message["content"]
                    if AIKA_ALWAYS_SHOW_SOURCES and collected_sources:
                        text = append_sources_to_text(text, collected_sources)
                    last_answer_text = text
                    last_answer_sources = collected_sources[:]
                    last_code_blocks = print_assistant(text)
                else:
                    # Force a final answer w/o tools
                    final_text, code_blocks = print_final_answer(conversation_history, collected_sources)
                    conversation_history.append({"role": "assistant", "content": final_text})
                    last_answer_text = final_text
                    last_answer_sources = collected_sources[:]
                    last_code_blocks = code_blocks
                break

            # If we broke out without an assistant message as the last turn, force a final answer
            if conversation_history and conversation_history[-1]["role"] != "assistant":
                final_text, code_blocks = print_final_answer(conversation_history, collected_sources)
                conversation_history.append({"role": "assistant", "content": final_text})
                last_answer_text = final_text
                last_answer_sources = collected_sources[:]
                last_code_blocks = code_blocks

        except KeyboardInterrupt:
            print_assistant("Interrupted. Type 'quit' to exit.")
            continue
        except Exception as e:
            print_assistant(f"An error occurred: {e}")
            time.sleep(0.2)

if __name__ == "__main__":
    main()
