"""End-to-end tests for the Copilot (HTTP) build. No Power BI Desktop, tenant or
network needed: tokens are signed with a throwaway RSA key and the server is
given a fake JWKS, so the real auth middleware and real MCP transport run.

    python -m unittest discover -s copilot/tests -v
"""
import json
import socket
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import jwt  # noqa: E402
import uvicorn  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402

from pbi_mcp import copilot_server  # noqa: E402
from pbi_mcp.http_app import build_app  # noqa: E402
from pbi_mcp.http_auth import AuthConfig, EntraTokenVerifier, parse_user_list  # noqa: E402

TENANT = "11111111-2222-3333-4444-555555555555"
CLIENT = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
AUDIENCE = f"api://tunnel.example/{CLIENT}"
ME = "someone@district.example"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class _FakeJwks:
    def get_signing_key_from_jwt(self, token):
        class _K:
            key = KEY.public_key()
        return _K()


def make_token(key=KEY, alg="RS256", **overrides):
    now = int(time.time())
    claims = {
        "iss": f"https://login.microsoftonline.com/{TENANT}/v2.0", "tid": TENANT, "aud": AUDIENCE,
        "iat": now, "nbf": now, "exp": now + 600, "scp": "access_as_user",
        "preferred_username": ME, "oid": "00000000-0000-0000-0000-000000000001",
    }
    claims.update(overrides)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, key, algorithm=alg, headers={"kid": "test"})


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ServerCase(unittest.TestCase):
    """Runs the real ASGI app under uvicorn on a random loopback port."""

    @classmethod
    def setUpClass(cls):
        config = AuthConfig(TENANT, (AUDIENCE, CLIENT), parse_user_list(ME))
        app = build_app(EntraTokenVerifier(config, jwks_client=_FakeJwks()), public_hosts=["tunnel.example"])
        cls.port = free_port()
        cls.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=cls.port, log_level="error"))
        cls.thread = threading.Thread(target=cls.server.run, daemon=True)
        cls.thread.start()
        for _ in range(100):
            if cls.server.started:
                break
            time.sleep(0.05)
        else:
            raise RuntimeError("test server did not start")

    @classmethod
    def tearDownClass(cls):
        cls.server.should_exit = True
        cls.thread.join(timeout=5)

    def post(self, body, token="default", host=None, path="/mcp"):
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if token == "default":
            token = make_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if host:
            headers["Host"] = host
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=json.dumps(body).encode(), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.status, dict(resp.headers), resp.read().decode()
        except urllib.error.HTTPError as err:
            return err.code, dict(err.headers), err.read().decode()

    def rpc(self, method, params=None, **kw):
        status, headers, text = self.post({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}, **kw)
        return status, headers, (json.loads(text) if text.startswith("{") else text)

    def call_tool(self, name, arguments):
        status, _, body = self.rpc("tools/call", {"name": name, "arguments": arguments})
        self.assertEqual(status, 200, body)
        return body["result"]


