"""Guided setup and runner for the Microsoft 365 Copilot build.

    setup-copilot.cmd            first-time setup; safe to re-run (finished steps are skipped)
    setup-copilot.cmd run        start the tunnel + server (what you run day to day)
    setup-copilot.cmd status     what is done and what setup would do next (changes nothing)
    setup-copilot.cmd package    rebuild the app package (e.g. after the tunnel URL changed)
    setup-copilot.cmd reset      forget the saved settings (does not delete anything remote)

What it automates: the Python packages, the dev tunnel (download + sign-in + create), the
Microsoft Entra app registration (through the Azure CLI and Microsoft Graph), starting the
server, and building the app package. What it cannot: Microsoft documents no API for the
Teams "token store" registration, so it walks you through that one form with every value
already on your clipboard. Every step asks before it changes anything, and every automatic
step falls back to written instructions if it fails.

Nothing here needs admin rights, winget or a client secret. Settings live in
%LOCALAPPDATA%\\powerbi-desktop-mcp\\copilot (override with POWERBI_MCP_HOME); they hold
IDs only, no secrets.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
import webbrowser
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Sequence

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DEFAULT_HOME = Path(os.environ.get("POWERBI_MCP_HOME") or Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "powerbi-desktop-mcp")

TOKEN_STORE_CLIENT_ID = "ab3be6b7-f5df-413d-ac2d-abf1e3fd9c0b"  # Microsoft Enterprise token store
CONSENT_REDIRECT = "https://teams.microsoft.com/api/platform/v1.0/oAuthConsentRedirect"
SCOPE_NAME = "access_as_user"
APP_NAME = "Power BI Desktop MCP (Copilot)"
DEFAULT_PORT = 8765
DEVTUNNEL_URL = "https://aka.ms/TunnelsCliDownload/win-x64"
TEAMS_PORTAL_URL = "https://dev.teams.microsoft.com/tools"
TEAMS_APPS_URL = "https://teams.microsoft.com"
GRAPH_APP_URL = "https://graph.microsoft.com/v1.0/applications/{}"

TUNNEL_URL_RE = re.compile(r"https://[a-z0-9-]+-(\d+)\.[a-z0-9]+\.devtunnels\.ms", re.I)
TUNNEL_ID_RE = re.compile(r"(?:Ready to accept connections for tunnel:|Tunnel ID\s*:)\s*([A-Za-z0-9._-]+)")
GUID_RE = re.compile(r"^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$", re.I)


# --------------------------------------------------------------------------
# Plumbing that tests replace: running programs, talking to the user, HTTP
# --------------------------------------------------------------------------

@dataclass
class Result:
    rc: int
    out: str = ""
    err: str = ""

    @property
    def ok(self) -> bool:
        return self.rc == 0


class Proc:
    """A background program whose output is captured (and logged) by a reader thread."""

    def __init__(self, popen: subprocess.Popen, log_path: Path):
        self._p = popen
        self.lines: list[str] = []
        self._log = log_path.open("a", encoding="utf-8", errors="replace")
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        for line in self._p.stdout:  # type: ignore[union-attr]
            self.lines.append(line.rstrip("\r\n"))
            self._log.write(line)
            self._log.flush()

    def wait_for(self, pattern: re.Pattern, timeout: float, accept: Callable[[re.Match], bool] = lambda m: True):
        deadline, seen = time.time() + timeout, 0
        while True:
            for line in self.lines[seen:]:
                m = pattern.search(line)
                if m and accept(m):
                    return m
            seen = len(self.lines)
            if time.time() > deadline or not self.alive():
                return None
            time.sleep(0.1)

    def alive(self) -> bool:
        return self._p.poll() is None

    def stop(self) -> None:
        if self.alive():
            self._p.terminate()
            try:
                self._p.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self._p.kill()


class Shell:
    def __init__(self, env: dict[str, str] | None = None):
        self.env = env

    def which(self, name: str) -> str | None:
        return shutil.which(name)

    def run(self, cmd: Sequence[str], *, interactive: bool = False, timeout: int = 900) -> Result:
        """interactive=True hands the console to the program (sign-in prompts)."""
        try:
            if interactive:
                return Result(subprocess.run(list(cmd), env=self.env).returncode)
            p = subprocess.run(list(cmd), capture_output=True, text=True, timeout=timeout, env=self.env,
                               encoding="utf-8", errors="replace")
            return Result(p.returncode, p.stdout, p.stderr)
        except FileNotFoundError as exc:
            return Result(127, "", str(exc))
        except subprocess.TimeoutExpired:
            return Result(124, "", "timed out")

    def start(self, cmd: Sequence[str], log_path: Path) -> Proc:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        popen = subprocess.Popen(list(cmd), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=self.env,
                                 encoding="utf-8", errors="replace", cwd=str(ROOT))
        return Proc(popen, log_path)


class UI:
    def __init__(self, assume_yes: bool = False):
        self.assume_yes = assume_yes

    def say(self, msg: str = "") -> None:
        print(msg, flush=True)

    def confirm(self, question: str) -> bool:
        if self.assume_yes:
            self.say(f"{question} [yes: --yes]")
            return True
        return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")

    def ask(self, prompt: str, validate: Callable[[str], bool] = lambda s: bool(s), hint: str = "") -> str:
        while True:
            value = input(f"{prompt}: ").strip()
            if validate(value):
                return value
            self.say(f"  That doesn't look right. {hint}".rstrip())

    def pause(self, msg: str) -> None:
        input(f"{msg} (press Enter) ")

    def copy(self, text: str) -> bool:
        try:
            return subprocess.run(["clip"], input=text, text=True, timeout=10).returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def open(self, url: str) -> None:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001 -- a missing browser must not stop setup
            pass

    def reveal(self, path: str) -> None:
        """Show a file in File Explorer."""
        try:
            subprocess.Popen(["explorer", f"/select,{path}"])
        except OSError:
            pass


def http_get(url: str, timeout: float = 10) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, ""
    except Exception as exc:  # noqa: BLE001
        return 0, str(exc)


def download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".download")
    with urllib.request.urlopen(url, timeout=120) as resp, tmp.open("wb") as f:
        shutil.copyfileobj(resp, f)
    tmp.replace(dest)


def signed_by_microsoft(path: Path) -> bool:
    """Authenticode check (Windows). A download that isn't validly signed by Microsoft is refused."""
    ps = (f"$s = Get-AuthenticodeSignature -LiteralPath '{path}'; "
          "if ($s.Status -eq 'Valid' -and $s.SignerCertificate.Subject -match 'Microsoft') { 'OK' } else { 'BAD ' + $s.Status }")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], capture_output=True,
                             text=True, timeout=60).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return out.strip() == "OK"


