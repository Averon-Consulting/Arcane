"""Local review dashboard for the advisor shortlist.

Binds to loopback only. The lead list is names, phones, and addresses, so this
process must not be published to GitHub Pages or any other public host.
"""

from __future__ import annotations

import json
import os
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv

from pipeline.load.upsert import connect

PAGE_PATH = Path(__file__).with_name("dashboard.html")
LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})
VERDICTS = frozenset({"worth_call", "hospital_employee", "wrong_size", "not_buyer"})
LEAD_KINDS = frozenset({"group_practice", "professional_corporation"})
DEFAULT_PORT = 8765

LEAD_SQL = """
SELECT
    segment_id, org_key, contact_key, full_name, credentials, title, specialty,
    phone, fax, phone_shared_count, phone_kind, practice, address_line1, city,
    licensee_count, practice_type,
    fit_score, lead_kind, verdict, note, reviewed_by, reviewed_at
FROM advisor_shortlist
WHERE {where}
ORDER BY fit_score DESC, licensee_count DESC, full_name
LIMIT %s OFFSET %s
"""


def bind_host(host: str) -> str:
    if host not in LOOPBACK:
        raise SystemExit(
            "The dashboard listens on 127.0.0.1 only. "
            "Do not publish this lead list."
        )
    if host == "localhost":
        return "127.0.0.1"
    return host


def _clean(value: str | None) -> str:
    return (value or "").strip()


def leads_where(filters: dict[str, str]) -> tuple[str, list[Any]]:
    clauses = ["TRUE"]
    params: list[Any] = []
    city = _clean(filters.get("city"))
    if city:
        clauses.append("city = %s")
        params.append(city)
    kind = _clean(filters.get("lead_kind"))
    if kind:
        if kind not in LEAD_KINDS:
            raise ValueError("Unknown lead_kind")
        clauses.append("lead_kind = %s")
        params.append(kind)
    specialty = _clean(filters.get("specialty"))
    if specialty:
        clauses.append("specialty = %s")
        params.append(specialty)
    verdict = _clean(filters.get("verdict"))
    if verdict == "unreviewed":
        clauses.append("verdict IS NULL")
    elif verdict:
        if verdict not in VERDICTS:
            raise ValueError("Unknown verdict")
        clauses.append("verdict = %s")
        params.append(verdict)
    query = _clean(filters.get("q"))
    if query:
        clauses.append(
            "(full_name ILIKE %s OR practice ILIKE %s OR coalesce(city, '') ILIKE %s)"
        )
        like = f"%{query}%"
        params.extend([like, like, like])
    min_score = _clean(filters.get("min_score"))
    if min_score:
        clauses.append("fit_score >= %s")
        params.append(Decimal(min_score))
    return " AND ".join(clauses), params


def page_bounds(filters: dict[str, str]) -> tuple[int, int]:
    try:
        limit = int(filters.get("limit") or "50")
    except ValueError:
        limit = 50
    try:
        offset = int(filters.get("offset") or "0")
    except ValueError:
        offset = 0
    return max(1, min(limit, 200)), max(0, offset)


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value == int(value) else float(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _rows(conn: Any, sql: str, params: list[Any]) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        columns = [col.name for col in cur.description]
        return [
            {key: _jsonable(val) for key, val in zip(columns, row)}
            for row in cur.fetchall()
        ]


def summary(conn: Any) -> dict[str, int]:
    sql = """
        SELECT
            count(*) AS total,
            count(*) FILTER (WHERE verdict IS NULL) AS unreviewed,
            count(*) FILTER (WHERE verdict = 'worth_call') AS worth_call,
            count(*) FILTER (WHERE verdict = 'hospital_employee') AS hospital_employee,
            count(*) FILTER (WHERE verdict = 'wrong_size') AS wrong_size,
            count(*) FILTER (WHERE verdict = 'not_buyer') AS not_buyer,
            count(*) FILTER (WHERE lead_kind = 'group_practice') AS group_practice,
            count(*) FILTER (WHERE lead_kind = 'professional_corporation') AS professional_corporation
        FROM advisor_shortlist
    """
    return {key: int(val) for key, val in _rows(conn, sql, [])[0].items()}


def options(conn: Any) -> dict[str, list[str]]:
    cities = _rows(
        conn,
        """
        SELECT city FROM advisor_shortlist
        WHERE city IS NOT NULL AND btrim(city) <> ''
        GROUP BY city ORDER BY count(*) DESC, city
        """,
        [],
    )
    specialties = _rows(
        conn,
        """
        SELECT specialty FROM advisor_shortlist
        WHERE specialty IS NOT NULL AND btrim(specialty) <> ''
        GROUP BY specialty ORDER BY count(*) DESC, specialty
        LIMIT 40
        """,
        [],
    )
    return {
        "cities": [row["city"] for row in cities],
        "specialties": [row["specialty"] for row in specialties],
    }


def leads(conn: Any, filters: dict[str, str]) -> dict[str, Any]:
    where, params = leads_where(filters)
    limit, offset = page_bounds(filters)
    rows = _rows(conn, LEAD_SQL.format(where=where), [*params, limit, offset])
    total = _rows(
        conn,
        f"SELECT count(*) AS n FROM advisor_shortlist WHERE {where}",
        params,
    )[0]["n"]
    return {"rows": rows, "total": int(total), "limit": limit, "offset": offset}


def save_review(conn: Any, body: dict[str, Any]) -> None:
    segment_id = _clean(body.get("segment_id"))
    org_key = _clean(body.get("org_key"))
    contact_key = _clean(body.get("contact_key"))
    verdict = _clean(body.get("verdict"))
    if not segment_id or not org_key or not contact_key:
        raise ValueError("segment_id, org_key, and contact_key are required")
    if verdict not in VERDICTS:
        raise ValueError("Unknown verdict")
    note = _clean(body.get("note")) or None
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO lead_reviews (
                segment_id, org_key, contact_key, verdict, note, reviewed_by
            )
            VALUES (%s, %s, %s, %s, %s, 'advisor')
            ON CONFLICT (segment_id, org_key, contact_key) DO UPDATE SET
                verdict = EXCLUDED.verdict,
                note = EXCLUDED.note,
                reviewed_by = 'advisor',
                updated_at = now()
            """,
            (segment_id, org_key, contact_key, verdict, note),
        )
    conn.commit()


def clear_review(conn: Any, body: dict[str, Any]) -> None:
    segment_id = _clean(body.get("segment_id"))
    org_key = _clean(body.get("org_key"))
    contact_key = _clean(body.get("contact_key"))
    if not segment_id or not org_key or not contact_key:
        raise ValueError("segment_id, org_key, and contact_key are required")
    with conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM lead_reviews
            WHERE segment_id = %s AND org_key = %s AND contact_key = %s
            """,
            (segment_id, org_key, contact_key),
        )
    conn.commit()