class AuthTests(ServerCase):
    def test_health_is_open_and_says_nothing_else(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/healthz", timeout=5) as r:
            self.assertEqual(json.loads(r.read()), {"status": "ok"})

    def test_no_token_is_401_with_challenge(self):
        status, headers, _ = self.rpc("tools/list", token=None)
        self.assertEqual(status, 401)
        self.assertTrue({k.lower(): v for k, v in headers.items()}["www-authenticate"].startswith("Bearer"))

    def test_garbage_token_is_401(self):
        self.assertEqual(self.rpc("tools/list", token="not.a.jwt")[0], 401)

    def test_valid_token_is_accepted(self):
        self.assertEqual(self.rpc("tools/list")[0], 200)

    def test_client_id_audience_is_accepted(self):
        self.assertEqual(self.rpc("tools/list", token=make_token(aud=CLIENT))[0], 200)

    def test_wrong_audience_is_401(self):
        self.assertEqual(self.rpc("tools/list", token=make_token(aud="api://someone-else"))[0], 401)

    def test_wrong_tenant_is_401(self):
        other = "99999999-9999-9999-9999-999999999999"
        self.assertEqual(self.rpc("tools/list", token=make_token(tid=other, iss=f"https://login.microsoftonline.com/{other}/v2.0"))[0], 401)

    def test_wrong_issuer_is_401(self):
        self.assertEqual(self.rpc("tools/list", token=make_token(iss="https://evil.example/"))[0], 401)

    def test_v1_issuer_is_accepted(self):
        self.assertEqual(self.rpc("tools/list", token=make_token(iss=f"https://sts.windows.net/{TENANT}/"))[0], 200)

    def test_expired_is_401(self):
        self.assertEqual(self.rpc("tools/list", token=make_token(exp=int(time.time()) - 3600))[0], 401)

    def test_missing_exp_is_401(self):
        self.assertEqual(self.rpc("tools/list", token=make_token(exp=None))[0], 401)

    def test_signed_by_another_key_is_401(self):
        self.assertEqual(self.rpc("tools/list", token=make_token(key=OTHER_KEY))[0], 401)

    def test_alg_none_is_401(self):
        forged = jwt.encode({"aud": AUDIENCE, "exp": int(time.time()) + 600}, None, algorithm="none")
        self.assertEqual(self.rpc("tools/list", token=forged)[0], 401)

    def test_hs256_public_key_confusion_is_401(self):
        forged = jwt.encode({"aud": AUDIENCE, "exp": int(time.time()) + 600}, "secret" * 8, algorithm="HS256")
        self.assertEqual(self.rpc("tools/list", token=forged)[0], 401)

    def test_missing_scope_is_403(self):
        self.assertEqual(self.rpc("tools/list", token=make_token(scp="something_else"))[0], 403)

    def test_app_only_token_without_scp_is_403(self):
        self.assertEqual(self.rpc("tools/list", token=make_token(scp=None, roles=["x"]))[0], 403)

    def test_user_not_on_allow_list_is_403(self):
        status, _, _ = self.rpc("tools/list", token=make_token(preferred_username="intruder@district.example", oid="0-0"))
        self.assertEqual(status, 403)

    def test_allow_list_matches_by_object_id_too(self):
        cfg = AuthConfig(TENANT, (AUDIENCE,), parse_user_list("00000000-0000-0000-0000-000000000001"))
        claims = EntraTokenVerifier(cfg, jwks_client=_FakeJwks()).verify(make_token(preferred_username="renamed@district.example"))
        self.assertEqual(claims["oid"], "00000000-0000-0000-0000-000000000001")

    def test_unknown_host_header_is_refused(self):
        self.assertEqual(self.rpc("tools/list", host="attacker.example")[0], 421)

    def test_tunnel_host_header_is_allowed(self):
        self.assertEqual(self.rpc("tools/list", host="tunnel.example")[0], 200)

    def test_config_refuses_empty_allow_list(self):
        with self.assertRaises(ValueError):
            AuthConfig(TENANT, (AUDIENCE,), frozenset())
        AuthConfig(TENANT, (AUDIENCE,), frozenset(), allow_any_tenant_user=True)


class ToolSurfaceTests(ServerCase):
    def tools(self):
        status, _, body = self.rpc("tools/list")
        self.assertEqual(status, 200, body)
        return body["result"]["tools"]

    WRITE_TOOLS = {"propose_change", "finish_change"}

    def test_eleven_or_fewer_tools(self):
        # Microsoft: answer quality degrades past ~10 tools per plugin. The edit layer takes us to 11
        # (user-approved trade-off): find_dashboards merges two tools, finish_change merges apply+rollback.
        self.assertLessEqual(len(self.tools()), 11)

    def test_only_the_edit_tools_are_not_read_only(self):
        for tool in self.tools():
            expected = tool["name"] not in self.WRITE_TOOLS
            self.assertIs(tool["annotations"]["readOnlyHint"], expected, tool["name"])

    def test_schemas_are_flat_primitives(self):
        for tool in self.tools():
            for name, prop in tool["inputSchema"].get("properties", {}).items():
                blob = json.dumps(prop)
                self.assertNotIn("anyOf", blob, f"{tool['name']}.{name}")
                self.assertNotIn('"null"', blob, f"{tool['name']}.{name}")
                self.assertIn(prop.get("type"), ("string", "integer", "number", "boolean"), f"{tool['name']}.{name}")

    def test_results_are_not_duplicated_as_structured_content(self):
        for tool in self.tools():
            self.assertNotIn("outputSchema", tool, tool["name"])
        result = self.call_tool("find_dashboards", {})  # works with or without Power BI Desktop running
        self.assertNotIn("structuredContent", result)
        self.assertEqual(len(result["content"]), 1)
        self.assertIn("open_files", json.loads(result["content"][0]["text"]))

    def test_descriptions_are_present(self):
        for tool in self.tools():
            self.assertGreater(len(tool.get("description", "")), 40, tool["name"])

    def test_every_original_capability_is_reachable(self):
        """All 30 functions in server.py must still be callable through some tool."""
        text = json.dumps(self.tools())
        for aspect in ("summary", "tables", "columns", "measures", "relationships", "dax", "hierarchies",
                       "security_roles", "data_sources", "model_size", "row_count", "queries", "refresh_health",
                       "query_diagnostics", "connections", "sql_queries", "folding_risk", "sharepoint_patterns",
                       "dependencies", "mashup_traces", "relationship_integrity", "profile_table", "unused_fields"):
            self.assertIn(aspect, text)

    def test_dispatch_covers_every_enum_value(self):
        """Each Literal value must be handled by its function's if/elif chain."""
        import inspect
        import typing
        for fn, arg in ((copilot_server.inspect_model, "aspect"), (copilot_server.inspect_power_query, "aspect"),
                        (copilot_server.check_data_quality, "check")):
            values = typing.get_args(typing.get_type_hints(fn)[arg])
            source = inspect.getsource(fn)
            for value in values[:-1]:  # the last value is the `else` branch
                self.assertIn(f'"{value}"', source.split('"""')[2], f"{fn.__name__} has no branch for {value}")


class GuardTests(ServerCase):
    def is_error(self, name, arguments):
        result = self.call_tool(name, arguments)
        self.assertTrue(result.get("isError"), result)
        return result["content"][0]["text"]

    def test_dax_must_be_a_dax_query(self):
        for bad in ('{"refresh": {"type": "full"}}', "SELECT * FROM $SYSTEM.TMSCHEMA_TABLES", "DROP TABLE x",
                    "/* EVALUATE */ {\"delete\": {}}", "-- comment only", ""):
            self.assertIn("read-only DAX", self.is_error("run_dax_query", {"instance": "x", "dax_query": bad}), bad)

    def test_dax_accepts_evaluate_and_define_and_comments(self):
        for good in ("EVALUATE ROW(\"x\", 1)", "  evaluate row(\"x\",1)", "DEFINE MEASURE T[m]=1 EVALUATE ROW(\"m\",[m])",
                     "-- note\nEVALUATE ROW(\"x\", 1)", "/* note */ EVALUATE ROW(\"x\", 1)"):
            copilot_server.require_read_only_dax(good)  # must not raise

    def test_pbix_arguments_must_be_pbix(self):
        for name, args in (("get_report_layout", {"pbix_path": "C:\\Windows\\win.ini"}),
                           ("audit_dashboard", {"instance": "x", "pbix_path": "C:\\secrets.txt"}),
                           ("check_data_quality", {"instance": "x", "check": "unused_fields", "pbix_path": "a.zip"}),
                           ("compare_dashboards", {"left": "C:\\a.txt", "right": "C:\\b.pbix"})):
            self.assertIn(".pbix", self.is_error(name, args), name)

    def test_search_dirs_must_stay_inside_the_profile(self):
        self.assertIn("inside", self.is_error("find_dashboards", {"search_dirs": "C:\\Windows\\System32", "scan_disk": True}))

    def test_missing_required_argument_is_explained(self):
        self.assertIn("table", self.is_error("check_data_quality", {"instance": "x", "check": "profile_table"}))
        self.assertIn("table", self.is_error("inspect_model", {"instance": "x", "aspect": "row_count"}))

    def test_unknown_aspect_is_rejected(self):
        result = self.call_tool("inspect_model", {"instance": "x", "aspect": "drop_everything"})
        self.assertTrue(result.get("isError"), result)


class ShrinkTests(unittest.TestCase):
    def test_small_payload_untouched(self):
        self.assertEqual(json.loads(copilot_server.shrink({"a": [1, 2, 3]})), {"a": [1, 2, 3]})

    def test_non_dict_is_wrapped(self):
        self.assertEqual(json.loads(copilot_server.shrink([1, 2])), {"result": [1, 2]})

    def test_big_lists_are_cut_to_budget_and_flagged(self):
        payload = {"tables": [{"name": f"t{i}", "note": "x" * 200} for i in range(500)], "small": [1, 2]}
        out = copilot_server.shrink(payload, budget=5000)
        self.assertLessEqual(len(out), 5200)
        data = json.loads(out)
        self.assertIn("PARTIAL", data["_truncated"]["message"])
        self.assertEqual(data["_truncated"]["omitted"][0]["total"], 500)
        self.assertLess(len(data["tables"]), 500)
        self.assertEqual(data["small"], [1, 2])  # untouched: only the biggest list was cut

    def test_long_strings_are_capped(self):
        out = json.loads(copilot_server.shrink({"m": "let " + "x" * 50000}, budget=6000))
        self.assertLess(len(out["m"]), 2100)
        self.assertIn("chars]", out["m"])

    def test_nested_lists_are_found(self):
        payload = {"a": {"b": [{"rows": list(range(3000))}, {"rows": [1]}]}}
        out = json.loads(copilot_server.shrink(payload, budget=4000))
        self.assertLess(len(out["a"]["b"][0]["rows"]), 3000)
        self.assertIn("omitted", out["_truncated"])

    def test_non_json_values_do_not_crash(self):
        json.loads(copilot_server.shrink({"when": time.gmtime(0), "p": Path("x"), "s": {1, 2}}))


if __name__ == "__main__":
    unittest.main()