# --------------------------------------------------------------------------
# Saved settings
# --------------------------------------------------------------------------

@dataclass
class State:
    port: int = DEFAULT_PORT
    tunnel_id: str = ""
    public_host: str = ""
    tunnel_visited: bool = False
    tenant_id: str = ""
    client_id: str = ""
    object_id: str = ""
    teams_uri: str = ""
    auth_config_id: str = ""
    allowed_users: list[str] = field(default_factory=list)
    package: str = ""
    package_for: str = ""
    uploaded: bool = False

    @classmethod
    def load(cls, path: Path) -> "State":
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")

    @property
    def mcp_url(self) -> str:
        return f"https://{self.public_host}/mcp" if self.public_host else ""

    @property
    def audiences(self) -> list[str]:
        return [a for a in (self.client_id, f"api://{self.client_id}" if self.client_id else "", self.teams_uri) if a]

    @property
    def scope(self) -> str:
        return f"api://{self.client_id}/{SCOPE_NAME}"

    def ready_to_run(self) -> list[str]:
        """What is still missing before `run` can work."""
        missing = []
        for label, value in (("tunnel", self.public_host), ("Entra app", self.client_id), ("tenant id", self.tenant_id),
                             ("allowed user", self.allowed_users), ("Teams auth config", self.auth_config_id)):
            if not value:
                missing.append(label)
        return missing


@dataclass
class Ctx:
    shell: Shell
    ui: UI
    state: State
    work: Path
    port: int = DEFAULT_PORT
    app_name: str = APP_NAME
    pin_tools: bool = False
    tenant: str = ""
    extra_users: list[str] = field(default_factory=list)
    client_id: str = ""  # an Entra app someone else created (--client-id)
    http: Callable[[str, float], tuple[int, str]] = http_get
    fetch: Callable[[str, Path], None] = download
    verify_signed: Callable[[Path], bool] = signed_by_microsoft
    build: Callable[[list[str]], None] | None = None
    tunnel_wait: float = 60  # seconds to wait for the tunnel to report its URL
    server_wait: float = 40  # seconds to wait for the server to start
    procs: list[Proc] = field(default_factory=list)

    @property
    def state_path(self) -> Path:
        return self.work / "state.json"

    @property
    def logs(self) -> Path:
        return self.work / "logs"

    def save(self) -> None:
        self.state.save(self.state_path)


# --------------------------------------------------------------------------
# Pure helpers (unit-tested)
# --------------------------------------------------------------------------

