"""Validate explicit public-list configuration without loading settings or env."""

from copy import deepcopy
import json
import math
from pathlib import Path
from urllib.parse import urlsplit


WORKSPACE = Path(__file__).resolve().parents[2]
MAX_CONFIG_BYTES = 1024 * 1024


def http_url(value):
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("url must be a nonempty HTTP(S) string")
    parts = urlsplit(value)
    if (parts.scheme not in {"http", "https"} or not parts.hostname
            or parts.username is not None or parts.password is not None
            or any(ord(char) <= 32 for char in value) or "\\" in value):
        raise ValueError("url must be public HTTP(S) without credentials")
    _ = parts.port
    return parts


def _path(value, name):
    parts = value.split(".") if isinstance(value, str) else value
    if (not isinstance(parts, list) or not parts
            or any(isinstance(p, bool) or not isinstance(p, (str, int))
                   or (isinstance(p, str) and not p) or (isinstance(p, int) and p < 0)
                   for p in parts)):
        raise ValueError(f"{name} must be a nonempty dotted path or key/index list")


def _integer(value, name, minimum, maximum=None):
    if (type(value) is not int or value < minimum
            or (maximum is not None and value > maximum)):
        raise ValueError(f"{name} must be an integer >= {minimum}" +
                         (f" and <= {maximum}" if maximum is not None else ""))


def validate_list_config(config):
    if not isinstance(config, dict):
        raise ValueError("list_config must be an object")
    json.dumps(config, allow_nan=False)
    http_url(config.get("url"))
    method = config.get("method", "GET")
    if not isinstance(method, str) or method.upper() not in {"GET", "POST"}:
        raise ValueError("method must be GET or POST")
    for name in ("pagination", "response", "query", "body", "headers", "fields", "http"):
        if not isinstance(config.get(name, {}), dict):
            raise ValueError(f"{name} must be an object")
    pagination, response = config.get("pagination", {}), config.get("response", {})
    _path(pagination.get("path"), "pagination.path")
    _path(response.get("items_path"), "response.items_path")
    if pagination.get("location", "query") not in {"query", "body"}:
        raise ValueError("pagination.location must be query or body")
    for name, default, minimum, maximum in (("start", 1, 0, None),
                                           ("increment", 1, 1, None),
                                           ("max_pages", 1000, 1, 10000)):
        _integer(pagination.get(name, default), f"pagination.{name}", minimum, maximum)
    for name, value in response.items():
        if name.endswith("_path") and value is not None:
            _path(value, f"response.{name}")
    for name, value in config.get("fields", {}).items():
        _path(value, f"fields.{name}")
    if type(response.get("null_items_are_empty", False)) is not bool:
        raise ValueError("response.null_items_are_empty must be boolean")
    if config.get("body_encoding", "json") not in {"json", "form"}:
        raise ValueError("body_encoding must be json or form")
    options = config.get("http", {})
    _integer(options.get("retries", 3), "http.retries", 0, 10)
    for name, default, zero_allowed in (("timeout", 30, False),
                                       ("connect_timeout", 10, False),
                                       ("interval", 0.5, True), ("backoff", 1, True)):
        value = options.get(name, default)
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or value < 0 or (value == 0 and not zero_allowed)):
            raise ValueError(f"invalid http.{name}")
    return deepcopy(config)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _resolve_list_config(value):
    if isinstance(value, (str, Path)):
        # The trusted anchor comes from this package, never from config['root'].
        anchor = (WORKSPACE / "config").resolve()
        path = Path(value)
        path = (path if path.is_absolute() else WORKSPACE / path).resolve()
        if not path.is_relative_to(anchor) or path.suffix.lower() != ".json":
            raise ValueError("list source paths must be JSON under workspace/config")
        with path.open("rb") as handle:
            raw = handle.read(MAX_CONFIG_BYTES + 1)
        if len(raw) > MAX_CONFIG_BYTES:
            raise ValueError("list source JSON exceeds 1 MiB")
        value = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_unique_object,
                           parse_constant=lambda value: (_ for _ in ()).throw(
                               ValueError(f"invalid JSON constant: {value}")))
    return validate_list_config(value)


def configured_list(url, options, config):
    if "list_config" in options:
        return _resolve_list_config(options["list_config"])
    sources = config.get("list_sources", {})
    if not isinstance(sources, dict):
        raise ValueError("list_sources must be an exact URL mapping")
    if url not in sources:
        return None
    return _resolve_list_config(sources[url])
