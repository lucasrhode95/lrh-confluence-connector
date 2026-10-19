"""Offline CQL-timezone calibration checks using a synthetic HTTP transport."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re
import unittest

import httpx

from confluence_connector.remote.confluence import ConfluenceApi
from confluence_connector.remote.transport import ConfluenceTransport
from confluence_connector.service.timezone_calibration import calibrate_cql_timezone
from fakes.credential_source import FakeCredentialSource
from fakes.settings import settings


_CUTOFF = re.compile(r"lastmodified >= '([^']+)'")
_REFERENCE_UTC = datetime(2026, 10, 19, 12, 34, 56, tzinfo=timezone.utc)
_PAGE_ID = "24680"


class TimezoneCalibrationChecks(unittest.IsolatedAsyncioTestCase):
    async def test_binary_search_measures_supported_effective_offsets(self) -> None:
        for expected_offset in (0, 330, -180):
            with self.subTest(offset_minutes=expected_offset):
                queries: list[str] = []
                ref_minute = _REFERENCE_UTC.replace(second=0, microsecond=0).replace(tzinfo=None)
                effective_cutoff = ref_minute + timedelta(minutes=expected_offset)

                def respond(request: httpx.Request) -> httpx.Response:
                    cql = request.url.params["cql"]
                    queries.append(cql)
                    if "lastmodified >=" not in cql:
                        rows = [_page_row()]
                    else:
                        match = _CUTOFF.search(cql)
                        self.assertIsNotNone(match)
                        cutoff = datetime.strptime(match.group(1), "%Y/%m/%d %H:%M")  # type: ignore[union-attr]
                        rows = [_page_row()] if cutoff <= effective_cutoff else []
                    return httpx.Response(
                        200,
                        json={"start": 0, "size": len(rows), "results": rows},
                    )

                async with httpx.AsyncClient(
                    base_url="https://confluence.example.test/wiki/",
                    transport=httpx.MockTransport(respond),
                ) as client:
                    transport = ConfluenceTransport(
                        settings(), FakeCredentialSource(), client=client  # type: ignore[arg-type]
                    )
                    api = ConfluenceApi(settings(), transport)
                    result = await calibrate_cql_timezone(api, "sample-user")

                self.assertEqual(result.offset_minutes, expected_offset)
                self.assertEqual(result.reference_utc, _REFERENCE_UTC)
                self.assertGreaterEqual(len(queries), 10)
                self.assertLessEqual(len(queries), 12)
                self.assertTrue(queries[0].startswith("type = page ORDER BY lastmodified DESC"))
                self.assertTrue(all(query.startswith(f"id = {_PAGE_ID} AND") for query in queries[1:]))


def _page_row() -> dict[str, object]:
    return {
        "content": {
            "id": _PAGE_ID,
            "type": "page",
            "title": "Synthetic calibration page",
            "version": {"when": _REFERENCE_UTC.isoformat().replace("+00:00", "Z")},
        }
    }


if __name__ == "__main__":
    unittest.main()