def entra_patch(app: dict, extra_uris: Sequence[str] = ()) -> tuple[dict, str]:
    """The Graph PATCH body that makes `app` a valid target for Copilot's Entra SSO, computed
    from the app's CURRENT settings so existing URIs, scopes and redirects are kept and
    running it twice changes nothing. Returns (body, scope_id)."""
    client_id = app["appId"]
    api = app.get("api") or {}
    scopes = [dict(s) for s in api.get("oauth2PermissionScopes") or []]
    scope = next((s for s in scopes if s.get("value") == SCOPE_NAME), None)
    if scope is None:
        scope = {
            "id": str(uuid.uuid4()), "value": SCOPE_NAME, "type": "User", "isEnabled": True,
            "adminConsentDisplayName": "Read Power BI Desktop analysis",
            "adminConsentDescription": "Allows Microsoft 365 Copilot to call the Power BI Desktop MCP server on behalf of the signed-in user.",
            "userConsentDisplayName": "Read Power BI Desktop analysis",
            "userConsentDescription": "Allows Microsoft 365 Copilot to analyze Power BI files on your PC for you.",
        }
        scopes.append(scope)
    scope["isEnabled"] = True

    pre = [dict(p) for p in api.get("preAuthorizedApplications") or []]
    entry = next((p for p in pre if str(p.get("appId", "")).lower() == TOKEN_STORE_CLIENT_ID), None)
    if entry is None:
        pre.append({"appId": TOKEN_STORE_CLIENT_ID, "delegatedPermissionIds": [scope["id"]]})
    else:
        entry["delegatedPermissionIds"] = sorted({*entry.get("delegatedPermissionIds", []), scope["id"]})

    uris = list(app.get("identifierUris") or [])
    for uri in (f"api://{client_id}", *extra_uris):
        if uri and uri not in uris:
            uris.append(uri)
    redirects = list((app.get("web") or {}).get("redirectUris") or [])
    if CONSENT_REDIRECT not in redirects:
        redirects.append(CONSENT_REDIRECT)

    body = {
        "identifierUris": uris,
        "api": {"requestedAccessTokenVersion": 2, "oauth2PermissionScopes": scopes, "preAuthorizedApplications": pre},
        "web": {"redirectUris": redirects},
    }
    return body, scope["id"]


def server_args(state: State) -> list[str]:
    args = ["--tenant-id", state.tenant_id]
    for audience in state.audiences:
        args += ["--audience", audience]
    for user in state.allowed_users:
        args += ["--allowed-user", user]
    args += ["--public-host", state.public_host, "--port", str(state.port)]
    return args


def is_guid(value: str) -> bool:
    return bool(GUID_RE.match(value.strip()))


def is_app_uri(value: str) -> bool:
    return bool(re.match(r"^(api|https)://\S+$", value.strip()))


MANUAL_ENTRA = """  Do this by hand in https://entra.microsoft.com (App registrations):
   1. New registration, single tenant. Copy the Application (client) ID.
   2. Expose an API: set the Application ID URI (default api://<client-id>), add a scope named
      {scope} (admins and users can consent).
   3. Expose an API > Add a client application: {store}, tick the scope.
   4. Authentication > Add a platform > Web: redirect URI {redirect}
   5. Manifest: set requestedAccessTokenVersion to 2."""


# --------------------------------------------------------------------------
# Steps. Each returns True when its part is done, False when the user must finish it by hand.
# --------------------------------------------------------------------------

NEEDED_MODULES = {"mcp": "mcp", "jwt": "PyJWT", "uvicorn": "uvicorn", "jsonschema": "jsonschema", "psutil": "psutil", "clr": "pythonnet"}


def has_module(name: str) -> bool:
    """find_spec raises for modules that are already imported without a spec (pythonnet's `clr`)."""
    if name in sys.modules:
        return True
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def step_python_deps(ctx: Ctx) -> bool:
    missing = [pkg for mod, pkg in NEEDED_MODULES.items() if not has_module(mod)]
    if not missing:
        ctx.ui.say("[ok] Python packages are installed.")
        return True
    if not ctx.ui.confirm(f"Install the missing Python packages ({', '.join(missing)}) with pip?"):
        ctx.ui.say(f"  Run: {sys.executable} -m pip install -r \"{HERE / 'requirements-copilot.txt'}\"")
        return False
    res = ctx.shell.run([sys.executable, "-m", "pip", "install", "-r", str(HERE / "requirements-copilot.txt")])
    if not res.ok:
        ctx.ui.say(f"  pip failed:\n{(res.err or res.out)[-600:]}")
        return False
    return True


def devtunnel_path(ctx: Ctx) -> str | None:
    found = ctx.shell.which("devtunnel")
    if found:
        return found
    private = ctx.work / "bin" / "devtunnel.exe"
    return str(private) if private.exists() else None


def step_devtunnel_install(ctx: Ctx) -> str | None:
    exe = devtunnel_path(ctx)
    if exe:
        ctx.ui.say(f"[ok] devtunnel found: {exe}")
        return exe
    dest = ctx.work / "bin" / "devtunnel.exe"
    if not ctx.ui.confirm(f"Download Microsoft's dev tunnel tool ({DEVTUNNEL_URL}) to {dest}? "
                          "It is checked for a valid Microsoft signature; no admin rights needed."):
        ctx.ui.say(f"  Install it yourself from https://learn.microsoft.com/azure/developer/dev-tunnels/get-started, then re-run.")
        return None
    try:
        ctx.fetch(DEVTUNNEL_URL, dest)
    except Exception as exc:  # noqa: BLE001
        ctx.ui.say(f"  Download failed: {exc}")
        return None
    if not ctx.verify_signed(dest):
        dest.unlink(missing_ok=True)
        ctx.ui.say("  The downloaded file is not validly signed by Microsoft, so it was deleted and NOT used.")
        return None
    return str(dest)


def _logged_in(res: Result) -> bool:
    text = res.out + res.err
    return res.ok and bool(re.search(r"logged in", text, re.I)) and not re.search(r"not logged in", text, re.I)


