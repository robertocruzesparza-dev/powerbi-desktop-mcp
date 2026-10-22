"""Tests for the setup wizard. Everything external (az, devtunnel, the console, HTTP, downloads) is
faked, so nothing is installed, signed in to, or created anywhere.

    python -m unittest discover -s copilot/tests -v
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[0]))  # copilot/

import setup_copilot as sc  # noqa: E402

_REAL_ROOT = sc.ROOT
_SANDBOX = None


def setUpModule():
    """The wizard writes packages and the IT request under ROOT/dist. Point ROOT at a throwaway folder so
    tests can never leave stub files in the real project (an earlier version left a fake .zip there)."""
    global _SANDBOX
    _SANDBOX = tempfile.TemporaryDirectory()
    sc.ROOT = Path(_SANDBOX.name)


def tearDownModule():
    sc.ROOT = _REAL_ROOT
    _SANDBOX.cleanup()


TENANT = "11111111-2222-3333-4444-555555555555"
CLIENT = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
OBJECT = "99999999-8888-7777-6666-555555555555"
AUTH_CFG = "0123abcd-0123-abcd-0123-0123abcd0123"
TEAMS_URI = "api://auth-abc/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
HOST = "abc123-8765.usw2.devtunnels.ms"

# Verbatim shape of `devtunnel host` output from Microsoft's docs.
TUNNEL_LINES = [
    "Hosting port: 8765",
    f"Connect via browser: https://{HOST}",
    "Inspect network activity: https://abc123-8765-inspect.usw2.devtunnels.ms",
    "Ready to accept connections for tunnel: test-tunnel.usw2",
]
SERVER_LINES = ["2026-09-18 INFO pbi_mcp.http: Serving MCP at http://127.0.0.1:8765/mcp (bind: loopback only)"]


class FakeProc(sc.Proc):
    def __init__(self, lines):
        self.lines, self.stopped = list(lines), False

    def alive(self):
        return not self.stopped

    def stop(self):
        self.stopped = True


class FakeShell:
    """Scripted stand-in for the shell; also simulates Entra applying PATCHes so re-runs are realistic."""

    def __init__(self, have=(), existing_apps=None, sp_exists=False, create_denied=False, app_readable=True):
        self.create_denied, self.app_readable = create_denied, app_readable
        self.have, self.calls, self.patches, self.started = set(have), [], [], []
        self.existing_apps, self.sp_exists = existing_apps or [], sp_exists
        self.app = {"appId": CLIENT, "id": OBJECT, "displayName": sc.APP_NAME, "identifierUris": [], "api": {}, "web": {}}
        self.tunnel_lines = TUNNEL_LINES

    def which(self, name):
        return name if name in self.have else None

    def _apply(self, body):
        self.app["identifierUris"] = body["identifierUris"]
        self.app["api"] = {**self.app["api"], **body["api"]}
        self.app["web"] = {**self.app["web"], **body["web"]}

    def run(self, cmd, **kw):
        self.calls.append(list(cmd))
        line = " ".join(cmd)
        if "signed-in-user" in line:  # must precede "user show": both contain it
            return sc.Result(0, "someone@district.example\n")
        if "user show" in line:
            return sc.Result(0, "Logged in as someone@district.example using Microsoft Entra ID")
        if "create --allow-anonymous" in line:
            return sc.Result(0, "Tunnel ID : test-tunnel.usw2\nAccess : anonymous")
        if "port create" in line:
            return sc.Result(0)
        if "account show" in line:
            return sc.Result(0, json.dumps({"tenantId": TENANT, "user": {"name": "someone@district.example"}}))
        if "ad app show" in line:
            return sc.Result(0, json.dumps(self.app)) if self.app_readable else sc.Result(1, "", "not found")
        if "ad app create" in line and self.create_denied:
            return sc.Result(1, "", "ERROR: Insufficient privileges to complete the operation.")
        if "ad app list" in line:
            return sc.Result(0, json.dumps(self.existing_apps))
        if "ad app create" in line:
            return sc.Result(0, json.dumps({"appId": CLIENT, "id": OBJECT, "displayName": sc.APP_NAME}))
        if "--method GET" in line:
            return sc.Result(0, json.dumps(self.app))
        if "--method PATCH" in line:
            body = json.loads(Path(cmd[cmd.index("--body") + 1][1:]).read_text(encoding="utf-8"))
            self.patches.append(body)
            self._apply(body)
            return sc.Result(0)
        if "ad sp show" in line:
            return sc.Result(0 if self.sp_exists else 1)
        if "ad sp create" in line:
            self.sp_exists = True  # like Entra: once created, it exists
        return sc.Result(0)

    def start(self, cmd, log_path):
        self.started.append(list(cmd))
        return FakeProc(SERVER_LINES if any("run_http_server" in c for c in cmd) else self.tunnel_lines)

    def count(self, text):
        return sum(1 for c in self.calls if text in " ".join(c))


class FakeUI:
    def __init__(self, answers=(), deny=()):
        self.answers, self.deny = list(answers), deny
        self.said, self.copied, self.opened, self.asked = [], [], [], []

    def say(self, msg=""):
        self.said.append(msg)

    def confirm(self, question):
        return not any(d in question for d in self.deny)

    def ask(self, prompt, validate=lambda s: bool(s), hint=""):
        value = self.answers.pop(0)
        assert validate(value), f"invalid scripted answer for {prompt!r}: {value!r}"
        self.asked.append(prompt)
        return value

    def pause(self, msg):
        pass

    def copy(self, text):
        self.copied.append(text)
        return True

    def open(self, url):
        self.opened.append(url)

    def reveal(self, path):
        self.revealed = path


def make_ctx(tmp, shell=None, ui=None, **kw):
    work = Path(tmp)
    built = []
    shell = shell or FakeShell(have=("az",))
    ui = ui or FakeUI([AUTH_CFG, TEAMS_URI])

    def fake_build(argv):
        out = Path(argv[argv.index("--out") + 1])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"zip")
        built.append(argv)

    def fetch(url, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"exe")

    ctx = sc.Ctx(shell=shell, ui=ui, state=sc.State.load(work / "state.json"), work=work,
                 http=lambda url, t: (200, '{"status": "ok"}'), fetch=fetch, verify_signed=lambda p: True,
                 build=fake_build, tunnel_wait=0.3, server_wait=0.3, **kw)
    ctx.built = built
    return ctx


class AzureCliLocationTests(unittest.TestCase):
    """Regression: `pip install azure-cli` creates Scripts\\az.bat (no az.exe), so the wizard
    looked for a file that never exists and silently fell back to asking for a tenant id."""

    def private_install(self, tmp, with_package=True):
        scripts = Path(tmp) / "azcli" / "Scripts"
        scripts.mkdir(parents=True)
        for name in ("python.exe", "az.bat", "az"):  # what pip really leaves behind
            (scripts / name).write_bytes(b"")
        if with_package:
            cli = Path(tmp) / "azcli" / "Lib" / "site-packages" / "azure" / "cli"
            cli.mkdir(parents=True)
            (cli / "__main__.py").write_text("")
        return scripts / "python.exe"

    def test_private_install_is_found_even_though_there_is_no_az_exe(self):
        with tempfile.TemporaryDirectory() as tmp:
            py = self.private_install(tmp)
            ctx = make_ctx(tmp, shell=FakeShell())  # no az on PATH
            self.assertEqual(sc.az_path(ctx), str(py))

    def test_private_install_runs_as_python_dash_m(self):
        self.assertEqual(sc.az_cmd(r"C:\x\azcli\Scripts\python.exe"), [r"C:\x\azcli\Scripts\python.exe", "-m", "azure.cli"])
        self.assertEqual(sc.az_cmd("az"), ["az"])

    def test_a_venv_without_the_package_is_not_mistaken_for_an_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.private_install(tmp, with_package=False)
            self.assertIsNone(sc.az_path(make_ctx(tmp, shell=FakeShell())))

    def test_nothing_installed_means_not_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(sc.az_path(make_ctx(tmp, shell=FakeShell())))

    def test_every_az_call_uses_the_python_dash_m_form(self):
        with tempfile.TemporaryDirectory() as tmp:
            py = self.private_install(tmp)
            shell = FakeShell()  # az is not on PATH; only the private copy exists
            shell.run_orig = shell.run
            shell.run = lambda cmd, **kw: shell.run_orig([c for c in cmd if c not in ("-m", "azure.cli")], **kw) \
                if list(cmd[:3]) == [str(py), "-m", "azure.cli"] else shell.run_orig(cmd, **kw)
            ctx = make_ctx(tmp, shell=shell)
            ctx.state.tenant_id = TENANT
            self.assertTrue(sc.step_az_login(ctx, str(py)))
            self.assertTrue(sc.step_entra_app(ctx, str(py)))
            self.assertTrue(shell.calls and all(c[0] == str(py) for c in shell.calls))

    def test_install_that_leaves_no_cli_says_so_instead_of_staying_silent(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = make_ctx(tmp, shell=FakeShell())  # pip "succeeds" but installs nothing
            self.assertIsNone(sc.step_az_install(ctx))
            self.assertTrue(any("isn't where expected" in m for m in ctx.ui.said))


class RestrictedTenantTests(unittest.TestCase):
    """A real situation on a locked-down tenant: 'Insufficient privileges' when creating the app."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._keep = sc.keep_running
        sc.keep_running = lambda ctx: 0
        self.addCleanup(setattr, sc, "keep_running", self._keep)

    def denied_ctx(self, answers=("",), **kw):
        return make_ctx(self._tmp.name, shell=FakeShell(have=("az",), create_denied=True), ui=FakeUI(list(answers)), **kw)

    def test_sandbox_is_active_so_tests_cannot_touch_the_real_project(self):
        self.assertNotEqual(sc.ROOT, _REAL_ROOT)

    def test_denied_creation_writes_an_it_request_and_pauses_instead_of_demanding_an_id(self):
        ctx = self.denied_ctx(answers=[""])  # user presses Enter at "Application (client) ID ... or press Enter"
        self.assertEqual(sc.cmd_setup(ctx), 1)
        request = sc.ROOT / "dist" / "IT-request-powerbi-copilot.txt"
        self.assertTrue(request.exists())
        self.assertTrue(any("Paused" in m for m in ctx.ui.said))
        self.assertEqual(ctx.shell.count("ad app create"), 1)
        self.assertEqual(ctx.state.client_id, "")
        self.assertEqual(ctx.shell.patches, [])  # nothing half-configured
        self.assertEqual(ctx.ui.revealed, str(request))

    def test_request_contains_everything_an_admin_needs(self):
        ctx = self.denied_ctx()
        sc.cmd_setup(ctx)
        text = (sc.ROOT / "dist" / "IT-request-powerbi-copilot.txt").read_text(encoding="utf-8")
        for needle in (sc.APP_NAME, "access_as_user", sc.TOKEN_STORE_CLIENT_ID, sc.CONSENT_REDIRECT, "Upload custom apps",
                       "someone@district.example", TENANT, "Insufficient privileges", "accessTokenAcceptedVersion", "Enterprise application"):
            self.assertIn(needle, text)
        self.assertNotIn("secret:", text.lower())

    def test_tunnel_is_still_stopped_when_setup_pauses(self):
        ctx = self.denied_ctx()
        self.assertEqual(sc.run_command(ctx, "setup"), 1)
        self.assertTrue(ctx.procs and all(p.stopped for p in ctx.procs))

    def test_it_replying_with_an_id_lets_setup_continue_in_the_same_run(self):
        ctx = self.denied_ctx(answers=[CLIENT, AUTH_CFG, TEAMS_URI])
        self.assertEqual(sc.cmd_setup(ctx), 0)
        self.assertEqual(ctx.state.client_id, CLIENT)
        self.assertTrue(Path(ctx.state.package).exists())

    def test_client_id_flag_uses_the_admin_created_app_and_never_creates_one(self):
        ctx = make_ctx(self._tmp.name, shell=FakeShell(have=("az",), create_denied=True), ui=FakeUI([AUTH_CFG, TEAMS_URI]), client_id=CLIENT)
        self.assertEqual(sc.cmd_setup(ctx), 0)
        self.assertEqual(ctx.shell.count("ad app create"), 0)
        self.assertEqual(ctx.shell.count("ad app show"), 1)
        self.assertEqual((ctx.state.client_id, ctx.state.object_id), (CLIENT, OBJECT))
        self.assertTrue(ctx.shell.patches)  # as owner it finishes the app configuration itself

    def test_client_id_flag_with_an_unreadable_app_says_so_and_does_not_create_another(self):
        ctx = make_ctx(self._tmp.name, shell=FakeShell(have=("az",), app_readable=False), ui=FakeUI([AUTH_CFG, TEAMS_URI]), client_id=CLIENT)
        sc.cmd_setup(ctx)
        self.assertEqual(ctx.shell.count("ad app create"), 0)
        self.assertTrue(any("Could not read app" in m for m in ctx.ui.said))

    def test_changing_the_client_id_forgets_the_old_apps_object_id(self):
        ctx = make_ctx(self._tmp.name, client_id=CLIENT)
        ctx.state.client_id, ctx.state.object_id = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", "old-object"
        sc.cmd_setup(ctx)
        self.assertEqual(ctx.state.object_id, OBJECT)

    def test_ctrl_c_stops_the_tunnel_and_exits_cleanly(self):
        ctx = make_ctx(self._tmp.name)
        proc = FakeProc([])
        ctx.procs.append(proc)
        sc.COMMANDS["boom"] = lambda c: (_ for _ in ()).throw(KeyboardInterrupt())
        self.addCleanup(sc.COMMANDS.pop, "boom")
        self.assertEqual(sc.run_command(ctx, "boom"), 130)
        self.assertTrue(proc.stopped)
        self.assertTrue(any("progress is saved" in m for m in ctx.ui.said))

    def test_end_of_input_is_treated_like_ctrl_c(self):
        ctx = make_ctx(self._tmp.name, ui=FakeUI([]))  # ask() on an empty script would raise IndexError, so use EOF
        sc.COMMANDS["eof"] = lambda c: (_ for _ in ()).throw(EOFError())
        self.addCleanup(sc.COMMANDS.pop, "eof")
        self.assertEqual(sc.run_command(ctx, "eof"), 130)


