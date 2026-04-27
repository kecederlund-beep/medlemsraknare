#!/usr/bin/env python3
"""
Realtime member counter + banner for iframe.

- Upload CSV (GDPR-safe) with only Betaldatum
- Counts rows where Betaldatum >= cutoff date
- Serves JSON at /member-count
- Serves banner HTML at /banner
- Serves upload page at /upload

No external dependencies.
"""

from __future__ import annotations

import csv
import json
import os
import re
import ssl
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse, parse_qs
from urllib.request import Request, urlopen


# --------------------
# Config (env vars)
# --------------------
PORT = int(os.getenv("PORT", "8088"))
CUTOFF_DATE = os.getenv("CUTOFF_DATE", "2026-04-01")  # YYYY-MM-DD
LAUNCH_ISO = os.getenv("LAUNCH_ISO", "2026-05-01T00:00:00+02:00")
REMINDERS_PATH = os.getenv(
    "REMINDERS_PATH",
    os.path.join(os.path.dirname(__file__), "reminders.csv"),
)
ITARGET_API_BASE = os.getenv("ITARGET_API_BASE", "https://app.itarget.se/api").rstrip("/")
ITARGET_TOKEN = os.getenv("ITARGET_TOKEN", "")
ITARGET_CLIENT_ID = os.getenv("ITARGET_CLIENT_ID", "")
ITARGET_POLL_SECONDS = int(os.getenv("ITARGET_POLL_SECONDS", "10"))
ITARGET_SKIP_SSL_VERIFY = os.getenv("ITARGET_SKIP_SSL_VERIFY", "0") == "1"
ITARGET_COUNT_KEY = os.getenv("ITARGET_COUNT_KEY", "").strip()
ITARGET_COUNT_ENDPOINT_TEMPLATE = os.getenv(
    "ITARGET_COUNT_ENDPOINT_TEMPLATE", "/clients/{client_id}/memberships"
).strip()
# Default to active memberships since the counter should show active members.
ITARGET_MEMBERSHIPS_QUERY = os.getenv("ITARGET_MEMBERSHIPS_QUERY", "includeCount=true&status=active").strip()
ITARGET_SOURCE = os.getenv("ITARGET_SOURCE", "").strip().lower()
ITARGET_INTERNAL_ENDPOINT = os.getenv("ITARGET_INTERNAL_ENDPOINT", "").strip()
ITARGET_INTERNAL_METHOD = os.getenv("ITARGET_INTERNAL_METHOD", "GET").strip().upper()
ITARGET_INTERNAL_HEADERS = os.getenv("ITARGET_INTERNAL_HEADERS", "").strip()
ITARGET_INTERNAL_BODY = os.getenv("ITARGET_INTERNAL_BODY", "").strip()
ITARGET_INTERNAL_EXPECTED_STATUS = os.getenv("ITARGET_INTERNAL_EXPECTED_STATUS", "active").strip().lower()
FORCE_LIVE = os.getenv("FORCE_LIVE", "0") == "1"
INTERNAL_COUNT_MODE = bool(ITARGET_INTERNAL_ENDPOINT)
API_COUNT_MODE = INTERNAL_COUNT_MODE or bool(ITARGET_TOKEN and ITARGET_CLIENT_ID)


# --------------------
# State
# --------------------
state = {
    "count": 0,
    "updated_at": None,
    "last_error": None,
    "last_upload": None,
}


# --------------------
# Helpers
# --------------------
def parse_dt(value: str) -> datetime | None:
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None

    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def launch_dt() -> datetime:
    try:
        return datetime.fromisoformat(LAUNCH_ISO)
    except ValueError:
        # fallback to local time without timezone
        return datetime.strptime(LAUNCH_ISO, "%Y-%m-%d %H:%M:%S")


def is_launch_live() -> bool:
    target = launch_dt()
    if target.tzinfo is None:
        return datetime.now() >= target
    return datetime.now(target.tzinfo) >= target


def clean_email(value: str) -> str:
    s = (value or "").strip().lower()
    if not s or "@" not in s:
        return ""
    return s


def clean_phone(value: str) -> str:
    s = re.sub(r"[^0-9+]", "", value or "")
    digits = re.sub(r"[^0-9]", "", s)
    return s if len(digits) >= 7 else ""