def step_tunnel_create(ctx: Ctx, exe: str) -> bool:
    if not _logged_in(ctx.shell.run([exe, "user", "show"])):
        ctx.ui.say("Sign in to dev tunnels with your work account (a browser or code prompt will appear).")
        ctx.shell.run([exe, "user", "login"], interactive=True)
        if not _logged_in(ctx.shell.run([exe, "user", "show"])):
            ctx.ui.say("  Dev tunnel sign-in did not complete. Run:  devtunnel user login   then re-run setup.")
            return False
    st = ctx.state
    if st.tunnel_id and st.port == ctx.port:
        ctx.ui.say(f"[ok] Tunnel already created ({st.tunnel_id}).")
        return True
    if not ctx.ui.confirm("Create a persistent dev tunnel that allows anonymous connections? (Copilot must be able to reach it; "
                          "the server itself rejects anyone without a valid Entra token.)"):
        return False
    res = ctx.shell.run([exe, "create", "--allow-anonymous", "--expiration", "30d"])
    if not res.ok:
        ctx.ui.say(f"  devtunnel create failed:\n{(res.err or res.out)[-500:]}")
        return False
    m = TUNNEL_ID_RE.search(res.out + res.err)
    st.tunnel_id = m.group(1) if m else ""
    port_cmd = [exe, "port", "create", *([st.tunnel_id] if st.tunnel_id else []), "--port-number", str(ctx.port), "--protocol", "http"]
    res = ctx.shell.run(port_cmd)
    if not res.ok and "already exists" not in (res.out + res.err).lower():
        ctx.ui.say(f"  Adding the port failed:\n{(res.err or res.out)[-500:]}")
        return False
    st.port = ctx.port
    ctx.save()
    return True


def start_tunnel(ctx: Ctx, exe: str) -> Proc | None:
    st = ctx.state
    proc = ctx.shell.start([exe, "host", *([st.tunnel_id] if st.tunnel_id else [])], ctx.logs / "tunnel.log")
    m = proc.wait_for(TUNNEL_URL_RE, ctx.tunnel_wait, accept=lambda m: m.group(1) == str(st.port))
    if not m:
        proc.stop()
        ctx.ui.say(f"  The tunnel did not report a URL. See {ctx.logs / 'tunnel.log'}")
        return None
    host = m.group(0).removeprefix("https://")
    if st.public_host and st.public_host != host:
        ctx.ui.say(f"  WARNING: the tunnel address changed ({st.public_host} -> {host}). Re-run `package` and update the Teams registration Base URL.")
    st.public_host = host
    if not st.tunnel_id:
        for line in proc.lines:
            idm = TUNNEL_ID_RE.search(line)
            if idm:
                st.tunnel_id = idm.group(1)
                break
    if not st.tunnel_visited:
        ctx.ui.say(f"First use of a new tunnel: open https://{host} once and select Continue (a browser error afterwards is expected).")
        ctx.ui.open(f"https://{host}")
        ctx.ui.pause("Done?")
        st.tunnel_visited = True
    ctx.save()
    ctx.ui.say(f"[ok] Tunnel address: https://{host}")
    return proc


def az_path(ctx: Ctx) -> str | None:
    """An `az` on PATH, else the python.exe of our private pip-installed copy. (A pip install of
    azure-cli creates az.bat, not az.exe, so the private copy is run as `python -m azure.cli`.)"""
    found = ctx.shell.which("az")
    if found:
        return found
    py = ctx.work / "azcli" / "Scripts" / "python.exe"
    installed = (ctx.work / "azcli" / "Lib" / "site-packages" / "azure" / "cli" / "__main__.py").exists()
    return str(py) if py.exists() and installed else None


def az_cmd(az: str) -> list[str]:
    """The command prefix that runs the Azure CLI, given what az_path returned."""
    return [az, "-m", "azure.cli"] if Path(az).name.lower() == "python.exe" else [az]


def step_az_install(ctx: Ctx) -> str | None:
    exe = az_path(ctx)
    if exe:
        ctx.ui.say(f"[ok] Azure CLI found: {exe}")
        return exe
    venv = ctx.work / "azcli"
    if not ctx.ui.confirm(f"Install the Azure CLI (used only to create the Entra app) into {venv} with pip? "
                          "It is a large download (several minutes), needs no admin rights and doesn't touch any az you already use."):
        ctx.ui.say("  Or install it from https://learn.microsoft.com/cli/azure/install-azure-cli, or do the Entra part by hand:")
        ctx.ui.say(MANUAL_ENTRA.format(scope=SCOPE_NAME, store=TOKEN_STORE_CLIENT_ID, redirect=CONSENT_REDIRECT))
        return None
    if not ctx.shell.run([sys.executable, "-m", "venv", str(venv)]).ok:
        ctx.ui.say("  Could not create the private environment.")
        return None
    py = venv / "Scripts" / "python.exe"
    res = ctx.shell.run([str(py), "-m", "pip", "install", "--disable-pip-version-check", "azure-cli"])
    if not res.ok:
        ctx.ui.say(f"  Installing the Azure CLI failed:\n{(res.err or res.out)[-600:]}")
        return None
    exe = az_path(ctx)
    if not exe:  # never fail silently: say what is missing
        ctx.ui.say(f"  pip finished, but the Azure CLI isn't where expected under {venv}. Falling back to manual steps.")
    return exe


