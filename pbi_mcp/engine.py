"""Thin wrapper around ADOMD.NET (via pythonnet) for talking to the local
Analysis Services engine that Power BI Desktop runs while a file is open.
"""
from __future__ import annotations

import sys
import threading
from typing import Any

from .discovery import PbiInstance, find_adomd_dll, find_running_instances

_load_lock = threading.Lock()
_adomd_types: dict[str, Any] | None = None


def _load_adomd() -> dict[str, Any]:
    """Boot the CLR and load ADOMD.NET types exactly once per process."""
    global _adomd_types
    with _load_lock:
        if _adomd_types is not None:
            return _adomd_types

        import clr  # noqa: F401  (pythonnet's CLR loader)

        dll_path = find_adomd_dll()
        dll_dir = str(dll_path.parent)
        if dll_dir not in sys.path:
            sys.path.append(dll_dir)

        clr.AddReference(str(dll_path))

        # Different Power BI Desktop builds ship the client under different
        # namespaces even though the concrete assembly file name varies
        # (Microsoft.PowerBI.AdomdClient.dll vs the classic SQL Server one).
        # Try both known namespaces before giving up.
        last_error: Exception | None = None
        for namespace in ("Microsoft.AnalysisServices.AdomdClient", "Microsoft.PowerBI.AdomdClient"):
            try:
                module = __import__(namespace, fromlist=["AdomdConnection", "AdomdCommand"])
                _adomd_types = {
                    "AdomdConnection": module.AdomdConnection,
                    "AdomdCommand": module.AdomdCommand,
                }
                return _adomd_types
            except Exception as exc:  # noqa: BLE001
                last_error = exc

        raise RuntimeError(
            f"Loaded {dll_path} but could not find AdomdConnection under a known "
            "namespace."
        ) from last_error


def list_instances() -> list[PbiInstance]:
    """Discover every currently-open Power BI Desktop file: its name (from
    the process command line, else the window title) and the local Analysis
    Services port its engine is listening on.
    """
    return find_running_instances()


def resolve_instance(identifier: str) -> PbiInstance:
    """Resolve a user-supplied identifier (the file's display name, or a
    bare port number as a string) to a live PbiInstance. Raises ValueError
    with a helpful list of what *is* open if nothing matches.
    """
    instances = list_instances()
    if not instances:
        raise ValueError(
            "No open Power BI Desktop files were found. Make sure Power BI "
            "Desktop is running with a .pbix file open, then try again."
        )

    by_port = [i for i in instances if str(i.port) == identifier]
    if by_port:
        return by_port[0]
    matches = [i for i in instances if i.display_name == identifier]
    if len(matches) > 1:
        # The same name can appear twice, e.g. a leftover engine with no model
        # loaded next to the freshly opened file. Prefer the one that has a model.
        loaded = [i for i in matches if model_loaded(i.port) is not False]
        if len(loaded) == 1:
            return loaded[0]
        if not loaded:
            matches = matches[:1]
        else:
            ports = ", ".join(str(i.port) for i in loaded)
            raise ValueError(
                f"{identifier!r} is open more than once (ports {ports}). Pass the port number as `instance` to choose one."
            )
    if matches:
        return matches[0]

    available = ", ".join(f"{i.display_name!r} (port {i.port})" for i in instances)
    raise ValueError(f"No open Power BI Desktop file matches {identifier!r}. Currently open: {available}")


def _connection_string(port: int) -> str:
    return f"Data Source=localhost:{port}"


def _open_connection(port: int):
    types = _load_adomd()
    conn = types["AdomdConnection"](_connection_string(port))
    conn.Open()
    return conn


def _to_python(value: Any) -> Any:
    """Best-effort conversion of a .NET value coming back from ADOMD into a
    plain Python/JSON-serialisable value.
    """
    if value is None:
        return None
    type_name = type(value).__name__
    if type_name == "DBNull":
        return None
    if type_name in ("DateTime",):
        return str(value)
    if isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def _clean_column_name(name: str) -> str:
    """DAX table functions (INFO.VIEW.*, etc.) return column names wrapped
    in brackets, e.g. "[Name]". Strip that wrapper for readability.
    """
    if name.startswith("[") and name.endswith("]"):
        return name[1:-1]
    return name


class ModelNotLoadedError(RuntimeError):
    """Power BI Desktop's engine is running but has no report loaded (the file
    was closed, is still opening, or a dialog is blocking the load)."""


_NO_MODEL_MESSAGE = (
    "Power BI Desktop is running, but no model is loaded in it (the file was "
    "closed, is still opening, or a dialog such as a sign-in/upgrade prompt is "
    "open). Open the .pbix in Power BI Desktop, wait until it finishes loading, "
    "then call list_open_files again."
)


def _execute(port: int, query: str, max_rows: int) -> tuple[list[dict[str, Any]], bool]:
    """Run a query and return (rows, truncated). Reads one row past
    `max_rows` so `truncated` is exact rather than a guess from len == max."""
    try:
        return _execute_raw(port, query, max_rows)
    except Exception as exc:  # noqa: BLE001 -- .NET exceptions arrive as arbitrary types
        if "CurrentCatalog" in str(exc):
            raise ModelNotLoadedError(_NO_MODEL_MESSAGE) from exc
        raise


def model_loaded(port: int) -> bool | None:
    """True if the engine at `port` has a model, False if it has none, None if
    that couldn't be determined (e.g. the connection itself failed)."""
    try:
        _execute_raw(port, 'EVALUATE ROW("x", 1)', 1)
        return True
    except Exception as exc:  # noqa: BLE001
        return False if "CurrentCatalog" in str(exc) else None


def _execute_raw(port: int, query: str, max_rows: int) -> tuple[list[dict[str, Any]], bool]:
    conn = _open_connection(port)
    types = _load_adomd()
    try:
        cmd = types["AdomdCommand"](query, conn)
        reader = cmd.ExecuteReader()
        try:
            columns = [_clean_column_name(reader.GetName(i)) for i in range(reader.FieldCount)]
            rows: list[dict[str, Any]] = []
            truncated = False
            while reader.Read():
                if len(rows) >= max_rows:
                    truncated = True
                    break
                rows.append({col: _to_python(reader.GetValue(i)) for i, col in enumerate(columns)})
            return rows, truncated
        finally:
            reader.Close()
    finally:
        conn.Close()


# Internal metadata reads (DMVs, INFO.VIEW) use a high cap: a silent cut-off
# there would quietly undercount things like VertiPaq segments on a big model.
_INTERNAL_MAX_ROWS = 200_000


def run_dax(instance: PbiInstance, dax: str, max_rows: int = _INTERNAL_MAX_ROWS) -> list[dict[str, Any]]:
    return _execute(instance.port, dax, max_rows)[0]


def run_dax_ex(instance: PbiInstance, dax: str, max_rows: int) -> tuple[list[dict[str, Any]], bool]:
    """Like run_dax, but also reports whether the result was cut off at max_rows."""
    return _execute(instance.port, dax, max_rows)


def run_dmv(instance: PbiInstance, query: str, max_rows: int = _INTERNAL_MAX_ROWS) -> list[dict[str, Any]]:
    return _execute(instance.port, query, max_rows)[0]
