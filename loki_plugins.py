"""Loki plugin loader — auto-loads user-defined tools from ~/.loki_plugins.py.

If ~/.loki_plugins.py exists, Loki imports it and adds its tools to the active
tool list so the model can call them like any built-in tool.

─────────────────────────────────────────────────────────────────────────────
How to write your own plugin file (~/.loki_plugins.py):
─────────────────────────────────────────────────────────────────────────────

1. Define one Python function per tool. It must:
   - Accept keyword arguments matching the parameters in the schema
   - Return a str (the output the model will see)

2. Define TOOLS_SCHEMA as a list of Ollama-compatible tool dicts (same format
   as loki_sec.py / tools_schema in loki.py).

Example ~/.loki_plugins.py:
─────────────────────────────────────────────────────────────────────────────
import json, requests

def query_api(url, method="GET", body=""):
    try:
        r = requests.request(method, url, json=json.loads(body) if body else None, timeout=10)
        return json.dumps({"status": r.status_code, "body": r.text[:2000]}, indent=2)
    except Exception as e:
        return f"ERROR: {e}"

def read_clipboard():
    import subprocess
    out = subprocess.run(['xclip', '-o'], capture_output=True, text=True)
    return out.stdout[:3000] or "(clipboard empty)"

TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "query_api",
            "description": "Make an HTTP request to a URL and return the response.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url":    {"type": "string", "description": "Full URL to call"},
                    "method": {"type": "string", "description": "HTTP method", "enum": ["GET","POST","PUT","DELETE"]},
                    "body":   {"type": "string", "description": "JSON body for POST/PUT (optional)"},
                },
                "required": ["url"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_clipboard",
            "description": "Read the current X11 clipboard content.",
            "parameters": {"type": "object", "properties": {}, "required": []}
        }
    },
]
─────────────────────────────────────────────────────────────────────────────
"""
import importlib.util
import os
import sys

PLUGIN_FILE = os.path.expanduser("~/.loki_plugins.py")

_plugin_module = None
_plugin_tools: list = []
_plugin_names: set  = set()


def load():
    """Load ~/.loki_plugins.py if it exists. Returns (tools_schema, error_or_None)."""
    global _plugin_module, _plugin_tools, _plugin_names
    if not os.path.isfile(PLUGIN_FILE):
        return [], None
    try:
        spec = importlib.util.spec_from_file_location("_loki_user_plugins", PLUGIN_FILE)
        mod  = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _plugin_module = mod
        schema = getattr(mod, 'TOOLS_SCHEMA', [])
        _plugin_tools = schema
        _plugin_names = {t['function']['name'] for t in schema if 'function' in t}
        return schema, None
    except Exception as e:
        return [], str(e)


def tool_names() -> set:
    return _plugin_names


def dispatch(tool_name: str, args: dict) -> str:
    """Call the user-defined function for tool_name. Returns string output."""
    if _plugin_module is None:
        return f"ERROR: plugins not loaded"
    fn = getattr(_plugin_module, tool_name, None)
    if fn is None:
        return f"ERROR: plugin function '{tool_name}' not found in {PLUGIN_FILE}"
    try:
        result = fn(**args)
        return str(result) if result is not None else "(no output)"
    except Exception as e:
        return f"ERROR in plugin '{tool_name}': {e}"