def _az_json(ctx: Ctx, az: str, *args: str) -> tuple[Result, object]:
    res = ctx.shell.run([*az_cmd(az), *args, "-o", "json"])
    try:
        return res, json.loads(res.out) if res.out.strip() else None
    except ValueError:
        return res, None


def step_az_login(ctx: Ctx, az: str) -> bool:
    res, acct = _az_json(ctx, az, "account", "show")
    if not (res.ok and isinstance(acct, dict) and acct.get("tenantId")):
        ctx.ui.say("Sign in with the work account you use for Microsoft 365 Copilot (a browser window will open).")
        ctx.shell.run([*az_cmd(az), "login", "--allow-no-subscriptions", *(["--tenant", ctx.tenant] if ctx.tenant else [])], interactive=True)
        res, acct = _az_json(ctx, az, "account", "show")
        if not (res.ok and isinstance(acct, dict) and acct.get("tenantId")):
            ctx.ui.say("  Azure sign-in did not complete.")
            return False
    ctx.state.tenant_id = acct["tenantId"]
    upn = ctx.shell.run([*az_cmd(az), "ad", "signed-in-user", "show", "--query", "userPrincipalName", "-o", "tsv"]).out.strip() \
        or str((acct.get("user") or {}).get("name", ""))
    if upn:  # whoever runs setup is always allowed, in addition to any --allowed-user extras
        ctx.state.allowed_users = sorted({*ctx.state.allowed_users, upn})
    ctx.ui.say(f"[ok] Signed in to tenant {ctx.state.tenant_id} as {', '.join(ctx.state.allowed_users) or 'unknown'}")
    ctx.save()
    return True


def _patch_app(ctx: Ctx, az: str, extra_uris: Sequence[str]) -> bool:
    st = ctx.state
    res, app = _az_json(ctx, az, "rest", "--method", "GET", "--url", GRAPH_APP_URL.format(st.object_id))
    if not (res.ok and isinstance(app, dict)):
        ctx.ui.say(f"  Could not read the app registration:\n{(res.err or res.out)[-500:]}")
        return False
    body, _ = entra_patch(app, extra_uris)
    body_file = ctx.work / "entra-patch.json"
    body_file.parent.mkdir(parents=True, exist_ok=True)
    body_file.write_text(json.dumps(body), encoding="utf-8")
    res = ctx.shell.run([*az_cmd(az), "rest", "--method", "PATCH", "--url", GRAPH_APP_URL.format(st.object_id),
                         "--headers", "Content-Type=application/json", "--body", f"@{body_file}"])
    if not res.ok:
        ctx.ui.say(f"  Updating the app registration failed:\n{(res.err or res.out)[-600:]}")
        return False
    return True


def write_it_request(ctx: Ctx) -> Path:
    """Write the ready-to-send IT request (see it_request.py) next to the built packages."""
    import it_request  # same folder as this script

    st = ctx.state
    text = it_request.render(app_name=ctx.app_name, scope=SCOPE_NAME, user=", ".join(st.allowed_users),
                             tenant_id=st.tenant_id, mcp_url=st.mcp_url)
    path = ROOT / "dist" / "IT-request-powerbi-copilot.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def step_entra_app(ctx: Ctx, az: str) -> bool:
    st = ctx.state
    if st.client_id and not st.object_id:  # an app IT created (or --client-id): find it, don't create another
        res, app = _az_json(ctx, az, "ad", "app", "show", "--id", st.client_id)
        if res.ok and isinstance(app, dict) and app.get("id"):
            st.object_id = app["id"]
            ctx.save()
        else:
            ctx.ui.say(f"  Could not read app {st.client_id} (wrong id, or signed in to the wrong tenant). Continuing with that id.")
            return False
    if not st.object_id:
        res, apps = _az_json(ctx, az, "ad", "app", "list", "--display-name", ctx.app_name)
        match = next((a for a in apps or [] if a.get("displayName") == ctx.app_name), None) if isinstance(apps, list) else None
        if match:
            ctx.ui.say(f"[ok] Reusing the existing app registration '{ctx.app_name}'.")
        else:
            if not ctx.ui.confirm(f"Create an app registration named '{ctx.app_name}' in your organization's Microsoft Entra tenant "
                                  f"({st.tenant_id})? It only defines a sign-in scope for this server; no client secret is created."):
                ctx.ui.say(MANUAL_ENTRA.format(scope=SCOPE_NAME, store=TOKEN_STORE_CLIENT_ID, redirect=CONSENT_REDIRECT))
                return False
            res, match = _az_json(ctx, az, "ad", "app", "create", "--display-name", ctx.app_name, "--sign-in-audience", "AzureADMyOrg")
            if not (res.ok and isinstance(match, dict) and match.get("appId")):
                ctx.ui.say(f"  Creating the app failed (your tenant may restrict app registration):\n{(res.err or res.out)[-500:]}")
                ctx.ui.say(MANUAL_ENTRA.format(scope=SCOPE_NAME, store=TOKEN_STORE_CLIENT_ID, redirect=CONSENT_REDIRECT))
                return False
        st.client_id, st.object_id = match["appId"], match["id"]
        ctx.save()
    if not _patch_app(ctx, az, [st.teams_uri] if st.teams_uri else []):
        ctx.ui.say(MANUAL_ENTRA.format(scope=SCOPE_NAME, store=TOKEN_STORE_CLIENT_ID, redirect=CONSENT_REDIRECT))
        return False
    if not ctx.shell.run([*az_cmd(az), "ad", "sp", "show", "--id", st.client_id]).ok:
        ctx.shell.run([*az_cmd(az), "ad", "sp", "create", "--id", st.client_id])  # lets users in the tenant sign in to it
    ctx.ui.say(f"[ok] Entra app ready. Client ID: {st.client_id}")
    return True