def _first(params: dict[str, list[str]], key: str) -> str:
    values = params.get(key) or [""]
    return values[0]


class DashboardHandler(BaseHTTPRequestHandler):
    database_url = ""

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        filters = {key: _first(parse_qs(parsed.query), key) for key in parse_qs(parsed.query)}
        try:
            if parsed.path == "/":
                self._bytes(200, "text/html; charset=utf-8", PAGE_PATH.read_bytes())
                return
            with connect(self.database_url) as conn:
                if parsed.path == "/api/summary":
                    payload = summary(conn)
                elif parsed.path == "/api/options":
                    payload = options(conn)
                elif parsed.path == "/api/leads":
                    payload = leads(conn, filters)
                else:
                    self._bytes(404, "application/json", b'{"error":"not found"}')
                    return
            self._bytes(200, "application/json", json.dumps(payload).encode())
        except ValueError as exc:
            self._bytes(400, "application/json", json.dumps({"error": str(exc)}).encode())
        except Exception as exc:  # local-only; surface the database error
            self._bytes(500, "application/json", json.dumps({"error": str(exc)}).encode())

    def do_POST(self) -> None:  # noqa: N802
        self._write("save")

    def do_DELETE(self) -> None:  # noqa: N802
        self._write("clear")

    def _write(self, action: str) -> None:
        if urlparse(self.path).path != "/api/reviews":
            self._bytes(404, "application/json", b'{"error":"not found"}')
            return
        length = int(self.headers.get("Content-Length") or "0")
        if length > 10_000:
            self._bytes(400, "application/json", b'{"error":"body too large"}')
            return
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
            with connect(self.database_url) as conn:
                if action == "save":
                    save_review(conn, body)
                else:
                    clear_review(conn, body)
            self._bytes(200, "application/json", b'{"ok":true}')
        except ValueError as exc:
            self._bytes(400, "application/json", json.dumps({"error": str(exc)}).encode())
        except Exception as exc:
            self._bytes(500, "application/json", json.dumps({"error": str(exc)}).encode())

    def _bytes(self, status: int, content_type: str, payload: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt: str, *args: Any) -> None:
        return


def serve(database_url: str, host: str = "127.0.0.1", port: int = DEFAULT_PORT) -> None:
    host = bind_host(host)
    DashboardHandler.database_url = database_url
    server = ThreadingHTTPServer((host, port), DashboardHandler)
    print(f"Advisor dashboard at http://{host}:{port}")
    print("Loopback only. Leave this running, and do not publish the lead list.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        server.server_close()


def main() -> None:
    load_dotenv()
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise SystemExit("DATABASE_URL is required")
    port = int(os.environ.get("DASHBOARD_PORT") or DEFAULT_PORT)
    serve(database_url, port=port)


if __name__ == "__main__":
    main()