def save_reminder(email: str, phone: str):
    os.makedirs(os.path.dirname(REMINDERS_PATH), exist_ok=True)
    is_new = not os.path.exists(REMINDERS_PATH)
    with open(REMINDERS_PATH, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if is_new:
            w.writerow(["created_at", "email", "phone"])
        w.writerow([datetime.now().isoformat(timespec="seconds"), email, phone])


def reminders_count() -> int:
    if not os.path.exists(REMINDERS_PATH):
        return 0
    try:
        with open(REMINDERS_PATH, newline="", encoding="utf-8") as f:
            r = csv.reader(f)
            rows = list(r)
        # subtract header if present
        return max(0, len(rows) - 1) if rows else 0
    except Exception:
        return 0


def _safe_int(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if value is None:
        return None
    s = str(value).strip().replace(" ", "").replace("\u00a0", "")
    if re.fullmatch(r"-?\d+", s):
        try:
            return int(s)
        except Exception:
            return None
    return None


def _get_path(payload, path):
    node = payload
    for key in path:
        if isinstance(node, dict) and key in node:
            node = node[key]
        else:
            return None
    return node


def _find_all_values(payload, wanted_key):
    found = []

    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if str(k) == wanted_key:
                    found.append(v)
                walk(v)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    return found


def _extract_members_index_count(payload):
    if not isinstance(payload, (dict, list)):
        return None, {"reason": "response-not-json-object"}

    if isinstance(payload, dict):
        message = str(payload.get("message") or "").strip().lower()
        if "unauthenticated" in message or "session" in message and "expired" in message:
            return None, {"reason": "session-expired", "message": payload.get("message")}

    status = None
    for path in (
        ("serverMemo", "data", "status"),
        ("effects", "serverMemo", "data", "status"),
        ("data", "status"),
    ):
        v = _get_path(payload, path)
        if isinstance(v, str) and v.strip():
            status = v.strip()
            break

    if status is None:
        status_candidates = [v for v in _find_all_values(payload, "status") if isinstance(v, str)]
        if status_candidates:
            status = status_candidates[0]

    if str(status or "").strip().lower() != ITARGET_INTERNAL_EXPECTED_STATUS:
        return None, {
            "reason": "status-not-active",
            "expected": ITARGET_INTERNAL_EXPECTED_STATUS,
            "actual": status,
        }

    # Prefer explicit numberOfContacts from the members-index-new response structure.
    for path in (
        ("numberOfContacts",),
        ("serverMemo", "data", "numberOfContacts"),
        ("effects", "serverMemo", "data", "numberOfContacts"),
        ("data", "numberOfContacts"),
    ):
        n = _safe_int(_get_path(payload, path))
        if n is not None:
            return n, {
                "source": "members-index-new",
                "status": status,
                "path": ".".join(path),
            }

    candidates = [_safe_int(v) for v in _find_all_values(payload, "numberOfContacts")]
    candidates = [v for v in candidates if v is not None]
    if candidates:
        return candidates[0], {
            "source": "members-index-new",
            "status": status,
            "path": "recursive:numberOfContacts",
        }

    return None, {
        "reason": "numberOfContacts-missing",
        "status": status,
    }


def _count_candidates_from_list(items):
    if not isinstance(items, list) or not items:
        return {}
    if not all(isinstance(x, dict) for x in items):
        return {}

    keys = set()
    for row in items:
        for k in row.keys():
            if "count" in str(k).lower():
                keys.add(k)

    candidates = {}
    for key in keys:
        total = 0.0
        valid = False
        for row in items:
            val = row.get(key)
            if val is None:
                continue
            try:
                total += float(val)
                valid = True
            except Exception:
                valid = False
                break
        if valid:
            candidates[key] = int(total)
    return candidates


def _extract_count_from_list(items, preferred_key=""):
    count_keys = (
        "count",
        "member_count",
        "members_count",
        "membership_count",
        "active_count",
        "total",
        "total_count",
    )
    if not isinstance(items, list) or not items:
        return None
    if not all(isinstance(x, dict) for x in items):
        return None

    candidates = _count_candidates_from_list(items)
    if not candidates:
        return None, {"mode": "no-count-key", "candidates": {}}

    if preferred_key and preferred_key in candidates:
        return candidates[preferred_key], {
            "key": preferred_key,
            "mode": "preferred-key",
            "candidates": candidates,
        }

    for key in (
        "active_contacts_count",
        "current_active_contacts_count",
        "active_members_count",
        "current_active_members_count",
        "active_count",
        "current_contacts_count",
        "current_members_count",
        "members_count",
        "contacts_count",
        "count",
        "total_count",
        "total",
    ):
        if key in candidates:
            return candidates[key], {"key": key, "mode": "priority-key", "candidates": candidates}

    def score_key(key):
        k = str(key).lower()
        score = 0
        if "active" in k:
            score += 100
        if "current" in k:
            score += 40
        if "member" in k or "contact" in k:
            score += 20
        return score

    best_key = max(candidates.keys(), key=lambda k: (score_key(k), candidates[k]))
    return candidates[best_key], {"key": best_key, "mode": "heuristic-key", "candidates": candidates}


def extract_api_count(payload, preferred_key=""):
    count_keys = (
        "count",
        "member_count",
        "members_count",
        "membership_count",
        "active_count",
        "total",
        "total_count",
    )

    if isinstance(payload, dict):
        for key in count_keys:
            if key in payload:
                try:
                    return int(float(payload.get(key) or 0)), {"source": f"dict.{key}"}
                except Exception:
                    pass
        for key in ("data", "items", "memberships", "result", "results"):
            if key in payload and isinstance(payload[key], list):
                n, list_meta = _extract_count_from_list(payload[key], preferred_key=preferred_key)
                if n is not None:
                    return n, {"source": f"list:{key}", "listMeta": list_meta}
        return None, {"source": "dict:unknown", "keys": list(payload.keys())[:20]}

    if isinstance(payload, list):
        n, list_meta = _extract_count_from_list(payload, preferred_key=preferred_key)
        if n is not None:
            return n, {"source": "list", "listMeta": list_meta}
        sample_keys = list(payload[0].keys())[:20] if payload and isinstance(payload[0], dict) else []
        return None, {"source": "list:unknown", "len": len(payload), "sampleKeys": sample_keys}

    return None, {"source": "unsupported", "type": str(type(payload))}


def _itarget_ssl_context():
    # Dev fallback for macOS Python cert issues.
    if ITARGET_SKIP_SSL_VERIFY:
        return ssl._create_unverified_context()
    return ssl.create_default_context()


def _internal_headers():
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    if ITARGET_TOKEN:
        headers["Authorization"] = f"Bearer {ITARGET_TOKEN}"
    if ITARGET_INTERNAL_HEADERS:
        extra = json.loads(ITARGET_INTERNAL_HEADERS)
        if not isinstance(extra, dict):
            raise ValueError("ITARGET_INTERNAL_HEADERS must be a JSON object")
        for k, v in extra.items():
            headers[str(k)] = str(v)
    return headers


def fetch_members_index_new_count():
    if not ITARGET_INTERNAL_ENDPOINT:
        raise ValueError("ITARGET_INTERNAL_ENDPOINT is missing")

    method = ITARGET_INTERNAL_METHOD if ITARGET_INTERNAL_METHOD in ("GET", "POST", "PATCH") else "GET"
    body = ITARGET_INTERNAL_BODY.encode("utf-8") if ITARGET_INTERNAL_BODY else None
    req = Request(
        ITARGET_INTERNAL_ENDPOINT,
        data=body,
        headers=_internal_headers(),
        method=method,
    )

    try:
        with urlopen(req, timeout=20, context=_itarget_ssl_context()) as resp:
            content_type = (resp.headers.get("Content-Type") or "").lower()
            raw = resp.read().decode("utf-8", errors="replace")
    except HTTPError as e:
        error_body = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else ""
        if e.code in (401, 403, 419):
            raise RuntimeError(f"session-expired (http {e.code})")
        if "unauthenticated" in error_body.lower() or "login" in error_body.lower():
            raise RuntimeError(f"session-expired (http {e.code})")
        raise

    if "json" not in content_type and raw.lstrip().startswith("<"):
        if "login" in raw.lower():
            raise RuntimeError("session-expired (redirected to login)")
        raise RuntimeError("members-index-new response format changed (expected JSON)")

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        if "unauthenticated" in raw.lower() or "login" in raw.lower():
            raise RuntimeError("session-expired")
        raise RuntimeError("members-index-new response format changed (invalid JSON)")

    count, meta = _extract_members_index_count(payload)
    if count is None:
        reason = meta.get("reason") if isinstance(meta, dict) else "unknown"
        if reason == "status-not-active":
            raise RuntimeError(
                "members-index-new returned non-active status "
                f"(expected={meta.get('expected')} actual={meta.get('actual')})"
            )
        if reason == "session-expired":
            raise RuntimeError("session-expired")
        if reason == "numberOfContacts-missing":
            raise RuntimeError("members-index-new format changed: numberOfContacts missing")
        raise RuntimeError(f"members-index-new parse error: {json.dumps(meta, ensure_ascii=False)}")

    return count, meta, ITARGET_INTERNAL_ENDPOINT


def fetch_itarget_membership_count():
    query = ITARGET_MEMBERSHIPS_QUERY or "includeCount=true"
    path = ITARGET_COUNT_ENDPOINT_TEMPLATE.format(client_id=ITARGET_CLIENT_ID).lstrip("/")
    endpoint = f"{ITARGET_API_BASE}/{path}"
    if query:
        endpoint = f"{endpoint}?{query}"
    req = Request(
        endpoint,
        headers={
            "Authorization": f"Bearer {ITARGET_TOKEN}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    with urlopen(req, timeout=20, context=_itarget_ssl_context()) as resp:
        body = resp.read().decode("utf-8")
        payload = json.loads(body)
    count, meta = extract_api_count(payload, preferred_key=ITARGET_COUNT_KEY)
    return count, meta, endpoint


def fetch_active_members_count():
    source = ITARGET_SOURCE
    if not source:
        source = "members-index-new" if INTERNAL_COUNT_MODE else "memberships-api"

    if source in ("members-index-new", "internal"):
        return fetch_members_index_new_count()
    if source in ("memberships-api", "api"):
        return fetch_itarget_membership_count()
    raise ValueError(f"Unknown ITARGET_SOURCE '{source}'")


def poll_itarget_count_loop():
    while True:
        try:
            count, meta, endpoint = fetch_active_members_count()
            source = ITARGET_SOURCE or ("members-index-new" if INTERNAL_COUNT_MODE else "memberships-api")
            if count is None:
                state["last_error"] = (
                    "No usable active count found. "
                    f"meta={json.dumps(meta, ensure_ascii=False)}"
                )
            else:
                state["count"] = count
                state["updated_at"] = datetime.now().isoformat(timespec="seconds")
                state["last_error"] = None
                state["last_upload"] = {
                    "mode": source,
                    "endpoint": endpoint,
                    "meta": meta,
                }
        except (HTTPError, URLError, json.JSONDecodeError, TimeoutError) as e:
            state["last_error"] = f"API poll error: {e}"
        except Exception as e:
            state["last_error"] = f"Unexpected API poll error: {e}"
        time.sleep(max(5, ITARGET_POLL_SECONDS))


def _detect_delim(lines: list[str]) -> str:
    for line in lines:
        s = line.strip()
        if not s:
            continue
        counts = {
            ";": s.count(";"),
            ",": s.count(","),
            "\t": s.count("\t"),
        }
        return max(counts, key=counts.get)
    return ";"


def count_from_csv_text(text: str, cutoff: datetime):
    lines = text.splitlines()
    if not lines:
        return 0, {"reason": "empty"}

    delim = _detect_delim(lines)
    reader = csv.reader(lines, delimiter=delim)
    rows = [r for r in reader if r]
    if not rows:
        return 0, {"reason": "no_rows"}

    header_raw = [h.strip() for h in rows[0]]
    header_norm = [h.strip().casefold() for h in rows[0]]
    idx = None
    if "betaldatum" in header_norm:
        idx = header_norm.index("betaldatum")
        data_rows = rows[1:]
    else:
        # No header: assume each row is a date
        data_rows = rows

    n = 0
    parsed = 0
    bad = 0
    sample = None
    for r in data_rows:
        if idx is not None:
            if idx >= len(r):
                bad += 1
                continue
            cell = r[idx]
        else:
            cell = r[0] if r else ""

        if sample is None and cell:
            sample = str(cell).strip()

        dt = parse_dt(cell)
        if dt is None:
            bad += 1
            continue
        parsed += 1
        if dt >= cutoff:
            n += 1

    info = {
        "delimiter": delim,
        "header": header_raw,
        "usedHeader": idx is not None,
        "totalRows": len(rows) - (1 if idx is not None else 0),
        "parsedDates": parsed,
        "badDates": bad,
        "sample": sample,
    }
    return n, info


# --------------------
# HTML
# --------------------
PAGE_HTML = """<!doctype html>
<html lang="sv">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Medlemsräknare</title>
  <link rel="preload" href="https://res.cloudinary.com/dufekxhkq/raw/upload/v1754975484/AvertaStd-Regular_z8oywc.woff2" as="font" type="font/woff2" crossorigin>
  <link rel="preload" href="https://res.cloudinary.com/dufekxhkq/raw/upload/v1754991669/AvertaStd-Bold_fczy4p.woff2" as="font" type="font/woff2" crossorigin>
  <link rel="preload" href="https://res.cloudinary.com/dufekxhkq/raw/upload/v1754975484/Prohibition-Regular_ikmxte.woff2" as="font" type="font/woff2" crossorigin>
  <style>
    @font-face {
      font-family: "AvertaStd";
      src: url("https://res.cloudinary.com/dufekxhkq/raw/upload/v1754975484/AvertaStd-Regular_z8oywc.woff2") format("woff2");
      font-weight: 400;
      font-style: normal;
      font-display: swap;
    }
    @font-face {
      font-family: "AvertaStd";
      src: url("https://res.cloudinary.com/dufekxhkq/raw/upload/v1754991669/AvertaStd-Bold_fczy4p.woff2") format("woff2");
      font-weight: 700;
      font-style: normal;
      font-display: swap;
    }
    @font-face {
      font-family: "Prohibition";
      src: url("https://res.cloudinary.com/dufekxhkq/raw/upload/v1754975484/Prohibition-Regular_ikmxte.woff2") format("woff2");
      font-weight: 400;
      font-style: normal;
      font-display: swap;
    }

    :root {
      --fg: #f5f7fb;
      --accent: #ffcb00;
      --muted: #d7d7d7;
      --btn: rgba(255,255,255,0.15);
      --btn-border: rgba(255,255,255,0.45);
      --shadow: 0 8px 24px rgba(0,0,0,0.35);
      --cta-width: min(320px, 90vw);
    }

    * { box-sizing: border-box; }

    html { scroll-behavior: smooth; }
    body {
      margin: 0;
      font-family: "AvertaStd", "Segoe UI", sans-serif;
      color: var(--fg);
      -webkit-text-size-adjust: 100%;
      background:
        url("https://images.markethype.io/dbf20cd3-e529-47e5-a643-47c8c201c7ab.png")
        center / cover no-repeat fixed;
      position: relative;
    }
    body::before {
      content: "";
      position: fixed;
      inset: 0;
      background: linear-gradient(180deg, rgba(0,0,0,0.12), rgba(0,0,0,0.38));
      pointer-events: none;
      z-index: 0;
    }

    @media (max-aspect-ratio: 9/16) {
      body {
        background-image:
          url("https://images.markethype.io/69abc93d-ba01-46cf-a4d7-864975afbbe2.png");
      }
    }

    .page {
      min-height: 100vh;
      padding: 20px 16px 64px;
      display: flex;
      flex-direction: column;
      gap: 32px;
      position: relative;
      z-index: 1;
    }

    .hero {
      display: grid;
      gap: 18px;
      align-items: center;
      justify-items: center;
      text-align: center;
    }

    .banner-img {
      width: 100%;
      max-width: 820px;
      justify-self: center;
      filter: drop-shadow(0 6px 18px rgba(0,0,0,0.35));
    }

    .title-block {
      width: clamp(320px, 90vw, 900px);
      display: grid;
      gap: 2px;
      text-align: center;
    }
    .title {
      font-family: "Prohibition", "AvertaStd", sans-serif;
      font-size: 34px;
      letter-spacing: 1px;
      text-transform: uppercase;
      margin: 0;
      width: 100%;
      line-height: 1.05;
    }
    .title-sub {
      font-family: "AvertaStd", "Segoe UI", sans-serif;
      font-weight: 700;
      font-size: 16px;
      letter-spacing: 0.12em;
      text-transform: uppercase;
      margin: 0;
      width: 100%;
      white-space: nowrap;
      line-height: 1.05;
    }

    .count {
      font-family: "Prohibition", "AvertaStd", sans-serif;
      font-size: 88px;
      font-weight: 400;
      color: var(--accent);
      letter-spacing: 1px;
      text-shadow: 0 2px 10px rgba(0,0,0,0.45);
      animation: pulse 2.4s ease-in-out infinite;
      width: var(--cta-width);
    }

    .count.bump {
      animation: bump 0.6s ease-out 1;
    }

    .meta {
      font-size: 14px;
      color: var(--muted);
      width: var(--cta-width);
    }

    .bodytext {
      margin: 0;
      font-size: 15px;
      line-height: 1.5;
      color: var(--fg);
      opacity: 0.95;
      width: var(--cta-width);
      background: rgba(0,0,0,0.25);
      border: 1px solid rgba(255,255,255,0.1);
      border-radius: 12px;
      padding: 10px 12px;
      box-shadow: 0 10px 24px rgba(0,0,0,0.25);
    }
    .bodytext p {
      margin: 0 0 6px 0;
    }
    .bodytext p:last-child {
      margin-bottom: 0;
    }

    .buttons {
      display: grid;
      gap: 10px;
      margin-top: 10px;
      justify-items: center;
    }

    .progress-wrap {
      margin-top: 12px;
      display: grid;
      gap: 8px;
      width: min(680px, 100%);
    }

    .progress-wrap {
      margin-top: 12px;
      display: grid;
      gap: 8px;
    }

    .progress-labels {
      display: flex;
      justify-content: space-between;
      font-size: 12px;
      color: var(--muted);
    }

    .progress-track {
      position: relative;
      height: 14px;
      border-radius: 999px;
      background: rgba(255,255,255,0.18);
      overflow: hidden;
      border: 1px solid rgba(255,255,255,0.35);
    }

    .progress-fill {
      height: 100%;
      width: 0%;
      background: linear-gradient(90deg, #ffd24d, #ffb347);
      box-shadow: 0 0 16px rgba(255,210,77,0.55);
      transition: width 0.5s ease;
    }

    .progress-tick {
      position: absolute;
      top: -4px;
      width: 2px;
      height: 22px;
      background: rgba(255,255,255,0.75);
      box-shadow: 0 0 8px rgba(255,255,255,0.35);
    }

    .milestones {
      position: relative;
      height: 44px;
      margin-top: 6px;
      padding: 0 8px;
    }

    .milestone {
      position: absolute;
      top: 0;
      transform: translateX(-50%);
      display: grid;
      gap: 4px;
      align-items: center;
      justify-items: center;
      color: var(--muted);
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.4px;
      white-space: nowrap;
    }

    .milestone img {
      width: 28px;
      height: 28px;
      object-fit: contain;
      filter: drop-shadow(0 4px 10px rgba(0,0,0,0.35));
    }

    .btn {
      appearance: none;
      border: 1px solid var(--btn-border);
      background: var(--btn);
      color: var(--fg);
      font-family: "AvertaStd", "Segoe UI", sans-serif;
      font-size: 16px;
      padding: 12px 16px;
      border-radius: 10px;
      cursor: pointer;
      box-shadow: var(--shadow);
      text-transform: uppercase;
      letter-spacing: 0.5px;
    }

    .btn.primary {
      background: #ffffff;
      color: #111;
      border-color: #ffffff;
      animation: cta-pulse 2.2s ease-in-out infinite;
      width: var(--cta-width);
    }

    .form-section {
      margin-top: 8px;
      background: rgba(0,0,0,0.18);
      border: 1px solid rgba(255,255,255,0.12);
      border-radius: 18px;
      padding: 14px;
      box-shadow: var(--shadow);
      display: none;
    }

    .form-title {
      font-family: "Prohibition", "AvertaStd", sans-serif;
      font-size: 20px;
      margin: 0 0 10px 6px;
      color: var(--fg);
    }

    .iframe-shell {
      position: relative;
      border-radius: 14px;
      overflow: hidden;
      background:
        linear-gradient(180deg, rgba(255,255,255,0.12), rgba(255,255,255,0.02)),
        url("https://images.markethype.io/dbf20cd3-e529-47e5-a643-47c8c201c7ab.png")
        center / cover no-repeat;
      border: 1px solid rgba(255,255,255,0.18);
      box-shadow: inset 0 1px 0 rgba(255,255,255,0.2), 0 16px 40px rgba(0,0,0,0.35);
      min-height: 75vh;
      isolation: isolate;
    }

    .iframe-shell::before {
      content: "";
      position: absolute;
      inset: 0;
      background: rgba(0,0,0,0.25);
      z-index: 0;
    }

    .iframe-bar {
      position: absolute;
      top: 0;
      left: 0;
      right: 0;
      height: 46px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 0 14px;
      background: rgba(0,0,0,0.45);
      color: var(--fg);
      z-index: 2;
      border-bottom: 1px solid rgba(255,255,255,0.12);
      font-size: 13px;
      letter-spacing: 0.4px;
      text-transform: uppercase;
    }

    .iframe-frame {
      position: relative;
      z-index: 1;
      margin-top: 46px;
      border-radius: 0 0 14px 14px;
      overflow: hidden;
      background: #fff;
    }

    iframe {
      width: 100%;
      height: 100%;
      min-height: 75vh;
      border: none;
      display: block;
      background: #fff;
    }

    .flash {
      animation: flash 0.6s ease-out 1;
    }

    @keyframes pulse {
      0% { transform: scale(1); }
      50% { transform: scale(1.04); }
      100% { transform: scale(1); }
    }

    @keyframes bump {
      0% { transform: scale(1); }
      30% { transform: scale(1.12); }
      60% { transform: scale(1.02); }
      100% { transform: scale(1); }
    }

    @keyframes flash {
      0% { box-shadow: 0 0 0 rgba(255,221,87,0.0); }
      40% { box-shadow: 0 0 28px rgba(255,221,87,0.6); }
      100% { box-shadow: 0 0 0 rgba(255,221,87,0.0); }
    }

    @keyframes cta-pulse {
      0% { transform: scale(1); box-shadow: 0 8px 22px rgba(0,0,0,0.2); }
      50% { transform: scale(1.05); box-shadow: 0 12px 28px rgba(0,0,0,0.28); }
      100% { transform: scale(1); box-shadow: 0 8px 22px rgba(0,0,0,0.2); }
    }

    @media (min-width: 720px) {
      .page { padding: 32px 48px 80px; }
      .hero { grid-template-columns: 1fr; }
      .count { font-size: 120px; }
      .title { font-size: 40px; }
      .title-sub { font-size: 20px; }
      .buttons { grid-template-columns: 1fr; }
      .form-title { font-size: 26px; }
    }

    @media (min-width: 1080px) {
      .page { max-width: 1100px; margin: 0 auto; }
      .count { font-size: 140px; }
      .title { font-size: 46px; white-space: nowrap; }
      .title-sub { font-size: 18px; }
    }
  </style>
</head>
<body>
  <main class="page">
    <section class="hero" id="top">
      <img class="banner-img" src="https://res.cloudinary.com/dufekxhkq/image/upload/v1758878747/banner_omro%CC%88stning_vit_tmguie.png" alt="Banner" />
      <div class="title-block">
        <h1 class="title">NU BÖRJAR RESAN</h1>
        <p class="title-sub">MOT 13 000 MEDLEMMAR!</p>
      </div>
      <div class="count" id="count">0</div>
      <div class="meta" id="remaining">Kvar: 13 000</div>
      <p class="bodytext" id="bodytext">
        Medlemsåret 2026/2027 är öppet.<br><strong>Nu skriver vi historia!</strong>
      </p>
      <div class="buttons">
        <button class="btn primary" data-open="form">Bli medlem</button>
      </div>
      <div class="progress-wrap" aria-live="polite">
        <div class="progress-labels">
          <span>0</span>
          <span>Mål: 13 000</span>
          <span>13 000</span>
        </div>
        <div class="progress-track" role="progressbar" aria-valuemin="0" aria-valuemax="13000" aria-valuenow="0">
          <div class="progress-fill" id="progress-fill"></div>
          <div class="progress-tick" id="tick-ske" aria-hidden="true"></div>
          <div class="progress-tick" id="tick-bry" aria-hidden="true"></div>
          <div class="progress-tick" id="tick-dif" aria-hidden="true"></div>
        </div>
        <div class="milestones">
          <div class="milestone" id="ms-ske">
            <img src="https://res.cloudinary.com/dufekxhkq/image/upload/v1758799013/Skelleftea%CC%8A_AIK_Logo.svg_gilq9s.png" alt="Skellefteå" />
            <div>5 000</div>
          </div>
          <div class="milestone" id="ms-bry">
            <img src="https://res.cloudinary.com/dufekxhkq/image/upload/v1758799014/200_1_rjtth0.png" alt="Brynäs" />
            <div>10 000</div>
          </div>
          <div class="milestone" id="ms-dif">
            <img src="https://res.cloudinary.com/dufekxhkq/image/upload/v1758799013/DIF_wveql2.png" alt="Djurgården" />
            <div>12 894</div>
          </div>
        </div>
      </div>
    </section>

    <section class="form-section" id="form">
      <h2 class="form-title">Medlemskap</h2>
      <div class="iframe-shell">
        <div class="iframe-bar">
          <span>Medlemsformulär</span>
          <span>luleahockey.propublik.se</span>
        </div>
        <div class="iframe-frame">
          <iframe id="member-form" src="https://luleahockey.propublik.se/test" title="Medlemsformulär"></iframe>
        </div>
      </div>
    </section>
  </main>

  <script>
    const params = new URLSearchParams(location.search);
    const animMode = params.get('anim') || '1';
    let current = 0;
    const countEl = document.getElementById('count');
    const remainingEl = document.getElementById('remaining');
    const hero = document.getElementById('top');
    const progressFill = document.getElementById('progress-fill');
    const progressTrack = document.querySelector('.progress-track');
    const tickSke = document.getElementById('tick-ske');
    const tickBry = document.getElementById('tick-bry');
    const tickDif = document.getElementById('tick-dif');
    const msSke = document.getElementById('ms-ske');
    const msBry = document.getElementById('ms-bry');
    const msDif = document.getElementById('ms-dif');
    const progressMax = 13000;

    function animateTo(target) {
      if (target <= current) {
        current = target;
        countEl.textContent = current.toLocaleString('sv-SE');
        updateProgress(current);
        return;
      }
      const step = Math.max(1, Math.floor((target - current) / 15));
      const tick = () => {
        current = Math.min(target, current + step);
        countEl.textContent = current.toLocaleString('sv-SE');
        updateProgress(current);
        if (current < target) requestAnimationFrame(tick);
      };
      requestAnimationFrame(tick);
    }

    function updateProgress(value) {
      const pct = Math.min(100, Math.max(0, (value / progressMax) * 100));
      progressFill.style.width = pct + '%';
      const clampPct = (pct) => Math.min(96, Math.max(2, pct));
      if (msSke) msSke.style.left = clampPct(5000 / progressMax * 100) + '%';
      if (msBry) msBry.style.left = clampPct(10000 / progressMax * 100) + '%';
      if (msDif) msDif.style.left = clampPct(12894 / progressMax * 100) + '%';
      if (tickSke) tickSke.style.left = clampPct(5000 / progressMax * 100) + '%';
      if (tickBry) tickBry.style.left = clampPct(10000 / progressMax * 100) + '%';
      if (tickDif) tickDif.style.left = clampPct(12894 / progressMax * 100) + '%';
      if (progressTrack) progressTrack.setAttribute('aria-valuenow', String(value));
    }

    function bump() {
      countEl.classList.remove('bump');
      hero.classList.remove('flash');
      hero.classList.remove('flash-2');
      void countEl.offsetWidth;
      countEl.classList.add('bump');
      if (animMode === '2') {
        hero.classList.add('flash-2');
      } else {
        hero.classList.add('flash');
      }
    }

    async function refresh() {
      try {
        const res = await fetch('/member-count');
        const data = await res.json();
        const next = data.count || 0;
        if (next > current) bump();
        animateTo(next);
        const remaining = Math.max(0, progressMax - next);
        remainingEl.textContent = 'Kvar: ' + remaining.toLocaleString('sv-SE');
      } catch (e) {
        remainingEl.textContent = 'Uppdatering misslyckades';
      }
    }

    document.querySelectorAll('[data-open]').forEach(btn => {
      btn.addEventListener('click', () => {
        const target = document.getElementById('form');
        if (!target) return;
        target.style.display = 'block';
        target.scrollIntoView({ behavior: 'smooth', block: 'start' });
      });
    });

    function sizeIframe() {
      const frame = document.getElementById('member-form');
      if (!frame) return;
      const h = Math.max(680, Math.floor(window.innerHeight * 0.85));
      frame.style.minHeight = h + 'px';
    }

    window.addEventListener('resize', sizeIframe);
    sizeIframe();

    refresh();
    setInterval(refresh, 5000);
  </script>
</body>
</html>
"""

PRE_PAGE_HTML = """<!doctype html>
<html lang="sv">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Medlemsräknare</title>
  <link rel="preload" href="https://res.cloudinary.com/dufekxhkq/raw/upload/v1754975484/AvertaStd-Regular_z8oywc.woff2" as="font" type="font/woff2" crossorigin>
  <link rel="preload" href="https://res.cloudinary.com/dufekxhkq/raw/upload/v1754991669/AvertaStd-Bold_fczy4p.woff2" as="font" type="font/woff2" crossorigin>
  <link rel="preload" href="https://res.cloudinary.com/dufekxhkq/raw/upload/v1754975484/Prohibition-Regular_ikmxte.woff2" as="font" type="font/woff2" crossorigin>
  <style>
    @font-face {
      font-family: "AvertaStd";
      src: url("https://res.cloudinary.com/dufekxhkq/raw/upload/v1754975484/AvertaStd-Regular_z8oywc.woff2") format("woff2");
      font-weight: 400;
      font-style: normal;
      font-display: swap;
    }
    @font-face {
      font-family: "AvertaStd";
      src: url("https://res.cloudinary.com/dufekxhkq/raw/upload/v1754991669/AvertaStd-Bold_fczy4p.woff2") format("woff2");
      font-weight: 700;
      font-style: normal;
      font-display: swap;
    }
    @font-face {
      font-family: "Prohibition";
      src: url("https://res.cloudinary.com/dufekxhkq/raw/upload/v1754975484/Prohibition-Regular_ikmxte.woff2") format("woff2");
      font-weight: 400;
      font-style: normal;
      font-display: swap;
    }
    :root {
      --fg: #f5f7fb;
      --accent: #ffcb00;
      --muted: #d7d7d7;
      --shadow: 0 8px 24px rgba(0,0,0,0.35);
      --cta-width: min(360px, 92vw);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: "AvertaStd", "Segoe UI", sans-serif;
      color: var(--fg);
      -webkit-text-size-adjust: 100%;
      font-synthesis: weight;
      background:
        url("https://images.markethype.io/dbf20cd3-e529-47e5-a643-47c8c201c7ab.png")
        center / cover no-repeat fixed;
      min-height: 100vh;
    }
    @media (max-aspect-ratio: 9/16) {
      body {
        background-image:
          url("https://images.markethype.io/69abc93d-ba01-46cf-a4d7-864975afbbe2.png");
      }
    }
    .page {
      min-height: 100vh;
      padding: 24px 16px 56px;
      display: grid;
      place-items: center;
      text-align: center;
      gap: 14px;
    }
    .banner-img {
      width: 100%;
      max-width: 760px;
      filter: drop-shadow(0 6px 18px rgba(0,0,0,0.35));
    }
    h1 {
      font-family: "Prohibition", "AvertaStd", sans-serif;
      font-size: 34px;
      margin: 0;
      letter-spacing: 1px;
      text-transform: uppercase;
      width: var(--cta-width);
    }
    .startline {
      font-family: "Prohibition", "AvertaStd", sans-serif;
      font-size: 20px;
      letter-spacing: 1px;
      text-transform: uppercase;
      color: var(--accent);
      margin-top: 6px;
      margin-bottom: -42px;
      width: var(--cta-width);
    }
    .countdown {
      font-family: "Prohibition", "AvertaStd", sans-serif;
      font-size: 74px;
      color: var(--accent);
      letter-spacing: 1px;
      text-shadow: 0 0 18px rgba(255,203,0,0.35), 0 2px 10px rgba(0,0,0,0.45);
      width: var(--cta-width);
    }
    .bodytext {
      margin: 0;
      font-size: 15px;
      line-height: 1.5;
      color: var(--fg);
      opacity: 0.95;
      width: var(--cta-width);
      background: rgba(0,0,0,0.25);
      border: 1px solid rgba(255,255,255,0.1);
      border-radius: 12px;
      padding: 10px 12px;
      box-shadow: 0 10px 24px rgba(0,0,0,0.25);
    }
    .bodytext p {
      margin: 0 0 6px 0;
    }
    .bodytext p:last-child {
      margin-bottom: 0;
    }
    .bodytext strong {
      font-weight: 700;
    }
    .sub {
      font-size: 14px;
      color: var(--muted);
      width: var(--cta-width);
    }
    .regional {
      font-size: 14px;
      color: var(--fg);
      opacity: 0.9;
      width: var(--cta-width);
      letter-spacing: 0.3px;
    }
    .card {
      width: min(480px, 86vw);
      padding: 16px;
      background: rgba(0,0,0,0.32);
      border: 1px solid rgba(255,255,255,0.22);
      border-radius: 20px;
      box-shadow: var(--shadow);
      text-align: left;
    }
    .card h2 {
      font-family: "Prohibition", "AvertaStd", sans-serif;
      font-size: 20px;
      margin: 0 0 6px 0;
    }
    label { display: block; font-size: 13px; margin: 8px 0 4px; color: var(--muted); }
    input {
      width: 100%;
      padding: 10px 12px;
      border-radius: 10px;
      border: 1px solid rgba(255,255,255,0.25);
      background: rgba(255,255,255,0.08);
      color: var(--fg);
      outline: none;
    }
    .row { display: grid; gap: 10px; }
    .btn {
      margin-top: 12px;
      width: 100%;
      padding: 12px 14px;
      border-radius: 10px;
      border: 1px solid #fff;
      background: #fff;
      color: #111;
      font-family: "AvertaStd", "Segoe UI", sans-serif;
      text-transform: uppercase;
      letter-spacing: 0.6px;
      cursor: pointer;
    }
    .status { font-size: 13px; color: var(--muted); margin-top: 6px; }
    .consent { font-size: 12px; color: var(--muted); margin-top: 6px; }
    .proof {
      font-size: 13px;
      color: var(--fg);
      opacity: 0.9;
      margin-top: 8px;
    }
    @media (min-width: 720px) {
      h1 { font-size: 42px; }
      .startline { font-size: 22px; }
      .countdown { font-size: 98px; }
      .bodytext { font-size: 17px; }
      .sub, .regional { font-size: 15px; }
    }
    @media (min-width: 1080px) {
      .bodytext { font-size: 18px; }
      .sub, .regional { font-size: 16px; }
    }
  </style>
</head>
<body>
  <main class="page">
    <img class="banner-img" src="https://res.cloudinary.com/dufekxhkq/image/upload/v1758878747/banner_omro%CC%88stning_vit_tmguie.png" alt="Banner" />
    <h1>Var med när vi blir störst!</h1>
    <div class="bodytext" id="pretext">
      <p>Målet: 13 000 medlemmar.</p>
      <p>Då är vi inte bara Sveriges, utan världens största ishockeyförening!</p>
      <p><strong>Var med och skriv historia!</strong></p>
    </div>

    <div class="startline">START 1 MAJ</div>
    <div class="countdown" id="countdown">--:--:--</div>
  

    <div class="card">
      <h2>Säkra din plats från dag ett</h2>
      <div class="sub">Lämna dina uppgifter så påminner vi dig när medlemsåret öppnar.</div>
      <div class="row">
        <div>
          <label for="email">E‑post</label>
          <input id="email" type="email" placeholder="namn@exempel.se" />
        </div>
        <div>
          <label for="phone">Telefon</label>
          <input id="phone" type="tel" placeholder="0701234567" />
        </div>
      </div>
      <div class="consent">Genom att skicka in godkänner du att vi kontaktar dig vid öppning.</div>
      <button class="btn" id="remind-btn">JAG VILL VARA MED!</button>
      <div class="proof" id="proof">0 personer har redan anmält sig.</div>
      <div class="status" id="status"></div>
    </div>
  </main>

  <script>
    const launch = new Date("{{LAUNCH_ISO}}");
    const countdownEl = document.getElementById('countdown');
    const statusEl = document.getElementById('status');
    const btn = document.getElementById('remind-btn');
    const proofEl = document.getElementById('proof');

    function pad(n) { return String(n).padStart(2, '0'); }
    function tick() {
      const now = new Date();
      const diff = launch - now;
      if (diff <= 0) {
        location.href = '/page';
        return;
      }
      const total = Math.floor(diff / 1000);
      const h = Math.floor(total / 3600);
      const m = Math.floor((total % 3600) / 60);
      const s = total % 60;
      countdownEl.textContent = pad(h) + ':' + pad(m) + ':' + pad(s);
    }
    setInterval(tick, 1000);
    tick();

    btn.addEventListener('click', async () => {
      const email = document.getElementById('email').value.trim();
      const phone = document.getElementById('phone').value.trim();
      const digits = phone.replace(/\\D/g, '');
      if (!email && digits.length < 7) {
        statusEl.textContent = 'Fyll i e‑post eller giltigt telefonnummer.';
        return;
      }
      statusEl.textContent = 'Skickar...';
      try {
        const res = await fetch('/remind', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ email, phone })
        });
        const data = await res.json();
        statusEl.textContent = data.ok ? 'Tack! Vi påminner dig.' : (data.error || 'Fel vid skick');
        if (data.ok) {
          document.getElementById('email').value = '';
          document.getElementById('phone').value = '';
          if (proofEl && typeof data.count === 'number') {
            proofEl.textContent = data.count.toLocaleString('sv-SE') + ' personer har redan anmält sig.';
          }
        }
      } catch (e) {
        statusEl.textContent = 'Fel vid skick';
      }
    });

    async function refreshProof() {
      try {
        const res = await fetch('/reminders-count');
        const data = await res.json();
        if (proofEl && typeof data.count === 'number') {
          proofEl.textContent = data.count.toLocaleString('sv-SE') + ' personer har redan anmält sig.';
        }
      } catch (e) {}
    }
    refreshProof();

    setInterval(async () => {
      try {
        const res = await fetch('/status');
        const data = await res.json();
        if (data.live) location.href = '/page';
      } catch (e) {}
    }, 5000);
  </script>
</body>
</html>
"""

STRIP_HTML = """<!doctype html>
<html lang="sv">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Medlemsräknare</title>
  <link rel="preload" href="https://res.cloudinary.com/dufekxhkq/raw/upload/v1754975484/AvertaStd-Regular_z8oywc.woff2" as="font" type="font/woff2" crossorigin>
  <link rel="preload" href="https://res.cloudinary.com/dufekxhkq/raw/upload/v1754991669/AvertaStd-Bold_fczy4p.woff2" as="font" type="font/woff2" crossorigin>
  <link rel="preload" href="https://res.cloudinary.com/dufekxhkq/raw/upload/v1754975484/Prohibition-Regular_ikmxte.woff2" as="font" type="font/woff2" crossorigin>
  <style>
    @font-face {
      font-family: "AvertaStd";
      src: url("https://res.cloudinary.com/dufekxhkq/raw/upload/v1754975484/AvertaStd-Regular_z8oywc.woff2") format("woff2");
      font-weight: 400;
      font-style: normal;
      font-display: swap;
    }
    @font-face {
      font-family: "AvertaStd";
      src: url("https://res.cloudinary.com/dufekxhkq/raw/upload/v1754991669/AvertaStd-Bold_fczy4p.woff2") format("woff2");
      font-weight: 700;
      font-style: normal;
      font-display: swap;
    }
    @font-face {
      font-family: "Prohibition";
      src: url("https://res.cloudinary.com/dufekxhkq/raw/upload/v1754975484/Prohibition-Regular_ikmxte.woff2") format("woff2");
      font-weight: 400;
      font-style: normal;
      font-display: swap;
    }

    :root {
      --fg: #f5f7fb;
      --accent: #ffcb00;
      --muted: #d9d2cf;
      --count-size: 64px;
      --headline-size: 22px;
    }
    * { box-sizing: border-box; }
    html, body {
      height: 100%;
    }
    body {
      margin: 0;
      font-family: "AvertaStd", "Segoe UI", sans-serif;
      color: var(--fg);
      -webkit-text-size-adjust: 100%;
      background:
        linear-gradient(180deg, rgba(0,0,0,0.15), rgba(0,0,0,0.35)),
        url("https://images.markethype.io/dbf20cd3-e529-47e5-a643-47c8c201c7ab.png")
        center / cover no-repeat;
      background-repeat: no-repeat, no-repeat;
      background-size: cover, cover;
      background-position: center, center;
    }
    .hero {
      min-height: 180px;
      padding: 20px 18px 16px;
      display: grid;
      gap: 10px;
      justify-items: center;
      text-align: center;
    }
    .headline {
      font-family: "Prohibition", "AvertaStd", sans-serif;
      font-size: var(--headline-size);
      letter-spacing: 1px;
      text-transform: uppercase;
    }
    .count-zone {
      width: min(520px, 92vw);
      padding: 12px 14px;
      border-radius: 16px;
      background: radial-gradient(80% 120% at 50% 40%, rgba(0,0,0,0.45), rgba(0,0,0,0.15));
      box-shadow: inset 0 1px 0 rgba(255,255,255,0.08);
    }
    .count {
      font-family: "Prohibition", "AvertaStd", sans-serif;
      font-size: var(--count-size);
      letter-spacing: 1px;
      color: var(--accent);
      text-shadow: 0 0 14px rgba(255,203,0,0.35), 0 2px 10px rgba(0,0,0,0.4);
    }
    .count.roll {
      animation: roll 0.3s ease-out 1;
    }
    .label {
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.8px;
      color: var(--muted);
      margin-top: 6px;
    }
    .progress {
      width: min(520px, 92vw);
      display: grid;
      gap: 6px;
    }
    .track {
      height: 14px;
      background: rgba(0,0,0,0.35);
      border-radius: 999px;
      overflow: hidden;
      border: 1px solid rgba(255,255,255,0.15);
    }
    .fill {
      height: 100%;
      width: 0%;
      background: linear-gradient(90deg, #ffcb00, #ffb300);
      transition: width 0.3s ease;
      box-shadow: 0 0 12px rgba(255,203,0,0.35);
    }
    .remain {
      font-size: 13px;
      color: var(--fg);
      opacity: 0.9;
    }
    @keyframes roll {
      0% { transform: translateY(6px); opacity: 0.7; }
      100% { transform: translateY(0); opacity: 1; }
    }
    @media (min-width: 720px) {
      .hero { padding: 26px 24px 18px; }
      .headline { font-size: var(--headline-size); }
      .count { font-size: var(--count-size); }
      .count-zone { width: min(720px, 92vw); }
      .progress { width: min(720px, 92vw); }
    }
  </style>
</head>
<body>
  <section class="hero" id="hero">
    <div class="headline">Nu går vi för 13 000 medlemmar!</div>
    <div class="count-zone">
      <div class="count" id="count">0</div>
      <div class="label">Medlemmar</div>
    </div>
    <div class="progress">
      <div class="track"><div class="fill" id="fill"></div></div>
      <div class="remain" id="remain">13 000 kvar till målet</div>
    </div>
  </section>

  <script>
    const params = new URLSearchParams(location.search);
    const h = params.get('h');
    if (h) {
      const hero = document.getElementById('hero');
      const px = parseInt(h, 10);
      if (hero && !Number.isNaN(px)) {
        hero.style.minHeight = px + 'px';
        hero.style.height = px + 'px';
        hero.style.maxHeight = px + 'px';
        hero.style.paddingTop = Math.max(8, Math.floor(px * 0.12)) + 'px';
        hero.style.paddingBottom = Math.max(8, Math.floor(px * 0.08)) + 'px';
        hero.style.gap = Math.max(6, Math.floor(px * 0.06)) + 'px';
        const countSize = Math.max(34, Math.floor(px * 0.45));
        const headSize = Math.max(14, Math.floor(px * 0.14));
        document.documentElement.style.setProperty('--count-size', countSize + 'px');
        document.documentElement.style.setProperty('--headline-size', headSize + 'px');
      }
    }
    let current = 0;
    const countEl = document.getElementById('count');
    const fillEl = document.getElementById('fill');
    const remainEl = document.getElementById('remain');
    const max = 13000;

    function setCount(val) {
      countEl.classList.remove('roll');
      void countEl.offsetWidth;
      countEl.classList.add('roll');
      countEl.textContent = val.toLocaleString('sv-SE');
      const pct = Math.min(100, Math.max(0, (val / max) * 100));
      fillEl.style.width = pct + '%';
      const remaining = Math.max(0, max - val);
      remainEl.textContent = remaining.toLocaleString('sv-SE') + ' kvar till målet';
    }

    async function refresh() {
      try {
        const res = await fetch('/member-count');
        const data = await res.json();
        const next = data.count || 0;
        if (next !== current) {
          current = next;
          setCount(current);
        }
      } catch (e) {}
    }
    refresh();
    setInterval(refresh, 5000);
  </script>
</body>
</html>
"""

PRE_STRIP_HTML = """<!doctype html>
<html lang="sv">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Medlemsräknare</title>
  <link rel="preload" href="https://res.cloudinary.com/dufekxhkq/raw/upload/v1754975484/AvertaStd-Regular_z8oywc.woff2" as="font" type="font/woff2" crossorigin>
  <link rel="preload" href="https://res.cloudinary.com/dufekxhkq/raw/upload/v1754991669/AvertaStd-Bold_fczy4p.woff2" as="font" type="font/woff2" crossorigin>
  <link rel="preload" href="https://res.cloudinary.com/dufekxhkq/raw/upload/v1754975484/Prohibition-Regular_ikmxte.woff2" as="font" type="font/woff2" crossorigin>
  <style>
    @font-face {
      font-family: "AvertaStd";
      src: url("https://res.cloudinary.com/dufekxhkq/raw/upload/v1754975484/AvertaStd-Regular_z8oywc.woff2") format("woff2");
      font-weight: 400;
      font-style: normal;
      font-display: swap;
    }
    @font-face {
      font-family: "AvertaStd";
      src: url("https://res.cloudinary.com/dufekxhkq/raw/upload/v1754991669/AvertaStd-Bold_fczy4p.woff2") format("woff2");
      font-weight: 700;
      font-style: normal;
      font-display: swap;
    }
    @font-face {
      font-family: "Prohibition";
      src: url("https://res.cloudinary.com/dufekxhkq/raw/upload/v1754975484/Prohibition-Regular_ikmxte.woff2") format("woff2");
      font-weight: 400;
      font-style: normal;
      font-display: swap;
    }
    :root {
      --fg: #f5f7fb;
      --accent: #ffcb00;
      --muted: #d9d2cf;
      --count-size: 64px;
      --headline-size: 22px;
    }
    * { box-sizing: border-box; }
    html, body {
      height: 100%;
    }
    body {
      margin: 0;
      font-family: "AvertaStd", "Segoe UI", sans-serif;
      color: var(--fg);
      -webkit-text-size-adjust: 100%;
      background:
        linear-gradient(180deg, rgba(0,0,0,0.15), rgba(0,0,0,0.35)),
        url("https://images.markethype.io/dbf20cd3-e529-47e5-a643-47c8c201c7ab.png")
        center / cover no-repeat;
      background-repeat: no-repeat, no-repeat;
      background-size: cover, cover;
      background-position: center, center;
    }
    .hero {
      min-height: 200px;
      padding: 22px 20px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 18px;
    }
    .left {
      flex: 1 1 auto;
      display: grid;
      gap: 6px;
    }
    .headline {
      font-family: "Prohibition", "AvertaStd", sans-serif;
      font-size: var(--headline-size);
      letter-spacing: 1px;
      text-transform: uppercase;
      color: var(--fg);
    }
    .sub {
      font-size: 13px;
      color: var(--muted);
    }
    .count-zone {
      flex: 0 0 auto;
      padding: 12px 14px;
      border-radius: 16px;
      background: radial-gradient(80% 120% at 50% 40%, rgba(0,0,0,0.45), rgba(0,0,0,0.15));
      box-shadow: inset 0 1px 0 rgba(255,255,255,0.08);
    }
    .countdown {
      font-family: "Prohibition", "AvertaStd", sans-serif;
      font-size: var(--count-size);
      color: var(--accent);
      letter-spacing: 1px;
      text-shadow: 0 0 12px rgba(255,203,0,0.35), 0 2px 10px rgba(0,0,0,0.4);
      white-space: nowrap;
    }
    @media (max-width: 720px) {
      .hero {
        flex-direction: column;
        text-align: center;
      }
    }
  </style>
</head>
<body>
  <section class="hero" id="hero">
    <div class="left">
      <div class="headline">VAR MED NÄR VI BLIR STÖRST.</div>
      <div class="sub">1 maj börjar resan mot 13 000 medlemmar</div>
    </div>
    <div class="count-zone">
      <div class="countdown" id="countdown">--:--:--</div>
    </div>
  </section>

  <script>
    const params = new URLSearchParams(location.search);
    const h = params.get('h');
    if (h) {
      const hero = document.getElementById('hero');
      const px = parseInt(h, 10);
      if (hero && !Number.isNaN(px)) {
        hero.style.minHeight = px + 'px';
        hero.style.height = px + 'px';
        hero.style.maxHeight = px + 'px';
        hero.style.paddingTop = Math.max(8, Math.floor(px * 0.12)) + 'px';
        hero.style.paddingBottom = Math.max(8, Math.floor(px * 0.08)) + 'px';
        const countSize = Math.max(34, Math.floor(px * 0.45));
        const headSize = Math.max(18, Math.floor(px * 0.18));
        document.documentElement.style.setProperty('--count-size', countSize + 'px');
        document.documentElement.style.setProperty('--headline-size', headSize + 'px');
      }
    }

    const launch = new Date("{{LAUNCH_ISO}}");
    const countdownEl = document.getElementById('countdown');

    function pad(n) { return String(n).padStart(2, '0'); }
    function tick() {
      const now = new Date();
      const diff = launch - now;
      if (diff <= 0) { location.href = '/banner'; return; }
      const total = Math.floor(diff / 1000);
      const d = Math.floor(total / 86400);
      const h = Math.floor((total % 86400) / 3600);
      const m = Math.floor((total % 3600) / 60);
      const s = total % 60;
      countdownEl.textContent = pad(d) + ':' + pad(h) + ':' + pad(m) + ':' + pad(s);
    }

    setInterval(tick, 1000);
    tick();
  </script>
</body>
</html>
"""


UPLOAD_HTML = """<!doctype html>
<html lang="sv">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Ladda upp medlems-CSV</title>
  <style>
    @font-face {
      font-family: "AvertaStd";
      src: url("https://res.cloudinary.com/dufekxhkq/raw/upload/v1754975484/AvertaStd-Regular_z8oywc.woff2") format("woff2");
      font-weight: 400;
      font-style: normal;
      font-display: swap;
    }
    @font-face {
      font-family: "Prohibition";
      src: url("https://res.cloudinary.com/dufekxhkq/raw/upload/v1754975484/Prohibition-Regular_ikmxte.woff2") format("woff2");
      font-weight: 400;
      font-style: normal;
      font-display: swap;
    }

    body {
      font-family: "AvertaStd", "Segoe UI", sans-serif;
      margin: 0;
      min-height: 100vh;
      color: #f5f7fb;
      background:
        url("https://images.markethype.io/dbf20cd3-e529-47e5-a643-47c8c201c7ab.png")
        center / cover no-repeat;
    }

    @media (max-aspect-ratio: 9/16) {
      body {
        background-image:
          url("https://images.markethype.io/69abc93d-ba01-46cf-a4d7-864975afbbe2.png");
      }
    }

    .box {
      max-width: 520px;
      padding: 16px;
      margin: 24px;
      border: 1px solid rgba(255,255,255,0.25);
      background: rgba(0,0,0,0.35);
      backdrop-filter: blur(4px);
    }

    h3 {
      font-family: "Prohibition", "AvertaStd", sans-serif;
      font-size: 26px;
      margin: 0 0 8px 0;
    }

    button {
      padding: 8px 14px;
      cursor: pointer;
    }
  </style>
</head>
<body>
  <div class="box">
    <h3>Ladda upp CSV (endast Betaldatum)</h3>
    <p>CSV kan innehålla endast kolumnen <code>Betaldatum</code>.</p>
    <input type="file" id="file" accept=".csv,text/csv" />
    <button id="send">Ladda upp</button>
    <p id="status"></p>
    <pre id="debug" style="white-space: pre-wrap;"></pre>
  </div>

  <script>
    const btn = document.getElementById('send');
    btn.onclick = async () => {
      const f = document.getElementById('file').files[0];
      if (!f) return;
      const text = await f.text();
      const res = await fetch('/upload', {
        method: 'POST',
        headers: { 'Content-Type': 'text/csv' },
        body: text
      });
      const data = await res.json();
      document.getElementById('status').textContent =
        'OK: ' + (data.count ?? 0) + ' (uppdaterad ' + (data.updatedAt ?? '') + ')';
      if (data.info) {
        document.getElementById('debug').textContent = JSON.stringify(data.info, null, 2);
      }
    };
  </script>
</body>
</html>
"""


# --------------------
# HTTP server
# --------------------
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        query_force_live = query.get("live", ["0"])[0] == "1"
        live_mode = True  # Always show the live member counter. No countdown/pre-launch mode.

        if path == "/" or path.startswith("/page"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            html = PRE_PAGE_HTML if not live_mode else PAGE_HTML
            html = html.replace("{{LAUNCH_ISO}}", LAUNCH_ISO)
            self.wfile.write(html.encode("utf-8"))
            return

        if path.startswith("/banner"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            html = PRE_STRIP_HTML if not live_mode else STRIP_HTML
            html = html.replace("{{LAUNCH_ISO}}", LAUNCH_ISO)
            self.wfile.write(html.encode("utf-8"))
            return

        if path.startswith("/upload"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(UPLOAD_HTML.encode("utf-8"))
            return

        if path.startswith("/member-count"):
            payload = {
                "count": state["count"],
                "updated_at": state["updated_at"],
                "updatedAt": state["updated_at"],
                "lastError": state["last_error"],
            }
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return

        if path.startswith("/public-count"):
            payload = {
                "count": state["count"],
                "updated_at": state["updated_at"],
            }
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return

        if path.startswith("/debug"):
            payload = {
                "count": state["count"],
                "updatedAt": state["updated_at"],
                "lastError": state["last_error"],
                "lastUpload": state["last_upload"],
                "cutoff": CUTOFF_DATE,
                "apiCountMode": API_COUNT_MODE,
                "itargetApiBase": ITARGET_API_BASE,
                "itargetClientId": ITARGET_CLIENT_ID,
                "itargetPollSeconds": ITARGET_POLL_SECONDS,
                "itargetSkipSslVerify": ITARGET_SKIP_SSL_VERIFY,
                "itargetCountKey": ITARGET_COUNT_KEY,
                "itargetCountEndpointTemplate": ITARGET_COUNT_ENDPOINT_TEMPLATE,
                "itargetMembershipsQuery": ITARGET_MEMBERSHIPS_QUERY,
                "itargetSource": ITARGET_SOURCE or ("members-index-new" if INTERNAL_COUNT_MODE else "memberships-api"),
                "internalCountMode": INTERNAL_COUNT_MODE,
                "itargetInternalEndpoint": ITARGET_INTERNAL_ENDPOINT,
                "itargetInternalMethod": ITARGET_INTERNAL_METHOD,
                "itargetInternalExpectedStatus": ITARGET_INTERNAL_EXPECTED_STATUS,
                "itargetInternalHasHeaders": bool(ITARGET_INTERNAL_HEADERS),
                "itargetInternalHasBody": bool(ITARGET_INTERNAL_BODY),
                "forceLive": FORCE_LIVE,
            }
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return

        if path.startswith("/status"):
            payload = {"live": True}
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return

        if path.startswith("/reminders-count"):
            payload = {"count": reminders_count()}
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return

        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        if self.path.startswith("/remind"):
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw.decode("utf-8"))
            except Exception:
                data = {}

            raw_phone = data.get("phone", "")
            email = clean_email(data.get("email", ""))
            phone = clean_phone(raw_phone)
            if raw_phone and not phone:
                payload = {"ok": False, "error": "Telefonnummer verkar fel."}
            elif not email and not phone:
                payload = {"ok": False, "error": "Fyll i e‑post eller telefon."}
            else:
                save_reminder(email, phone)
                payload = {"ok": True, "count": reminders_count()}

            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path.startswith("/upload"):
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            text = raw.decode("utf-8-sig", errors="replace")

            cutoff = datetime.strptime(CUTOFF_DATE, "%Y-%m-%d")
            count, info = count_from_csv_text(text, cutoff)
            state["count"] = count
            state["updated_at"] = datetime.now().isoformat(timespec="seconds")
            state["last_error"] = None
            state["last_upload"] = info

            payload = {
                "count": state["count"],
                "updatedAt": state["updated_at"],
                "info": info,
            }
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)
            return

        self.send_response(404)
        self.end_headers()

    def log_message(self, format, *args):
        return


if __name__ == "__main__":
    if API_COUNT_MODE:
        t = threading.Thread(target=poll_itarget_count_loop, daemon=True)
        t.start()

    server = HTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Member stream running on http://localhost:{PORT}")
    print("Banner: /banner   Upload: /upload")
    if API_COUNT_MODE:
        source = ITARGET_SOURCE or ("members-index-new" if INTERNAL_COUNT_MODE else "memberships-api")
        if source in ("members-index-new", "internal"):
            print("iTarget count mode ON (source=members-index-new)")
        else:
            print(f"iTarget API count mode ON (client={ITARGET_CLIENT_ID})")
    else:
        print("iTarget API count mode OFF (using upload/manual count)")
    server.serve_forever()