def step_teams_registration(ctx: Ctx, az: str | None) -> bool:
    st = ctx.state
    if st.auth_config_id and st.teams_uri:
        ctx.ui.say("[ok] Teams auth config already registered.")
        return True
    rows = [
        ("Registration name", "Power BI Desktop MCP"),
        ("Base URL", st.mcp_url),
        ("Restrict usage by org", "(select your organization)"),
        ("Restrict usage by app", "Any Teams app"),
        ("Client ID", st.client_id),
        ("Scope", st.scope),
    ]
    ctx.ui.say("\nOne step Microsoft has no API for: register the sign-in in the Teams developer portal.")
    ctx.ui.say("A browser will open. Choose  Tools > Microsoft Entra SSO client ID registration > Register client ID.")
    ctx.ui.say("For each field I will copy the value to your clipboard; paste it into the form.\n")
    ctx.ui.open(TEAMS_PORTAL_URL)
    for label, value in rows:
        copied = not value.startswith("(") and ctx.ui.copy(value)  # "(select ...)" rows are instructions, not values
        ctx.ui.say(f"  {label}: {value}" + ("   [copied]" if copied else ""))
        ctx.ui.pause("  Fill that in")
    ctx.ui.say("\nSave the registration. The portal now shows two values; paste them here.")
    st.auth_config_id = ctx.ui.ask("Microsoft Entra SSO registration ID (a GUID)", is_guid, "It looks like 0123abcd-....")
    st.teams_uri = ctx.ui.ask("Application ID URI (api://... )", is_app_uri, "It starts with api:// or https://.")
    ctx.save()
    if az and st.object_id:
        if _patch_app(ctx, az, [st.teams_uri]):
            ctx.ui.say("[ok] Added that Application ID URI to your Entra app.")
            return True
    ctx.ui.say(f"  Add this to the app's identifierUris by hand (Entra > App registrations > Manifest): {st.teams_uri}")
    return False


def start_server(ctx: Ctx) -> Proc | None:
    st = ctx.state
    proc = ctx.shell.start([sys.executable, str(ROOT / "run_http_server.py"), *server_args(st)], ctx.logs / "server.log")
    if not proc.wait_for(re.compile(r"Serving MCP at"), ctx.server_wait):
        ctx.ui.say(f"  The server did not start:\n" + "\n".join(proc.lines[-6:]))
        proc.stop()
        return None
    for _ in range(20):
        if ctx.http(f"http://127.0.0.1:{st.port}/healthz", 3)[0] == 200:
            break
        time.sleep(0.5)
    else:
        ctx.ui.say("  The server started but did not answer /healthz.")
        proc.stop()
        return None
    ctx.ui.say(f"[ok] Server is running on 127.0.0.1:{st.port} (only signed-in, allow-listed users are served).")
    code, _ = ctx.http(f"https://{st.public_host}/healthz", 15)
    if code == 200:
        ctx.ui.say("[ok] It is reachable through the tunnel.")
    else:
        ctx.ui.say(f"  Could not confirm the tunnel path yet (got {code}). If Copilot can't connect, check {ctx.logs}.")
    return proc


def step_package(ctx: Ctx) -> bool:
    st = ctx.state
    stamp = f"{st.mcp_url}|{st.auth_config_id}|{ctx.pin_tools}"
    if st.package and Path(st.package).exists() and st.package_for == stamp:
        ctx.ui.say(f"[ok] App package is current: {st.package}")
        return True
    version = json.loads((HERE / "appPackage" / "manifest.json").read_text(encoding="utf-8"))["version"]
    out = ROOT / "dist" / f"powerbi-desktop-copilot-{version}.zip"
    argv = ["--mcp-url", st.mcp_url, "--auth-config-id", st.auth_config_id, "--out", str(out)] + (["--pin-tools"] if ctx.pin_tools else [])
    try:
        if ctx.build:
            ctx.build(argv)
        else:
            sys.path.insert(0, str(HERE))
            import build_package
            build_package.main(argv)
    except SystemExit as exc:
        ctx.ui.say(f"  Building the package failed: {exc}")
        return False
    st.package, st.package_for = str(out), stamp
    ctx.save()
    return True


