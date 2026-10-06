"""Local-image Baidu OCR with independently durable accounting.

Both ``general`` (with positions) and ``general_basic`` (without positions)
are supported. Coordinates, when available, refer to EXIF-normalized source
pixels. No resizing is performed. Official references (checked 2026-10):
https://ai.baidu.com/ai-doc/OCR/vk3h7y58v
https://ai.baidu.com/ai-doc/OCR/tk3h7y2aq
https://ai.baidu.com/ai-doc/REFERENCE/Ck3dwjhhu

Pricing reference: https://ai.baidu.com/ai-doc/OCR/9k3h7xuv6
At the entry postpaid tier, general (positions) is CNY 0.01/successful
call; general_basic (no positions) is CNY 0.005. Free quotas, packages and
volume tiers can change actual charges. Our attempt count is deliberately
more conservative than the provider's successful-call billing count.

The conservative 4096px / 4MiB *form-encoded* limit also fits older Baidu
limits. Larger images are tiled, not shrunk. The budget is cache-directory
wide, including failed/ambiguous attempts; OAuth is not charged. max_calls
initializes a new budget only; use set_budget() to explicitly change an existing
ceiling. Reopening never raises or lowers the persisted ceiling implicitly.
"""

from __future__ import annotations

import asyncio
import base64
from contextlib import closing
from dataclasses import dataclass
import hashlib
from io import BytesIO
import json
import math
import os
from pathlib import Path
import sqlite3
import time
from urllib.parse import urlencode
import warnings

import httpx
from PIL import Image, ImageOps, UnidentifiedImageError


BASE_URL = "https://aip.baidubce.com"
PREPROCESSING_VERSION = "tiles-v1-rgb-png-exif-4096-overlap100-form4m"
MAX_SIDE = 4096
MIN_SIDE = 15
OVERLAP = 100
MAX_FORM_BYTES = 4 * 1024 * 1024
MAX_ATTEMPTS = 3
TRANSIENT_CODES = {1, 2, 4, 18, 282000}
BASE_MODELS = {"general", "general_basic"}
POSITION_MODELS = {"general", "accurate"}


class _Failure(Exception):
    def __init__(self, message: str, *, budget: bool = False):
        self.message = message
        self.budget = budget
        super().__init__(message)


@dataclass(frozen=True)
class _Slice:
    image: str
    x: int
    y: int
    width: int
    height: int


def _starts(length: int) -> list[int]:
    starts = [0]
    while starts[-1] + MAX_SIDE < length:
        starts.append(starts[-1] + MAX_SIDE - OVERLAP)
    return starts