class PureHelperTests(unittest.TestCase):
    def test_tunnel_url_regex_takes_the_public_url_not_the_inspect_url(self):
        found = [m.group(0) for line in TUNNEL_LINES if (m := sc.TUNNEL_URL_RE.search(line))]
        self.assertEqual(found, [f"https://{HOST}"])
        self.assertEqual(sc.TUNNEL_URL_RE.search(TUNNEL_LINES[1]).group(1), "8765")

    def test_tunnel_id_regex(self):
        self.assertEqual(sc.TUNNEL_ID_RE.search(TUNNEL_LINES[3]).group(1), "test-tunnel.usw2")
        self.assertEqual(sc.TUNNEL_ID_RE.search("Tunnel ID            : wild-fog-1.usw2").group(1), "wild-fog-1.usw2")

    def test_validators(self):
        self.assertTrue(sc.is_guid(AUTH_CFG))
        self.assertFalse(sc.is_guid("not-a-guid"))
        self.assertFalse(sc.is_guid(""))
        self.assertTrue(sc.is_app_uri(TEAMS_URI))
        self.assertTrue(sc.is_app_uri("https://contoso.example/x"))
        self.assertFalse(sc.is_app_uri("contoso"))

    def test_state_round_trip_ignores_unknown_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.json"
            st = sc.State(client_id=CLIENT, allowed_users=["a@b.org"], public_host=HOST)
            st.save(path)
            data = json.loads(path.read_text())
            data["from_the_future"] = 1
            path.write_text(json.dumps(data))
            back = sc.State.load(path)
            self.assertEqual((back.client_id, back.allowed_users, back.mcp_url), (CLIENT, ["a@b.org"], f"https://{HOST}/mcp"))
            self.assertEqual(sc.State.load(Path(tmp) / "missing.json").client_id, "")

    def test_state_holds_no_secrets(self):
        self.assertFalse([f for f in sc.State.__dataclass_fields__ if "secret" in f or "token" in f or "password" in f])

    def test_server_args(self):
        st = sc.State(tenant_id=TENANT, client_id=CLIENT, teams_uri=TEAMS_URI, allowed_users=["me@d.org"], public_host=HOST)
        args = sc.server_args(st)
        self.assertEqual(args[:2], ["--tenant-id", TENANT])
        self.assertEqual([args[i + 1] for i, a in enumerate(args) if a == "--audience"], [CLIENT, f"api://{CLIENT}", TEAMS_URI])
        self.assertIn("me@d.org", args)
        self.assertEqual(args[args.index("--public-host") + 1], HOST)

    def test_ready_to_run_lists_what_is_missing(self):
        self.assertIn("Teams auth config", sc.State(public_host=HOST, client_id=CLIENT, tenant_id=TENANT, allowed_users=["a"]).ready_to_run())
        self.assertEqual(sc.State(public_host=HOST, client_id=CLIENT, tenant_id=TENANT, allowed_users=["a"], auth_config_id=AUTH_CFG).ready_to_run(), [])