def step_install(ctx: Ctx) -> bool:
    st = ctx.state
    atk = ctx.shell.which("atk")
    if atk and ctx.ui.confirm("The Microsoft 365 Agents Toolkit CLI (atk) is installed. Upload the agent to your account with it?"):
        ctx.shell.run([atk, "auth", "login", "m365"], interactive=True)
        res = ctx.shell.run([atk, "install", "--file-path", st.package, "--scope", "Personal"], interactive=True)
        if res.ok:
            st.uploaded = True
            ctx.save()
            return True
        ctx.ui.say("  atk could not upload it; do it by hand:")
    ctx.ui.say("\nUpload the agent in Teams:  Apps > Manage your apps > Upload an app > Upload a custom app")
    ctx.ui.say(f"  File: {st.package}")
    ctx.ui.reveal(st.package)
    ctx.ui.open(TEAMS_APPS_URL)
    if ctx.ui.confirm("Did the upload succeed?"):
        st.uploaded = True
        ctx.save()
        return True
    ctx.ui.say("  If the Upload option is missing, your admin must enable 'Upload custom apps' (Teams admin center > Setup policies).")
    return False


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def keep_running(ctx: Ctx) -> int:
    st = ctx.state
    ctx.ui.say(f"\nRunning. MCP URL: {st.mcp_url}")
    ctx.ui.say("Keep this window open while you use the agent (Power BI Desktop must also be open with your file). Ctrl+C stops everything.")
    try:
        while all(p.alive() for p in ctx.procs):
            time.sleep(1)
        ctx.ui.say("A component stopped unexpectedly. See the logs in " + str(ctx.logs))
        return 1
    except KeyboardInterrupt:
        ctx.ui.say("\nStopping...")
        return 0
    finally:
        for p in ctx.procs:
            p.stop()


def cmd_setup(ctx: Ctx) -> int:
    ui, st = ctx.ui, ctx.state
    ui.say("Setting up Power BI Desktop for Microsoft 365 Copilot.\n"
           "You will be asked before anything is installed or created. You will sign in three times (dev tunnels, Azure, and\n"
           "the Teams developer portal) because Microsoft requires interactive sign-in for each.\n")
    if ctx.extra_users:
        st.allowed_users = sorted({*st.allowed_users, *ctx.extra_users})
    if ctx.client_id:
        if ctx.client_id != st.client_id:
            st.object_id = ""
        st.client_id = ctx.client_id
    if not step_python_deps(ctx):
        return 1
    dt = step_devtunnel_install(ctx)
    if not dt or not step_tunnel_create(ctx, dt):
        return 1
    tunnel = start_tunnel(ctx, dt)
    if not tunnel:
        return 1
    ctx.procs.append(tunnel)

    az = step_az_install(ctx)
    if az and step_az_login(ctx, az):
        entra_ok = step_entra_app(ctx, az)
    else:
        entra_ok = False
        ui.say("\nTo find your tenant ID: sign in at https://entra.microsoft.com and copy 'Tenant ID' from Overview.")
        st.tenant_id = st.tenant_id or ui.ask("Your tenant (directory) ID (a GUID)", is_guid, "It looks like 0123abcd-0123-abcd-0123-0123abcd0123.")
    if not entra_ok and not st.client_id:
        request = write_it_request(ctx)
        ui.say("\nYour account can't create the Entra app, so your IT admin has to. I wrote a ready-to-send request:")
        ui.say(f"  {request}")
        ui.reveal(str(request))
        ui.say("When they reply with the Application (client) ID, run:  setup-copilot.cmd --client-id <that id>")
        answer = ui.ask("Application (client) ID if you already have it, or press Enter to pause setup here",
                        lambda s: s == "" or is_guid(s), "It looks like 0123abcd-0123-abcd-0123-0123abcd0123.")
        if not answer:
            ui.say("\nPaused. Your progress is saved (tunnel, sign-in). Re-run setup-copilot.cmd when you have the ID.")
            return 1
        st.client_id, st.object_id = answer, ""
        if az:  # signed in already: read the app now, and finish configuring it if we own it
            entra_ok = step_entra_app(ctx, az)
    if not st.allowed_users:
        st.allowed_users = [ui.ask("Your sign-in email/UPN (only this user may use the server)")]
    ctx.save()

    if not step_teams_registration(ctx, az if entra_ok else None):
        ui.say("\nSetup is not finished. Fix the item above and run setup again; completed steps are skipped.")
        return 1
    server = start_server(ctx)
    if not server:
        return 1
    ctx.procs.append(server)
    if not step_package(ctx):
        return 1
    step_install(ctx)
    ui.say("\nAll set. Open https://m365.cloud.microsoft/chat, choose 'Power BI Desktop Analyzer' under Agents, and ask: "
           "\"Run a full audit of my open Power BI file.\"")
    ui.say("Next time, just run:  setup-copilot.cmd run")
    return keep_running(ctx)


def cmd_run(ctx: Ctx) -> int:
    missing = ctx.state.ready_to_run()
    if missing:
        ctx.ui.say(f"Setup isn't finished (missing: {', '.join(missing)}). Run:  setup-copilot.cmd")
        return 1
    dt = devtunnel_path(ctx)
    if not dt:
        ctx.ui.say("devtunnel is missing. Run:  setup-copilot.cmd")
        return 1
    tunnel = start_tunnel(ctx, dt)
    if not tunnel:
        return 1
    ctx.procs.append(tunnel)
    server = start_server(ctx)
    if not server:
        tunnel.stop()
        return 1
    ctx.procs.append(server)
    return keep_running(ctx)