def _image_slices(raw: bytes) -> tuple[list[_Slice], list[str]]:
    notes = []
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(BytesIO(raw)) as source:
            if getattr(source, "n_frames", 1) > 1:
                notes.append("Only the first image frame is recognized.")
            if source.getexif().get(274, 1) != 1:
                notes.append("Coordinates refer to EXIF-normalized source pixels.")
            image = ImageOps.exif_transpose(source).convert("RGBA")
            background = Image.new("RGBA", image.size, "white")
            image = Image.alpha_composite(background, image).convert("RGB")

    slices = []

    def encode(x: int, y: int, width: int, height: int) -> None:
        crop = image.crop((x, y, x + width, y + height))
        if min(crop.size) < MIN_SIDE:
            padded = Image.new("RGB", (max(width, MIN_SIDE), max(height, MIN_SIDE)), "white")
            padded.paste(crop, (0, 0))
            crop = padded
            notes.append("Small slice padded to Baidu's 15px minimum; pixels not scaled.")
        buffer = BytesIO()
        crop.save(buffer, format="PNG")
        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
        form = {"image": encoded, "probability": "true", "vertexes_location": "true"}
        if len(urlencode(form).encode("ascii")) <= MAX_FORM_BYTES:
            slices.append(_Slice(encoded, x, y, width, height))
            return
        # Split further when entropy makes even a dimension-valid PNG too big.
        horizontal = width >= height
        length = width if horizontal else height
        if length <= 2 * MIN_SIDE:
            raise _Failure("Image slice cannot fit the upload size limit.")
        overlap = min(OVERLAP, length // 4)
        first = (length + overlap) // 2
        start = first - overlap
        if horizontal:
            encode(x, y, first, height)
            encode(x + start, y, width - start, height)
        else:
            encode(x, y, width, first)
            encode(x, y + start, width, height - start)

    for y in _starts(image.height):
        for x in _starts(image.width):
            encode(x, y, min(MAX_SIDE, image.width - x), min(MAX_SIDE, image.height - y))
    if len(slices) > 1:
        notes.append(f"Image tiled into {len(slices)} overlapping slices without resizing.")
    return slices, list(dict.fromkeys(notes))


def _confidence(row: dict) -> float | None:
    probability = row.get("probability")
    value = probability.get("average") if isinstance(probability, dict) else probability
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if math.isfinite(value) and 0 <= value <= 1:
            return float(value)
    return None


def _lines(payload: dict, tile: _Slice, index: int, model: str) -> list[dict]:
    rows = payload.get("words_result")
    if not isinstance(rows, list):
        raise _Failure("OCR returned an invalid result structure.")
    lines = []
    for line_index, row in enumerate(rows):
        if not isinstance(row, dict) or not isinstance(row.get("words"), str):
            raise _Failure("OCR returned an invalid text line.")
        text = row["words"].strip()
        if not text:
            continue
        line = {"text": text, "slice_index": index, "line_index": line_index,
                "model": model}
        location = row.get("location")
        polygon = row.get("vertexes_location")
        if isinstance(location, dict) and all(
            isinstance(location.get(k), int) and not isinstance(location[k], bool)
            and location[k] >= 0 for k in ("left", "top", "width", "height")
        ):
            line["box"] = [location["left"] + tile.x, location["top"] + tile.y,
                           location["width"], location["height"]]
        elif isinstance(polygon, list) and len(polygon) >= 3 and all(
            isinstance(p, dict) and all(isinstance(p.get(k), int) and not isinstance(p[k], bool)
                                       and p[k] >= 0 for k in ("x", "y")) for p in polygon
        ):
            line["polygon"] = [[p["x"] + tile.x, p["y"] + tile.y] for p in polygon]
        elif model in POSITION_MODELS:
            raise _Failure("Position OCR returned a text line without valid coordinates.")
        confidence = _confidence(row)
        if confidence is not None:
            line["confidence"] = confidence
        lines.append(line)
    return lines


def _box(line: dict) -> list[int]:
    if "box" in line:
        return line["box"]
    xs, ys = zip(*line["polygon"])
    return [min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)]


def _deduplicate(lines: list[dict]) -> list[dict]:
    if not lines or not all("box" in line or "polygon" in line for line in lines):
        # Basic OCR has no geometry. Preserve provider order rather than guessing
        # which repeated lines in adjacent tiles are genuine duplicates.
        return sorted(lines, key=lambda item: (item["slice_index"], item.get("line_index", 0)))
    result = []
    for line in sorted(lines, key=lambda item: (_box(item)[1], _box(item)[0], item["slice_index"])):
        x, y, w, h = _box(line)
        duplicate = None
        for i, previous in enumerate(result):
            if previous["slice_index"] == line["slice_index"] or previous["text"] != line["text"]:
                continue
            px, py, pw, ph = _box(previous)
            intersection = max(0, min(x + w, px + pw) - max(x, px)) * max(
                0, min(y + h, py + ph) - max(y, py))
            if intersection >= 0.5 * max(1, min(w * h, pw * ph)):
                duplicate = i
                break
        if duplicate is None:
            result.append(line)
        elif line.get("confidence", -1) > result[duplicate].get("confidence", -1):
            result[duplicate] = line
    return sorted(result, key=lambda item: (_box(item)[1], _box(item)[0]))


def _mean_confidence(lines: list[dict]) -> float | None:
    values = [line["confidence"] for line in lines if "confidence" in line]
    return sum(values) / len(values) if values else None