class EntraPatchTests(unittest.TestCase):
    def fresh(self):
        return {"appId": CLIENT, "id": OBJECT, "identifierUris": [], "api": {}, "web": {}}

    def test_fresh_app_gets_everything_microsoft_requires(self):
        body, scope_id = sc.entra_patch(self.fresh(), [TEAMS_URI])
        self.assertEqual(body["identifierUris"], [f"api://{CLIENT}", TEAMS_URI])
        scope = body["api"]["oauth2PermissionScopes"][0]
        self.assertEqual((scope["value"], scope["type"], scope["isEnabled"], scope["id"]), ("access_as_user", "User", True, scope_id))
        self.assertEqual(body["api"]["preAuthorizedApplications"], [{"appId": sc.TOKEN_STORE_CLIENT_ID, "delegatedPermissionIds": [scope_id]}])
        self.assertEqual(body["web"]["redirectUris"], [sc.CONSENT_REDIRECT])
        self.assertEqual(body["api"]["requestedAccessTokenVersion"], 2)
        self.assertTrue(sc.is_guid(scope_id))

    def test_existing_settings_are_preserved(self):
        app = self.fresh()
        app.update(identifierUris=["api://other", f"api://{CLIENT}"], web={"redirectUris": ["https://mine.example/cb"]})
        app["api"] = {"oauth2PermissionScopes": [{"id": "s-1", "value": "other_scope", "isEnabled": True}],
                      "preAuthorizedApplications": [{"appId": "11111111-1111-1111-1111-111111111111", "delegatedPermissionIds": ["s-1"]}]}
        body, _ = sc.entra_patch(app, [TEAMS_URI])
        self.assertEqual(body["identifierUris"], ["api://other", f"api://{CLIENT}", TEAMS_URI])
        self.assertEqual([s["value"] for s in body["api"]["oauth2PermissionScopes"]], ["other_scope", "access_as_user"])
        self.assertEqual(len(body["api"]["preAuthorizedApplications"]), 2)
        self.assertEqual(body["web"]["redirectUris"], ["https://mine.example/cb", sc.CONSENT_REDIRECT])

    def test_running_twice_changes_nothing(self):
        app = self.fresh()
        first, _ = sc.entra_patch(app, [TEAMS_URI])
        app.update(identifierUris=first["identifierUris"], api=first["api"], web=first["web"])
        second, _ = sc.entra_patch(app, [TEAMS_URI])
        self.assertEqual(first, second)

    def test_disabled_scope_is_re_enabled_and_keeps_its_id(self):
        app = self.fresh()
        app["api"] = {"oauth2PermissionScopes": [{"id": "keep-me", "value": "access_as_user", "isEnabled": False}]}
        body, scope_id = sc.entra_patch(app)
        self.assertEqual(scope_id, "keep-me")
        self.assertTrue(body["api"]["oauth2PermissionScopes"][0]["isEnabled"])

    def test_token_store_is_added_to_an_existing_preauth_entry(self):
        app = self.fresh()
        app["api"] = {"oauth2PermissionScopes": [{"id": "sid", "value": "access_as_user", "isEnabled": True}],
                      "preAuthorizedApplications": [{"appId": sc.TOKEN_STORE_CLIENT_ID.upper(), "delegatedPermissionIds": ["old"]}]}
        body, _ = sc.entra_patch(app)
        entry = body["api"]["preAuthorizedApplications"]
        self.assertEqual(len(entry), 1)
        self.assertEqual(entry[0]["delegatedPermissionIds"], ["old", "sid"])


class StepTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.addCleanup(self._tmp.cleanup)

    def test_signed_download_is_kept(self):
        ctx = make_ctx(self.tmp)
        exe = sc.step_devtunnel_install(ctx)
        self.assertTrue(exe and Path(exe).exists())

    def test_unsigned_download_is_deleted_and_not_used(self):
        ctx = make_ctx(self.tmp)
        ctx.verify_signed = lambda p: False
        self.assertIsNone(sc.step_devtunnel_install(ctx))
        self.assertFalse((Path(self.tmp) / "bin" / "devtunnel.exe").exists())

    def test_declining_the_download_downloads_nothing(self):
        downloaded = []
        ctx = make_ctx(self.tmp, ui=FakeUI(deny=("Download",)))
        ctx.fetch = lambda u, d: downloaded.append(u)
        self.assertIsNone(sc.step_devtunnel_install(ctx))
        self.assertEqual(downloaded, [])

    def test_existing_devtunnel_on_path_is_used_without_downloading(self):
        ctx = make_ctx(self.tmp, shell=FakeShell(have=("devtunnel",)))
        ctx.fetch = lambda u, d: self.fail("must not download")
        self.assertEqual(sc.step_devtunnel_install(ctx), "devtunnel")

    def test_tunnel_creation_and_url_discovery(self):
        ctx = make_ctx(self.tmp)
        self.assertTrue(sc.step_tunnel_create(ctx, "devtunnel"))
        self.assertEqual(ctx.state.tunnel_id, "test-tunnel.usw2")
        port_call = next(c for c in ctx.shell.calls if c[1:3] == ["port", "create"])
        self.assertEqual(port_call[3:], ["test-tunnel.usw2", "--port-number", "8765", "--protocol", "http"])
        create_call = next(c for c in ctx.shell.calls if "--allow-anonymous" in c)
        self.assertIn("--expiration", create_call)
        proc = sc.start_tunnel(ctx, "devtunnel")
        self.assertIsNotNone(proc)
        self.assertEqual(ctx.state.public_host, HOST)
        self.assertEqual(ctx.ui.opened, [f"https://{HOST}"])  # first-visit "Continue" page

    def test_tunnel_creation_is_skipped_when_it_already_exists(self):
        ctx = make_ctx(self.tmp)
        ctx.state.tunnel_id = "have-one.usw2"
        self.assertTrue(sc.step_tunnel_create(ctx, "devtunnel"))
        self.assertEqual(ctx.shell.count("create --allow-anonymous"), 0)

    def test_tunnel_address_change_is_flagged(self):
        ctx = make_ctx(self.tmp)
        ctx.state.public_host, ctx.state.tunnel_visited = "old-8765.usw2.devtunnels.ms", True
        sc.start_tunnel(ctx, "devtunnel")
        self.assertTrue(any("address changed" in m for m in ctx.ui.said))

    def test_no_url_from_the_tunnel_fails_cleanly(self):
        shell = FakeShell()
        shell.tunnel_lines = ["Hosting port: 8765"]
        ctx = make_ctx(self.tmp, shell=shell)
        self.assertIsNone(sc.start_tunnel(ctx, "devtunnel"))

    def test_entra_app_is_created_after_consent_and_configured(self):
        ctx = make_ctx(self.tmp)
        ctx.state.tenant_id = TENANT
        self.assertTrue(sc.step_entra_app(ctx, "az"))
        self.assertEqual(ctx.shell.count("ad app create"), 1)
        self.assertEqual((ctx.state.client_id, ctx.state.object_id), (CLIENT, OBJECT))
        self.assertEqual(ctx.shell.count("ad sp create"), 1)
        self.assertEqual(len(ctx.shell.patches), 1)
        self.assertNotIn("--password", " ".join(" ".join(c) for c in ctx.shell.calls))  # no client secret anywhere

    def test_declining_creates_nothing_and_prints_manual_steps(self):
        ctx = make_ctx(self.tmp, ui=FakeUI(deny=("Create an app registration",)))
        self.assertFalse(sc.step_entra_app(ctx, "az"))
        self.assertEqual(ctx.shell.count("ad app create"), 0)
        self.assertTrue(any("Expose an API" in m for m in ctx.ui.said))

    def test_existing_app_is_reused_not_duplicated(self):
        shell = FakeShell(have=("az",), existing_apps=[{"appId": CLIENT, "id": OBJECT, "displayName": sc.APP_NAME}], sp_exists=True)
        ctx = make_ctx(self.tmp, shell=shell)
        self.assertTrue(sc.step_entra_app(ctx, "az"))
        self.assertEqual(shell.count("ad app create"), 0)
        self.assertEqual(shell.count("ad sp create"), 0)

    def test_similarly_named_app_is_not_mistaken_for_ours(self):
        shell = FakeShell(have=("az",), existing_apps=[{"appId": "x", "id": "y", "displayName": sc.APP_NAME + " (old)"}])
        ctx = make_ctx(self.tmp, shell=shell)
        sc.step_entra_app(ctx, "az")
        self.assertEqual(shell.count("ad app create"), 1)

    def test_patch_failure_falls_back_to_manual_steps(self):
        shell = FakeShell(have=("az",))
        shell.run_orig = shell.run
        shell.run = lambda cmd, **kw: sc.Result(1, "", "Forbidden") if "--method PATCH" in " ".join(cmd) else shell.run_orig(cmd, **kw)
        ctx = make_ctx(self.tmp, shell=shell)
        self.assertFalse(sc.step_entra_app(ctx, "az"))
        self.assertTrue(any("Expose an API" in m for m in ctx.ui.said))

    def test_teams_registration_copies_values_and_patches_the_uri(self):
        ctx = make_ctx(self.tmp)
        st = ctx.state
        st.public_host, st.client_id, st.object_id = HOST, CLIENT, OBJECT
        self.assertTrue(sc.step_teams_registration(ctx, "az"))
        self.assertIn(f"https://{HOST}/mcp", ctx.ui.copied)
        self.assertIn(f"api://{CLIENT}/access_as_user", ctx.ui.copied)
        self.assertIn(CLIENT, ctx.ui.copied)
        self.assertEqual((st.auth_config_id, st.teams_uri), (AUTH_CFG, TEAMS_URI))
        self.assertIn(TEAMS_URI, ctx.shell.patches[-1]["identifierUris"])
        self.assertEqual(ctx.ui.opened, [sc.TEAMS_PORTAL_URL])

    def test_teams_registration_is_skipped_once_done(self):
        ctx = make_ctx(self.tmp)
        ctx.state.auth_config_id, ctx.state.teams_uri = AUTH_CFG, TEAMS_URI
        self.assertTrue(sc.step_teams_registration(ctx, "az"))
        self.assertEqual(ctx.ui.asked, [])

    def test_server_start_checks_local_and_tunnel_health(self):
        ctx = make_ctx(self.tmp)
        ctx.state.public_host, ctx.state.tenant_id, ctx.state.client_id = HOST, TENANT, CLIENT
        ctx.state.allowed_users = ["me@d.org"]
        proc = sc.start_server(ctx)
        self.assertIsNotNone(proc)
        cmd = ctx.shell.started[0]
        self.assertTrue(cmd[1].endswith("run_http_server.py"))
        self.assertIn("me@d.org", cmd)

    def test_server_that_never_starts_is_reported(self):
        ctx = make_ctx(self.tmp)
        ctx.shell.start = lambda cmd, log: FakeProc(["Traceback: boom"])
        ctx.state.public_host = HOST
        self.assertIsNone(sc.start_server(ctx))
        self.assertTrue(any("boom" in m for m in ctx.ui.said))

    def test_package_is_built_once_and_rebuilt_when_the_url_changes(self):
        ctx = make_ctx(self.tmp)
        st = ctx.state
        st.public_host, st.auth_config_id = HOST, AUTH_CFG
        self.assertTrue(sc.step_package(ctx))
        argv = ctx.built[0]
        self.assertEqual(argv[argv.index("--mcp-url") + 1], f"https://{HOST}/mcp")
        self.assertEqual(argv[argv.index("--auth-config-id") + 1], AUTH_CFG)
        self.assertTrue(sc.step_package(ctx))
        self.assertEqual(len(ctx.built), 1)  # unchanged -> not rebuilt
        st.public_host = "new-8765.usw2.devtunnels.ms"
        sc.step_package(ctx)
        self.assertEqual(len(ctx.built), 2)

    def test_package_build_failure_is_reported(self):
        ctx = make_ctx(self.tmp)
        ctx.state.public_host, ctx.state.auth_config_id = HOST, AUTH_CFG
        ctx.build = lambda argv: sys.exit("error: package is invalid")
        self.assertFalse(sc.step_package(ctx))


class CommandTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.addCleanup(self._tmp.cleanup)
        self._keep = sc.keep_running
        sc.keep_running = lambda ctx: 0  # the real one blocks until Ctrl+C
        self.addCleanup(setattr, sc, "keep_running", self._keep)

    def test_full_setup_end_to_end(self):
        ctx = make_ctx(self.tmp)
        self.assertEqual(sc.cmd_setup(ctx), 0)
        st = ctx.state
        self.assertEqual((st.tenant_id, st.client_id, st.auth_config_id, st.teams_uri, st.public_host),
                         (TENANT, CLIENT, AUTH_CFG, TEAMS_URI, HOST))
        self.assertEqual(st.allowed_users, ["someone@district.example"])  # the signed-in user, and only them
        self.assertTrue(Path(st.package).exists())
        self.assertEqual(ctx.shell.count("ad app create"), 1)
        self.assertEqual(len(ctx.shell.patches), 2)  # before and after the Teams URI is known
        self.assertEqual(len(ctx.procs), 2)
        self.assertEqual(sc.State.load(ctx.state_path).client_id, CLIENT)  # persisted

    def test_second_run_does_no_repeat_work(self):
        first = make_ctx(self.tmp)
        sc.cmd_setup(first)
        shell = first.shell
        shell.calls.clear()
        again = make_ctx(self.tmp, shell=shell, ui=FakeUI([]))
        self.assertEqual(sc.cmd_setup(again), 0)
        for repeated in ("ad app create", "create --allow-anonymous", "ad sp create"):
            self.assertEqual(shell.count(repeated), 0, repeated)
        self.assertEqual(again.ui.asked, [])
        self.assertEqual(len(again.built), 0)

    def test_extra_allowed_users_are_added(self):
        ctx = make_ctx(self.tmp, extra_users=["colleague@district.example"])
        sc.cmd_setup(ctx)
        self.assertEqual(ctx.state.allowed_users, ["colleague@district.example", "someone@district.example"])

    def test_run_refuses_until_setup_is_finished(self):
        ctx = make_ctx(self.tmp)
        self.assertEqual(sc.cmd_run(ctx), 1)
        self.assertEqual(ctx.shell.started, [])

    def test_run_starts_tunnel_then_server(self):
        first = make_ctx(self.tmp)
        sc.cmd_setup(first)
        ctx = make_ctx(self.tmp, shell=FakeShell(have=("az", "devtunnel")))
        self.assertEqual(sc.cmd_run(ctx), 0)
        self.assertEqual(["devtunnel" in c[0] or "run_http_server" in c[1] for c in ctx.shell.started], [True, True])
        self.assertIn("run_http_server.py", ctx.shell.started[1][1])

    def test_status_changes_nothing_and_reports_everything_open(self):
        shell = FakeShell()  # nothing installed
        ctx = make_ctx(self.tmp, shell=shell)
        self.assertEqual(sc.cmd_status(ctx), 0)
        self.assertEqual(shell.started, [])
        self.assertEqual(shell.patches, [])
        self.assertTrue(any("[ ] devtunnel downloaded" in m for m in ctx.ui.said))
        self.assertFalse((Path(self.tmp) / "state.json").exists())

    def test_reset_deletes_only_local_settings(self):
        ctx = make_ctx(self.tmp)
        ctx.state.client_id, ctx.state.object_id, ctx.state.tunnel_id = CLIENT, OBJECT, "t.usw2"
        ctx.save()
        self.assertEqual(sc.cmd_reset(ctx), 0)
        self.assertFalse(ctx.state_path.exists())
        self.assertEqual(ctx.shell.calls, [])  # nothing remote touched
        self.assertTrue(any("az ad app delete" in m for m in ctx.ui.said))


if __name__ == "__main__":
    unittest.main()