def cmd_package(ctx: Ctx) -> int:
    missing = ctx.state.ready_to_run()
    if missing:
        ctx.ui.say(f"Setup isn't finished (missing: {', '.join(missing)}).")
        return 1
    return 0 if step_package(ctx) else 1


def cmd_reset(ctx: Ctx) -> int:
    st = ctx.state
    ctx.state_path.unlink(missing_ok=True)
    ctx.ui.say("Saved settings deleted. Nothing remote was removed. To clean up yourself:")
    if st.object_id:
        ctx.ui.say(f"  az ad app delete --id {st.client_id}")
    if st.tunnel_id:
        ctx.ui.say(f"  devtunnel delete {st.tunnel_id}")
    ctx.ui.say("  and delete the registration in https://dev.teams.microsoft.com/tools, and the app in Teams > Manage your apps.")
    return 0


def checks(ctx: Ctx) -> list[tuple[str, bool, str]]:
    st, sh = ctx.state, ctx.shell
    dt, az = devtunnel_path(ctx), az_path(ctx)
    pkgs = all(has_module(m) for m in NEEDED_MODULES)
    dt_in = bool(dt) and _logged_in(sh.run([dt, "user", "show"]))
    az_in = False
    if az:
        res, acct = _az_json(ctx, az, "account", "show")
        az_in = res.ok and isinstance(acct, dict) and bool(acct.get("tenantId"))
    return [
        ("Python packages", pkgs, "will run pip install"),
        ("devtunnel downloaded", bool(dt), f"will download to {ctx.work / 'bin'}"),
        ("devtunnel signed in", dt_in, "will ask you to sign in"),
        ("Tunnel created", bool(st.tunnel_id and st.public_host), "will create a persistent anonymous tunnel"),
        ("Azure CLI installed", bool(az), f"will pip-install into {ctx.work / 'azcli'}"),
        ("Azure CLI signed in", az_in, "will ask you to sign in"),
        ("Entra app registration", bool(st.client_id), f"will create '{ctx.app_name}' (after asking)"),
        ("Teams auth config registered", bool(st.auth_config_id and st.teams_uri), "guided form with values on your clipboard"),
        ("App package built", bool(st.package and Path(st.package).exists()), "will build and validate the zip"),
        ("Agent uploaded to Teams", st.uploaded, "guided upload"),
    ]


def cmd_status(ctx: Ctx) -> int:
    for label, done, hint in checks(ctx):
        ctx.ui.say(f"  [{'x' if done else ' '}] {label}" + ("" if done else f"   -> {hint}"))
    st = ctx.state
    if st.public_host:
        ctx.ui.say(f"\nMCP URL: {st.mcp_url}   Allowed users: {', '.join(st.allowed_users) or '(none yet)'}")
    return 0


COMMANDS = {"setup": cmd_setup, "run": cmd_run, "status": cmd_status, "package": cmd_package, "reset": cmd_reset}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", nargs="?", default="setup", choices=COMMANDS)
    ap.add_argument("--yes", action="store_true", help="answer yes to every confirmation (sign-ins and the Teams form still need you)")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--pin-tools", action="store_true", help="freeze the tool list into the package instead of runtime discovery")
    ap.add_argument("--app-name", default=APP_NAME)
    ap.add_argument("--tenant", default="", help="tenant id or domain for the Azure sign-in")
    ap.add_argument("--client-id", default="", help="client id of an Entra app your IT admin created for you (skips creating one)")
    ap.add_argument("--allowed-user", action="append", default=[], help="extra user allowed to use the server (repeatable)")
    args = ap.parse_args(argv)

    work = DEFAULT_HOME / "copilot"
    work.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "AZURE_CONFIG_DIR": str(work / "az")}  # keep this wizard's Azure login separate from any other
    ctx = Ctx(shell=Shell(env), ui=UI(args.yes), state=State.load(work / "state.json"), work=work, port=args.port,
              app_name=args.app_name, pin_tools=args.pin_tools, tenant=args.tenant, extra_users=args.allowed_user,
              client_id=args.client_id.strip())
    if ctx.client_id and not is_guid(ctx.client_id):
        sys.exit("error: --client-id must be a GUID like 0123abcd-0123-abcd-0123-0123abcd0123")
    if sys.platform != "win32":
        ctx.ui.say("Note: this wizard is written for Windows (the Power BI Desktop server is Windows-only).")
    return run_command(ctx, args.command)


def run_command(ctx: Ctx, name: str) -> int:
    """Run a command; whatever way it ends (done, error, Ctrl+C), stop the tunnel and server it started
    so no orphaned process is left holding the tunnel."""
    try:
        return COMMANDS[name](ctx)
    except (KeyboardInterrupt, EOFError):
        ctx.ui.say("\nStopped. Your progress is saved; run the command again to continue.")
        return 130
    finally:
        for proc in ctx.procs:
            proc.stop()


if __name__ == "__main__":
    sys.exit(main())
