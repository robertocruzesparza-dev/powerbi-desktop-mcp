"""Build the Microsoft 365 Copilot app package (a .zip) from copilot/appPackage.

    python copilot/build_package.py --mcp-url https://<tunnel-host>/mcp --auth-config-id <id>

It fills the two placeholders in ai-plugin.json, validates every JSON file against
Microsoft's published schemas plus the documented length limits, and zips the
result. Upload the zip via Teams > Apps > Manage your apps > Upload a custom app
(needs "Upload custom apps" enabled by your admin).

  --anonymous     build for a server with no authentication (development only)
  --pin-tools     freeze today's tool list into the package (mcp-tools.json)
                  instead of letting Copilot discover tools at runtime
  --offline       skip downloading the schemas (length/structure checks still run)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import struct
import sys
import urllib.request
import zipfile
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SRC = HERE / "appPackage"
CACHE = HERE / ".schema-cache"
DIST = ROOT / "dist"

SCHEMAS = {
    "manifest.json": "https://developer.microsoft.com/json-schemas/teams/v1.25/MicrosoftTeams.schema.json",
    "declarativeAgent.json": "https://developer.microsoft.com/json-schemas/copilot/declarative-agent/v1.8/schema.json",
    "ai-plugin.json": "https://developer.microsoft.com/json-schemas/copilot/plugin/v2.4/schema.json",
}
FILES = ["manifest.json", "declarativeAgent.json", "ai-plugin.json", "color.png", "outline.png"]


def fail(msg: str) -> None:
    sys.exit(f"error: {msg}")


def load_schema(name: str) -> dict:
    CACHE.mkdir(exist_ok=True)
    cached = CACHE / f"{name}.schema.json"
    if not cached.exists():
        with urllib.request.urlopen(SCHEMAS[name], timeout=30) as resp:
            cached.write_bytes(resp.read())
    return json.loads(cached.read_text(encoding="utf-8"))


def png_size(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        fail(f"{path.name} is not a PNG")
    return struct.unpack(">II", data[16:24])


def pinned_tools() -> list[dict]:
    """Today's tools/list, in the shape Microsoft expects in mcp_tool_description."""
    sys.path.insert(0, str(ROOT))
    from pbi_mcp.copilot_server import mcp

    tools = asyncio.run(mcp.list_tools())
    return [t.model_dump(mode="json", by_alias=True, exclude_none=True) for t in tools]


def limits(manifest: dict, agent: dict, plugin: dict) -> list[str]:
    """Length limits from the Microsoft docs that the JSON schemas don't all encode."""
    problems = []

    def cap(label: str, value: str, maximum: int) -> None:
        if not value.strip() or len(value) > maximum:
            problems.append(f"{label} must be 1-{maximum} characters (is {len(value)})")

    cap("manifest name.short", manifest["name"]["short"], 30)
    cap("manifest name.full", manifest["name"].get("full", "x"), 100)
    cap("manifest description.short", manifest["description"]["short"], 80)
    cap("manifest description.full", manifest["description"]["full"], 4000)
    # Microsoft's docs require these but its schema does not, so check them here.
    cap("agent name", agent.get("name", ""), 100)
    cap("agent description", agent.get("description", ""), 1000)
    cap("agent instructions", agent.get("instructions", ""), 8000)
    cap("agent disclaimer", agent.get("disclaimer", {"text": "x"})["text"], 500)
    if len(agent.get("conversation_starters", [])) > 12:
        problems.append("agent has more than 12 conversation starters")
    cap("plugin name_for_human", plugin["name_for_human"], 20)
    cap("plugin description_for_human", plugin["description_for_human"], 100)
    cap("plugin description_for_model", plugin["description_for_model"], 2048)
    return problems


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mcp-url", required=True, help="Public HTTPS URL of the MCP endpoint, e.g. https://abc-8765.usw2.devtunnels.ms/mcp")
    auth = ap.add_mutually_exclusive_group(required=True)
    auth.add_argument("--auth-config-id", help="Auth config id from Agents Toolkit / the Teams developer portal (Entra SSO or OAuth registration).")
    auth.add_argument("--anonymous", action="store_true", help="No authentication (development only).")
    ap.add_argument("--pin-tools", action="store_true")
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--out", type=Path, default=None, help="Output .zip (default: dist/powerbi-desktop-copilot-<version>.zip)")
    args = ap.parse_args(argv)

    url = urlparse(args.mcp_url)
    if url.scheme != "https" or not url.netloc:
        fail("--mcp-url must be an absolute https:// URL (Copilot reaches the server over the internet; localhost will not work)")
    if url.hostname in ("localhost", "127.0.0.1", "::1"):
        fail("--mcp-url points at localhost, which Microsoft 365 Copilot cannot reach; use the tunnel's public URL")
    if url.path.rstrip("/") != "/mcp":
        print(f"warning: the server serves MCP at /mcp but the URL path is {url.path!r}", file=sys.stderr)

    manifest = json.loads((SRC / "manifest.json").read_text(encoding="utf-8"))
    agent = json.loads((SRC / "declarativeAgent.json").read_text(encoding="utf-8"))
    plugin = json.loads((SRC / "ai-plugin.json").read_text(encoding="utf-8"))

    runtime = plugin["runtimes"][0]
    runtime["spec"]["url"] = args.mcp_url
    runtime["auth"] = {"type": "None"} if args.anonymous else {"type": "OAuthPluginVault", "reference_id": args.auth_config_id}

    extra: dict[str, bytes] = {}
    if args.pin_tools:
        tools = pinned_tools()
        extra["mcp-tools.json"] = json.dumps({"tools": tools}, indent=2).encode()
        plugin["functions"] = [{"name": t["name"]} for t in tools]
        runtime["run_for_functions"] = [t["name"] for t in tools]
        runtime["spec"]["mcp_tool_description"] = {"file": "mcp-tools.json"}

    problems = limits(manifest, agent, plugin)
    for name, expected in (("color.png", (192, 192)), ("outline.png", (32, 32))):
        if png_size(SRC / name) != expected:
            problems.append(f"{name} must be {expected[0]}x{expected[1]}")
    for ref in (manifest["icons"]["color"], manifest["icons"]["outline"], manifest["copilotAgents"]["declarativeAgents"][0]["file"],
                agent["actions"][0]["file"]):
        if ref not in FILES:
            problems.append(f"{ref} is referenced but not packaged")

    documents = {"manifest.json": manifest, "declarativeAgent.json": agent, "ai-plugin.json": plugin}
    if not args.offline:
        import jsonschema

        for name, doc in documents.items():
            validator = jsonschema.Draft7Validator(load_schema(name))
            for err in sorted(validator.iter_errors(doc), key=lambda e: list(e.absolute_path)):
                where = "/".join(map(str, err.absolute_path)) or "(root)"
                problems.append(f"{name}: {where}: {err.message[:200]}")
    if problems:
        fail("package is invalid:\n  - " + "\n  - ".join(problems))

    out = args.out or DIST / f"powerbi-desktop-copilot-{manifest['version']}.zip"
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for name, doc in documents.items():
            z.writestr(name, json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
        for name, data in extra.items():
            z.writestr(name, data)
        for name in ("color.png", "outline.png"):
            z.write(SRC / name, name)
    checked = "unchecked (offline)" if args.offline else "validated against Microsoft schemas"
    mode = "pinned tools" if args.pin_tools else "dynamic tool discovery"
    print(f"wrote {out}  [{checked}; {mode}; auth={'none' if args.anonymous else 'OAuthPluginVault'}]")


if __name__ == "__main__":
    main()
