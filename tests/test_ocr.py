"""Deterministic OCR tests: all HTTP uses MockTransport, never paid requests."""

import asyncio
import base64
from contextlib import closing, contextmanager
from io import BytesIO
import json
from pathlib import Path
import random
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode

import httpx
from PIL import Image

from jobprep.ocr import BaiduOCR, MAX_FORM_BYTES, PREPROCESSING_VERSION, _image_slices


API_KEY = "fixture-api-key-secret"
SECRET_KEY = "fixture-secret-key-secret"
TOKEN = "fixture-oauth-token-secret"
REAL_CLIENT = httpx.AsyncClient


def row(text="job", x=5, y=10, confidence=0.95):
    item = {"words": text, "location": {"left": x, "top": y, "width": 40, "height": 20}}
    if confidence is not None:
        item["probability"] = {"average": confidence}
    return item


def success(*rows):
    return {"words_result": list(rows), "words_result_num": len(rows)}


@contextmanager
def network(ocr_responses, *, oauth=None):
    requests = []
    responses = iter(ocr_responses)

    def handler(request):
        requests.append(request)
        if request.url.path == "/oauth/2.0/token":
            payload = oauth if oauth is not None else {"access_token": TOKEN, "expires_in": 3600}
        else:
            payload = next(responses)
        if isinstance(payload, Exception):
            raise payload
        if isinstance(payload, httpx.Response):
            return payload
        if isinstance(payload, tuple):
            return httpx.Response(payload[0], json=payload[1])
        return httpx.Response(200, json=payload)

    def factory(**kwargs):
        return REAL_CLIENT(transport=httpx.MockTransport(handler), verify=False, trust_env=False, **kwargs)

    with patch("jobprep.ocr.httpx.AsyncClient", side_effect=factory):
        yield requests


class OCRTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.image = self.root / "image.png"
        Image.new("RGB", (100, 100), "white").save(self.image)
        self.env = patch.dict("os.environ", {"BAIDU_OCR_API_KEY": "", "BAIDU_OCR_SECRET_KEY": ""})
        self.env.start()
        self.addCleanup(self.env.stop)

    def client(self, **kwargs):
        return BaiduOCR(self.root / "ocr-cache", api_key=API_KEY, secret_key=SECRET_KEY, **kwargs)

    async def test_missing_credentials_no_http_or_reservations(self):
        client = BaiduOCR(self.root / "ocr-cache")
        with network([]) as requests:
            result = await client.recognize(self.image)
        self.assertEqual(result["status"], "pending_credentials")
        self.assertEqual(result["provider"], "baidu")
        self.assertEqual(result["lines"], [])
        self.assertEqual(requests, [])
        self.assertEqual(client.status(), {"credentials_configured": False, "calls_used": 0, "max_calls": 50})
        json.dumps(result, allow_nan=False)

    async def test_env_defaults_and_explicit_empty_override(self):
        with patch.dict("os.environ", {"BAIDU_OCR_API_KEY": API_KEY, "BAIDU_OCR_SECRET_KEY": SECRET_KEY}):
            self.assertTrue(BaiduOCR(self.root / "env").credentials_configured)
            self.assertFalse(BaiduOCR(self.root / "empty", api_key="").credentials_configured)

    async def test_oauth_form_local_upload_and_durable_cache(self):
        client = self.client(max_calls=2)
        with network([success(row())]) as requests:
            first = await client.recognize(self.image)
        self.assertEqual(first["status"], "ok")
        self.assertEqual(first["model"], "general")
        self.assertEqual(first["lines"][0]["box"], [5, 10, 40, 20])
        self.assertEqual(first["calls_used"], 1)
        self.assertFalse(first["cache_hit"])
        self.assertEqual(len(requests), 2)
        self.assertTrue(all(request.method == "POST" for request in requests))
        self.assertEqual(requests[0].url.params["grant_type"], "client_credentials")
        self.assertEqual(requests[0].url.params["client_id"], API_KEY)
        self.assertEqual(requests[0].url.params["client_secret"], SECRET_KEY)
        self.assertEqual(requests[1].url.path, "/rest/2.0/ocr/v1/general")
        fields = parse_qs(requests[1].content.decode())
        self.assertNotIn("url", fields)
        self.assertEqual(fields["probability"], ["true"])
        with Image.open(BytesIO(base64.b64decode(fields["image"][0]))) as uploaded:
            self.assertEqual(uploaded.size, (100, 100))
        # A new instance can read successful cache without credentials or budget.
        reopened = BaiduOCR(self.root / "ocr-cache", max_calls=0)
        with network([]) as requests:
            second = await reopened.recognize(self.image)
        self.assertEqual(second["status"], "ok")
        self.assertTrue(second["cache_hit"])
        self.assertEqual(second["calls_used"], 1)
        self.assertEqual(requests, [])
        persisted = (self.root / "ocr-cache" / "baidu_ocr.sqlite3").read_bytes()
        for secret in (API_KEY, SECRET_KEY, TOKEN):
            self.assertNotIn(secret.encode(), persisted)

    async def test_general_basic_path_no_coordinates_and_cache_isolated(self):
        basic_rows = {"words_result": [
            {"words": "first line", "probability": {"average": 0.9}},
            {"words": "second line"},
        ]}
        with network([basic_rows]) as requests:
            basic = await self.client(model="general_basic").recognize(self.image)
        self.assertEqual(basic["status"], "ok")
        self.assertEqual(basic["model"], "general_basic")
        self.assertEqual(basic["text"], "first line\nsecond line")
        self.assertTrue(all("box" not in line and "polygon" not in line
                            for line in basic["lines"]))
        self.assertEqual(requests[-1].url.path, "/rest/2.0/ocr/v1/general_basic")
        fields = parse_qs(requests[-1].content.decode())
        self.assertNotIn("vertexes_location", fields)

        with network([success(row("position result"))]) as requests:
            positioned = await self.client(model="general").recognize(self.image)
        self.assertEqual(positioned["text"], "position result")
        self.assertFalse(positioned["cache_hit"])
        self.assertEqual(positioned["calls_used"], 2)
        self.assertEqual(requests[-1].url.path, "/rest/2.0/ocr/v1/general")

        with network([]) as requests:
            cached = await self.client(model="general_basic").recognize(self.image)
        self.assertTrue(cached["cache_hit"])
        self.assertEqual(requests, [])

    async def test_legacy_general_complete_cache_remains_readable(self):
        client = self.client(model="general")
        with network([success(row("legacy general"))]):
            created = await client.recognize(self.image)
        digest = __import__("hashlib").sha256(self.image.read_bytes()).hexdigest()
        new_key = f"result:{PREPROCESSING_VERSION}:{digest}:base=general:accurate=0"
        old_key = f"result:{PREPROCESSING_VERSION}:{digest}:general:accurate=0"
        with closing(sqlite3.connect(client._db_path)) as db, db:
            db.execute("UPDATE cache SET key=? WHERE key=?", (old_key, new_key))
            db.execute("DELETE FROM cache WHERE key LIKE 'slice:%'")
        with network([]) as requests:
            cached = await self.client(model="general").recognize(self.image)
        self.assertEqual(cached["text"], created["text"])
        self.assertTrue(cached["cache_hit"])
        self.assertEqual(requests, [])

    async def test_general_basic_tiled_order_is_safe_and_explicit(self):
        Image.new("RGB", (100, 4200), "white").save(self.image)
        payloads = [success({"words": "top"}), success({"words": "bottom"})]
        with network(payloads):
            result = await self.client(model="general_basic").recognize(self.image)
        self.assertEqual(result["text"], "top\nbottom")
        self.assertTrue(any("no coordinates" in note for note in result["warnings"]))

    def test_model_validation(self):
        with self.assertRaises(ValueError):
            self.client(model="unsupported")
        with self.assertRaises(ValueError):
            self.client(model="general_basic", high_accuracy_retry=True)

    async def test_long_image_offsets_overlap_dedup_and_repeated_text(self):
        Image.new("RGB", (100, 8200), "white").save(self.image)
        payloads = [success(row("overlap", y=4000), row("repeated", y=100)),
                    success(row("overlap", y=4), row("next overlap", y=4000), row("repeated", y=200)),
                    success(row("next overlap", y=4), row("last", y=100))]
        with network(payloads) as requests:
            result = await self.client().recognize(self.image)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["calls_used"], 3)
        self.assertEqual(len(result["lines"]), 5)
        self.assertEqual(result["text"].count("repeated"), 2)
        self.assertEqual(result["text"].splitlines().count("overlap"), 1)
        self.assertEqual(result["text"].splitlines().count("next overlap"), 1)
        last = next(line for line in result["lines"] if line["text"] == "last")
        self.assertEqual(last["box"][1], 8092)
        self.assertEqual(last["slice_index"], 2)
        sizes = []
        for request in requests[1:]:
            encoded = parse_qs(request.content.decode())["image"][0]
            with Image.open(BytesIO(base64.b64decode(encoded))) as uploaded:
                sizes.append(uploaded.size)
        self.assertEqual(sizes, [(100, 4096), (100, 4096), (100, 208)])

    async def test_horizontal_offsets_polygon_and_missing_confidence(self):
        Image.new("RGB", (4200, 100), "white").save(self.image)
        polygon = {"words": "wide", "vertexes_location": [
            {"x": 4, "y": 1}, {"x": 44, "y": 1}, {"x": 44, "y": 21}, {"x": 4, "y": 21}]}
        with network([success(), success(polygon)]):
            result = await self.client(high_accuracy_retry=False).recognize(self.image)
        self.assertEqual(result["lines"][0]["polygon"][0], [4000, 1])
        self.assertNotIn("confidence", result["lines"][0])
        self.assertTrue(any("confidence" in message for message in result["warnings"]))

    async def test_encoded_byte_limit_splits_without_resizing(self):
        rng = random.Random(7)
        image = Image.frombytes("RGB", (1100, 1100), rng.randbytes(1100 * 1100 * 3))
        buffer = BytesIO()
        image.save(buffer, format="PNG")
        tiles, notes = _image_slices(buffer.getvalue())
        self.assertGreater(len(tiles), 1)
        self.assertTrue(notes)
        for tile in tiles:
            form = {"image": tile.image, "probability": "true", "vertexes_location": "true"}
            self.assertLessEqual(len(urlencode(form).encode()), MAX_FORM_BYTES)
            with Image.open(BytesIO(base64.b64decode(tile.image))) as crop:
                self.assertEqual(crop.size, (tile.width, tile.height))
                self.assertEqual(crop.getpixel((0, 0)), image.getpixel((tile.x, tile.y)))

    async def test_small_image_padding(self):
        Image.new("RGBA", (2, 8), (0, 0, 0, 0)).save(self.image)
        tiles, notes = _image_slices(self.image.read_bytes())
        self.assertTrue(any("padded" in message for message in notes))
        with Image.open(BytesIO(base64.b64decode(tiles[0].image))) as crop:
            self.assertEqual(crop.size, (15, 15))
            self.assertEqual(crop.getpixel((0, 0)), (255, 255, 255))

    async def test_oauth_failure_not_charged_and_sanitized(self):
        client = self.client()
        with network([], oauth={"error": "invalid_client", "error_description": SECRET_KEY + API_KEY}) as requests:
            result = await client.recognize(self.image)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["calls_used"], 0)
        self.assertEqual(len(requests), 1)
        self.assertNotIn(SECRET_KEY, json.dumps(result))

    async def test_oauth_transient_bounded_no_ocr_budget(self):
        with network([], oauth=(503, {"error": SECRET_KEY})) as requests:
            result = await self.client().recognize(self.image)
        self.assertEqual(len(requests), 3)
        self.assertEqual(result["calls_used"], 0)
        self.assertEqual(result["status"], "error")

    async def test_transient_ocr_retries_ceiling_and_restart(self):
        client = self.client(max_calls=2)
        with network([{"error_code": 18, "error_msg": TOKEN}, (503, {"error": SECRET_KEY})]) as requests:
            result = await client.recognize(self.image)
        self.assertEqual(result["status"], "budget_exhausted")
        self.assertEqual(result["calls_used"], 2)
        self.assertEqual(len(requests), 3)
        reopened = self.client(max_calls=50)
        self.assertEqual(reopened.status()["max_calls"], 2)
        with network([]) as requests:
            retry = await reopened.recognize(self.image)
        self.assertEqual(retry["status"], "budget_exhausted")
        self.assertEqual(requests, [])
        for secret in (API_KEY, SECRET_KEY, TOKEN):
            self.assertNotIn(secret, json.dumps(result))

    async def test_transient_attempts_bounded_even_with_large_budget(self):
        with network([(503, {}), (503, {}), (503, {})]) as requests:
            result = await self.client().recognize(self.image)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["calls_used"], 3)
        self.assertEqual(len(requests), 4)

    async def test_timeout_retried_counted_success_cached(self):
        with network([httpx.ReadTimeout(TOKEN), success(row())]):
            result = await self.client().recognize(self.image)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["calls_used"], 2)
        self.assertNotIn(TOKEN, json.dumps(result))

    async def test_invalid_json_permanent_provider_errors_not_retried(self):
        for response in ({"error_code": 216201, "error_msg": SECRET_KEY}, [], {"unexpected": TOKEN},
                         {"words_result": [{"words": "no coordinates"}]},
                         httpx.Response(200, text=TOKEN), {"_failure": SECRET_KEY}):
            with self.subTest(response=response):
                with network([response]) as requests:
                    result = await self.client().recognize(self.image)
                self.assertEqual(result["status"], "error")
                self.assertEqual(len(requests), 2)
                self.assertNotIn(SECRET_KEY, json.dumps(result))
        self.assertEqual(self.client().calls_used, 6)

    async def test_token_refresh_counts_both_ocr_attempts(self):
        with network([{"error_code": 110, "error_msg": TOKEN}, success(row())]) as requests:
            result = await self.client().recognize(self.image)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["calls_used"], 2)
        self.assertEqual(sum(request.url.path.endswith("/token") for request in requests), 2)

    async def test_high_accuracy_opt_in_improvement_and_config_cache_isolation(self):
        with network([success(row(confidence=0.3))]):
            standard = await self.client().recognize(self.image)
        self.assertEqual(standard["model"], "general")
        with network([success(row("better job", confidence=0.96))]) as requests:
            retried = await self.client(high_accuracy_retry=True).recognize(self.image)
        self.assertEqual(retried["text"], "better job")
        self.assertEqual(retried["model"], "general+accurate")
        self.assertEqual(retried["calls_used"], 2)
        self.assertEqual(requests[-1].url.path, "/rest/2.0/ocr/v1/accurate")
        with network([]) as requests:
            cached = await self.client(high_accuracy_retry=True).recognize(self.image)
        self.assertTrue(cached["cache_hit"])
        self.assertEqual(requests, [])

    async def test_high_accuracy_lower_score_retains_standard(self):
        with network([success(row("standard", confidence=0.5)), success(row("worse", confidence=0.2))]):
            result = await self.client(high_accuracy_retry=True).recognize(self.image)
        self.assertEqual(result["text"], "standard")
        self.assertEqual(result["calls_used"], 2)
        self.assertTrue(any("Retained" in note for note in result["warnings"]))

    async def test_high_accuracy_empty_and_unknown_confidence(self):
        with network([success(), success(row("found"))]):
            result = await self.client(high_accuracy_retry=True).recognize(self.image)
        self.assertEqual(result["text"], "found")
        Image.new("RGB", (100, 100), "red").save(self.image)
        with network([success(row(confidence=None))]) as requests:
            result = await self.client(high_accuracy_retry=True).recognize(self.image)
        self.assertEqual(result["model"], "general")
        self.assertEqual(len(requests), 2)

    async def test_optional_high_accuracy_budget_returns_partial(self):
        with network([success(row(confidence=0.5))]):
            result = await self.client(max_calls=1, high_accuracy_retry=True).recognize(self.image)
        self.assertEqual(result["status"], "budget_exhausted")
        self.assertEqual(result["text"], "job")
        self.assertTrue(any("Partial" in note for note in result["warnings"]))

    async def test_partial_slices_reused_after_failure(self):
        Image.new("RGB", (100, 4200), "white").save(self.image)
        client = self.client()
        with network([success(row("first")), {"error_code": 216201}]):
            partial = await client.recognize(self.image)
        self.assertEqual(partial["status"], "error")
        self.assertEqual(partial["text"], "first")
        with network([success(row("second"))]) as requests:
            complete = await self.client().recognize(self.image)
        self.assertEqual(complete["status"], "ok")
        self.assertEqual(complete["calls_used"], 3)
        self.assertEqual(len(requests), 2)

    async def test_parallel_instances_atomic_budget(self):
        first = self.client(max_calls=1)
        second = self.client(max_calls=1)
        other = self.root / "other.png"
        Image.new("RGB", (100, 100), "blue").save(other)
        with network([success(row())]):
            results = await asyncio.gather(first.recognize(self.image), second.recognize(other))
        self.assertEqual(sorted(result["status"] for result in results), ["budget_exhausted", "ok"])
        self.assertEqual(first.calls_used, 1)
        self.assertEqual(second.calls_used, 1)

    async def test_same_instance_serialized_cache_prevents_duplicate_call(self):
        client = self.client()
        with network([success(row())]) as requests:
            results = await asyncio.gather(client.recognize(self.image), client.recognize(self.image))
        self.assertEqual(len(requests), 2)
        self.assertEqual([result["cache_hit"] for result in results], [False, True])

    async def test_cancel_after_reservation_never_refunds_budget(self):
        client = self.client(max_calls=1)
        reserved = asyncio.Event()

        async def post(http, path, **kwargs):
            if path.endswith("/token"):
                return {"access_token": TOKEN, "expires_in": 3600}, False
            reserved.set()
            await asyncio.Future()

        with network([]), patch.object(client, "_post", side_effect=post):
            task = asyncio.create_task(client.recognize(self.image))
            await asyncio.wait_for(reserved.wait(), timeout=2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        reopened = self.client(max_calls=50)
        self.assertEqual(reopened.calls_used, 1)
        with network([]) as requests:
            result = await reopened.recognize(self.image)
        self.assertEqual(result["status"], "budget_exhausted")
        self.assertEqual(requests, [])

    async def test_hash_preprocessing_cache_isolation_and_main_database_untouched(self):
        main_db = self.root / "main.sqlite3"
        main_db.write_bytes(b"unrelated database fixture")
        client = self.client()
        with network([success(row("white")), success(row("red")), success(row("version"))]):
            white = await client.recognize(self.image)
            Image.new("RGB", (100, 100), "red").save(self.image)
            red = await client.recognize(self.image)
            with patch("jobprep.ocr.PREPROCESSING_VERSION", PREPROCESSING_VERSION + "-new"):
                version = await client.recognize(self.image)
        self.assertEqual([white["text"], red["text"], version["text"]], ["white", "red", "version"])
        self.assertEqual(client.calls_used, 3)
        self.assertEqual(main_db.read_bytes(), b"unrelated database fixture")

    async def test_invalid_image_no_billable_calls(self):
        self.image.write_bytes(b"not an image")
        with network([]) as requests:
            result = await self.client().recognize(self.image)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["calls_used"], 0)
        self.assertEqual(requests, [])

    async def test_zero_budget_no_oauth(self):
        with network([]) as requests:
            result = await self.client(max_calls=0).recognize(self.image)
        self.assertEqual(result["status"], "budget_exhausted")
        self.assertEqual(requests, [])

    async def test_explicit_budget_raise_retains_attempts_and_survives_reopen(self):
        client = self.client(max_calls=1)
        self.assertFalse(client.high_accuracy_retry)
        with network([success(row("first"))]):
            await client.recognize(self.image)
        Image.new("RGB", (100, 100), "green").save(self.image)
        with network([]):
            exhausted = await client.recognize(self.image)
        self.assertEqual(exhausted["status"], "budget_exhausted")
        client.set_budget(3)
        self.assertEqual(client.status()["calls_used"], 1)
        self.assertEqual(client.status()["max_calls"], 3)
        reopened = self.client()
        self.assertEqual(reopened.status()["max_calls"], 3)
        with network([success(row("second"))]):
            result = await reopened.recognize(self.image)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["calls_used"], 2)
        reopened.set_budget(0)
        self.assertEqual(client.status()["max_calls"], 0)
        self.assertEqual(client.calls_used, 2)
        for invalid in (-1, True, 1.5, "4"):
            with self.assertRaises(ValueError):
                client.set_budget(invalid)
        self.assertEqual(client.calls_used, 2)

    async def test_corrupt_cache_fails_without_resetting_budget(self):
        client = self.client()
        with network([success(row())]):
            await client.recognize(self.image)
        with closing(sqlite3.connect(client._db_path)) as db, db:
            db.execute("UPDATE cache SET value='invalid' WHERE key LIKE 'result:%'")
        with network([]) as requests:
            result = await client.recognize(self.image)
        self.assertEqual(result["status"], "error")
        self.assertEqual(client.calls_used, 1)
        self.assertEqual(requests, [])


if __name__ == "__main__":
    unittest.main()