class BaiduOCR:
    def __init__(self, cache_dir: Path, max_calls: int = 50,
                 api_key: str | None = None, secret_key: str | None = None,
                 high_accuracy_retry: bool = False, model: str = "general"):
        if not isinstance(max_calls, int) or isinstance(max_calls, bool) or max_calls < 0:
            raise ValueError("max_calls must be a nonnegative integer")
        if model not in BASE_MODELS:
            raise ValueError(f"model must be one of: {', '.join(sorted(BASE_MODELS))}")
        if model == "general_basic" and high_accuracy_retry:
            raise ValueError("high_accuracy_retry requires the position-capable general model")
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._db_path = self.cache_dir / "baidu_ocr.sqlite3"
        self.max_calls = max_calls
        self._api_key = (os.getenv("BAIDU_OCR_API_KEY", "") if api_key is None else api_key).strip()
        self._secret_key = (os.getenv("BAIDU_OCR_SECRET_KEY", "") if secret_key is None else secret_key).strip()
        self.high_accuracy_retry = high_accuracy_retry
        self.model = model
        self._lock = asyncio.Lock()
        self._token = ""
        self._token_until = 0.0
        with closing(self._connect()) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS budget (id INTEGER PRIMARY KEY CHECK(id=1), ceiling INTEGER NOT NULL)")
            db.execute("INSERT OR IGNORE INTO budget VALUES (1, ?)", (max_calls,))
            db.execute("CREATE TABLE IF NOT EXISTS attempts (id INTEGER PRIMARY KEY, model TEXT NOT NULL, reserved_at REAL NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            self.max_calls = db.execute("SELECT ceiling FROM budget WHERE id=1").fetchone()[0]

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self._db_path, timeout=30)
        db.execute("PRAGMA synchronous=FULL")
        return db

    @property
    def credentials_configured(self) -> bool:
        return bool(self._api_key and self._secret_key)

    @property
    def calls_used(self) -> int:
        with closing(self._connect()) as db:
            return db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0]

    def status(self) -> dict:
        with closing(self._connect()) as db:
            ceiling = db.execute("SELECT ceiling FROM budget WHERE id=1").fetchone()[0]
            used = db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0]
        return {"credentials_configured": self.credentials_configured,
                "calls_used": used, "max_calls": ceiling}

    def set_budget(self, max_calls: int) -> None:
        """Explicitly change the shared durable ceiling, never the attempts.

        A ceiling below calls_used is allowed and prevents further calls.
        Existing instances read this ceiling again before every reservation.
        """
        if not isinstance(max_calls, int) or isinstance(max_calls, bool) or max_calls < 0:
            raise ValueError("max_calls must be a nonnegative integer")
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE budget SET ceiling=? WHERE id=1", (max_calls,))
        self.max_calls = max_calls

    def _reserve(self, model: str) -> None:
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            ceiling = db.execute("SELECT ceiling FROM budget WHERE id=1").fetchone()[0]
            if db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] >= ceiling:
                raise _Failure("Local OCR call budget exhausted.", budget=True)
            db.execute("INSERT INTO attempts(model, reserved_at) VALUES (?, ?)", (model, time.time()))

    def _cached(self, key: str) -> dict | None:
        with closing(self._connect()) as db:
            row = db.execute("SELECT value FROM cache WHERE key=?", (key,)).fetchone()
        if row is None:
            return None
        try:
            value = json.loads(row[0])
            if isinstance(value, dict):
                return value
        except (ValueError, TypeError):
            pass
        raise _Failure("Local OCR cache is invalid; retained accounting was not reset.")

    def _save(self, key: str, value: dict) -> None:
        with closing(self._connect()) as db, db:
            db.execute("INSERT OR REPLACE INTO cache VALUES (?, ?)",
                       (key, json.dumps(value, ensure_ascii=True, allow_nan=False)))

    def _redact(self, text: str) -> str:
        for secret in (self._api_key, self._secret_key, self._token):
            if secret:
                text = text.replace(secret, "[redacted]")
        return text

    async def _post(self, client: httpx.AsyncClient, path: str, *, params: dict,
                    data: dict | None = None) -> tuple[dict, bool]:
        try:
            response = await client.post(BASE_URL + path, params=params, data=data)
        except httpx.RequestError:
            return {"_failure": "Baidu request failed or timed out."}, True
        if response.status_code != 200:
            return {"_failure": f"Baidu HTTP status {response.status_code}."}, (
                response.status_code in {408, 429} or response.status_code >= 500)
        try:
            payload = response.json()
        except ValueError:
            return {"_failure": "Baidu returned invalid JSON."}, False
        if not isinstance(payload, dict):
            return {"_failure": "Baidu returned an invalid response structure."}, False
        if "_failure" in payload:
            return {"_failure": "Baidu returned a reserved internal response field."}, False
        return payload, False

    async def _oauth(self, client: httpx.AsyncClient) -> str:
        if self._token and time.monotonic() < self._token_until:
            return self._token
        for attempt in range(MAX_ATTEMPTS):
            payload, transient = await self._post(client, "/oauth/2.0/token", params={
                "grant_type": "client_credentials", "client_id": self._api_key,
                "client_secret": self._secret_key})
            token, expires = payload.get("access_token"), payload.get("expires_in", 2592000)
            if ("error" not in payload and "error_code" not in payload
                    and isinstance(token, str) and token
                    and isinstance(expires, (int, float)) and not isinstance(expires, bool)
                    and math.isfinite(expires) and expires > 0):
                self._token = token
                self._token_until = time.monotonic() + max(0, expires - 60)
                return token
            if not transient or attempt == MAX_ATTEMPTS - 1:
                raise _Failure(payload.get("_failure", "Baidu OAuth authentication failed."))
            await asyncio.sleep(0.25 * 2 ** attempt)
        raise AssertionError("unreachable")

    async def _ocr(self, client: httpx.AsyncClient, tile: _Slice, index: int,
                   model: str, key: str) -> tuple[list[dict], bool]:
        cached = self._cached(key)
        if cached is not None:
            return _lines(cached, tile, index, model), True
        for attempt in range(MAX_ATTEMPTS):
            state = self.status()
            if state["calls_used"] >= state["max_calls"]:
                raise _Failure("Local OCR call budget exhausted.", budget=True)
            token = await self._oauth(client)
            self._reserve(model)
            payload, transient = await self._post(client, f"/rest/2.0/ocr/v1/{model}",
                params={"access_token": token}, data={"image": tile.image,
                    "probability": "true", **({"vertexes_location": "true"}
                    if model in POSITION_MODELS else {})})
            code = payload.get("error_code")
            if isinstance(code, int) and not isinstance(code, bool):
                transient = code in TRANSIENT_CODES or code in {110, 111}
                if code in {110, 111}:
                    self._token = ""
                    self._token_until = 0
                failure = f"Baidu OCR error code {code}."
            elif "_failure" in payload:
                failure = payload["_failure"]
            elif code is not None or "error" in payload:
                failure = "Baidu OCR returned an invalid error response."
            else:
                lines = _lines(payload, tile, index, model)
                # Persist normalized fields only, never provider error messages or tokens.
                rows = []
                for line in lines:
                    row = {"words": self._redact(line["text"])}
                    if "box" in line:
                        x, y, w, h = line["box"]
                        row["location"] = {"left": x - tile.x, "top": y - tile.y, "width": w, "height": h}
                    elif "polygon" in line:
                        row["vertexes_location"] = [{"x": x - tile.x, "y": y - tile.y} for x, y in line["polygon"]]
                    if "confidence" in line:
                        row["probability"] = {"average": line["confidence"]}
                    rows.append(row)
                clean = {"words_result": rows}
                self._save(key, clean)
                return _lines(clean, tile, index, model), False
            if not transient or attempt == MAX_ATTEMPTS - 1:
                raise _Failure(failure)
            await asyncio.sleep(0.25 * 2 ** attempt)
        raise AssertionError("unreachable")

    async def recognize(self, image_path: Path) -> dict:
        """Recognize a local file; failed runs retain any completed slice evidence.

        calls_used is the durable lifetime total, not the cost of this image.
        cache_hit means the entire result was served without a new OCR attempt.
        High accuracy is requested only for empty/low-confidence (<0.8) slices.
        """
        async with self._lock:
            result = {"status": "error", "text": "", "lines": [], "provider": "baidu",
                      "model": self.model, "calls_used": self.calls_used,
                      "cache_hit": False, "warnings": []}
            initial_calls = result["calls_used"]
            models = {self.model}
            try:
                raw = Path(image_path).read_bytes()
                digest = hashlib.sha256(raw).hexdigest()
                prefix = f"{PREPROCESSING_VERSION}:{digest}"
                full_key = (f"result:{prefix}:base={self.model}:"
                            f"accurate={int(self.high_accuracy_retry)}")
                cached = self._cached(full_key)
                if cached is None and self.model == "general":
                    # Read the pre-model-configuration key so existing general
                    # results remain first-class cache hits after this upgrade.
                    cached = self._cached(
                        f"result:{prefix}:general:accurate={int(self.high_accuracy_retry)}")
                if cached is not None:
                    if (cached.get("status") != "ok" or not isinstance(cached.get("text"), str)
                            or not isinstance(cached.get("lines"), list)
                            or not isinstance(cached.get("warnings"), list)
                            or not isinstance(cached.get("model"), str)):
                        raise _Failure("Local OCR result cache is invalid; retained accounting was not reset.")
                    cached.update(calls_used=self.calls_used, cache_hit=True)
                    return cached
                if not self.credentials_configured:
                    result["status"] = "pending_credentials"
                    result["warnings"].append("Configure BAIDU_OCR_API_KEY and BAIDU_OCR_SECRET_KEY locally.")
                    return result
                slices, notes = _image_slices(raw)
                result["warnings"].extend(notes)
                async with httpx.AsyncClient(timeout=httpx.Timeout(30, connect=10), follow_redirects=False) as client:
                    for index, tile in enumerate(slices):
                        tile_key = f"slice:{prefix}:{tile.x},{tile.y},{tile.width},{tile.height}"
                        lines, _ = await self._ocr(client, tile, index, self.model,
                                                   tile_key + ":" + self.model)
                        result["lines"].extend(lines)
                        confidence = _mean_confidence(lines)
                        if self.high_accuracy_retry and (not lines or (confidence is not None and confidence < 0.8)):
                            models.add("accurate")
                            result["warnings"].append(f"High-accuracy retry requested for slice {index}; improvement is not guaranteed.")
                            higher, _ = await self._ocr(client, tile, index, "accurate", tile_key + ":accurate")
                            high_confidence = _mean_confidence(higher)
                            improved = bool(higher) and (not lines or (
                                high_confidence is not None and confidence is not None
                                and high_confidence > confidence
                                and sum(len(line["text"]) for line in higher) >= sum(len(line["text"]) for line in lines)))
                            if improved:
                                result["lines"] = [line for line in result["lines"] if line["slice_index"] != index] + higher
                            else:
                                result["warnings"].append(f"Retained standard result for slice {index}; retry did not demonstrate improvement.")
                        elif lines and confidence is None:
                            result["warnings"].append(f"Provider supplied no usable confidence for slice {index}.")
                result["status"] = "ok"
                if len(slices) > 1 and any(
                        "box" not in line and "polygon" not in line for line in result["lines"]):
                    result["warnings"].append(
                        "Basic OCR has no coordinates; tiled slice order was preserved and overlap duplicates may remain.")
            except _Failure as exc:
                result["status"] = "budget_exhausted" if exc.budget else "error"
                result["error"] = exc.message
            except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError, Image.DecompressionBombWarning):
                result["error"] = "Local image could not be read or safely decoded."
            except sqlite3.Error:
                result["error"] = "Local OCR accounting/cache failed; no budget reset was performed."
            result["lines"] = _deduplicate(result["lines"])
            result["text"] = "\n".join(line["text"] for line in result["lines"])
            result["model"] = "+".join(sorted(models, reverse=True))
            result["calls_used"] = self.calls_used
            result["cache_hit"] = result["status"] == "ok" and result["calls_used"] == initial_calls
            if result["status"] != "ok" and result["lines"]:
                result["warnings"].append("Partial OCR evidence; not all requested slices/models completed.")
            if result["status"] == "ok":
                try:
                    self._save(full_key, result)
                except sqlite3.Error:
                    result["warnings"].append("Complete-result cache write failed; successful slice caches remain reusable.")
            return result
