"""Bullhorn REST client. Writes go through the REST API rather than browser
automation because it is stable and auditable (every note carries the API
user's id and a timestamp).

Auth flow (OAuth 2 password grant, Bullhorn flavour):
  1. GET  {auth_url}?client_id&response_type=code&username&password&action=Login
     -> redirect whose Location carries ?code=...
  2. POST {rest_token_url}?grant_type=authorization_code&code&client_id&client_secret
     -> access_token
  3. GET  {rest_login_url}?version=*&access_token=...
     -> BhRestToken + restUrl, used for every subsequent call.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import parse_qs, urlparse

import requests

from .config import Settings
from .errors import BullhornError
from .models import RecordRef

log = logging.getLogger(__name__)

PERSON_FIELDS = {
    "Candidate": "id,firstName,lastName,name,email,email2,email3,status,isDeleted",
    "ClientContact": "id,firstName,lastName,name,email,email2,email3,status,isDeleted,clientCorporation(id,name)",
}
EMAIL_FIELDS = ("email", "email2", "email3")


def lucene_quote(value: str) -> str:
    """Quote a value for a Lucene phrase query, escaping backslashes and quotes."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def record_from_data(entity: str, data: dict[str, Any]) -> RecordRef:
    name = (data.get("name") or f"{data.get('firstName', '')} {data.get('lastName', '')}").strip()
    email = (data.get("email") or data.get("email2") or data.get("email3") or None)
    detail = None
    if entity == "ClientContact":
        corp = data.get("clientCorporation") or {}
        detail = corp.get("name") if isinstance(corp, dict) else None
    elif entity == "Candidate":
        detail = data.get("status")
    return RecordRef(entity=entity, id=int(data["id"]), name=name, email=email.lower() if email else None, detail=detail)


class BullhornClient:
    def __init__(self, settings: Settings, session: requests.Session | None = None, timeout: float = 30.0):
        self.settings = settings
        self.session = session or requests.Session()
        self.timeout = timeout
        self.rest_url: str | None = None
        self.rest_token: str | None = None

    # ---- auth --------------------------------------------------------------

    def login(self) -> None:
        s = self.settings
        code = self._authorize(s)
        token_resp = self.session.post(
            s.bullhorn_rest_token_url,
            params={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": s.bullhorn_client_id,
                "client_secret": s.bullhorn_client_secret,
            },
            timeout=self.timeout,
        )
        if token_resp.status_code != 200:
            raise BullhornError(f"token exchange failed: HTTP {token_resp.status_code} {token_resp.text[:300]}")
        access_token = token_resp.json().get("access_token")
        if not access_token:
            raise BullhornError("token exchange response had no access_token")

        login_resp = self.session.get(
            s.bullhorn_rest_login_url,
            params={"version": "*", "access_token": access_token},
            timeout=self.timeout,
        )
        if login_resp.status_code != 200:
            raise BullhornError(f"REST login failed: HTTP {login_resp.status_code} {login_resp.text[:300]}")
        body = login_resp.json()
        self.rest_token = body.get("BhRestToken")
        self.rest_url = body.get("restUrl")
        if not self.rest_token or not self.rest_url:
            raise BullhornError("REST login response missing BhRestToken or restUrl")
        if not self.rest_url.endswith("/"):
            self.rest_url += "/"
        log.info("Bullhorn login ok, restUrl=%s", self.rest_url)

    def _authorize(self, s: Settings) -> str:
        """Follow the authorize redirects manually until a Location carries ?code=."""
        url = s.bullhorn_auth_url
        params: dict[str, str] | None = {
            "client_id": s.bullhorn_client_id,
            "response_type": "code",
            "username": s.bullhorn_username,
            "password": s.bullhorn_password,
            "action": "Login",
        }
        for _ in range(6):
            resp = self.session.get(url, params=params, allow_redirects=False, timeout=self.timeout)
            params = None
            location = resp.headers.get("Location", "")
            code = self._code_from_url(location) or self._code_from_url(resp.url)
            if code:
                return code
            if resp.status_code in (301, 302, 303, 307, 308) and location:
                url = location
                continue
            raise BullhornError(
                f"authorize did not return a code: HTTP {resp.status_code}. "
                "Check BULLHORN_USERNAME/PASSWORD and that the API user is not locked."
            )
        raise BullhornError("authorize redirect loop without a code")

    @staticmethod
    def _code_from_url(url: str | None) -> str | None:
        if not url:
            return None
        query = parse_qs(urlparse(url).query)
        codes = query.get("code")
        return codes[0] if codes else None

    # ---- transport -------------------------------------------------------

    def _request(self, method: str, path: str, *, retry_auth: bool = True, **kwargs: Any) -> Any:
        if not self.rest_url or not self.rest_token:
            self.login()
        assert self.rest_url
        headers = dict(kwargs.pop("headers", {}) or {})
        headers["BhRestToken"] = self.rest_token
        resp = self.session.request(method, self.rest_url + path, headers=headers, timeout=self.timeout, **kwargs)
        if resp.status_code == 401 and retry_auth:
            log.info("Bullhorn session expired, logging in again")
            self.login()
            return self._request(method, path, retry_auth=False, headers=headers, **kwargs)
        if resp.status_code >= 400:
            raise BullhornError(f"{method} {path} failed: HTTP {resp.status_code} {resp.text[:500]}")
        try:
            return resp.json()
        except ValueError as exc:
            raise BullhornError(f"{method} {path} returned non-JSON: {resp.text[:200]}") from exc

    # ---- reads -----------------------------------------------------------

    def get_record(self, entity: str, record_id: int) -> RecordRef:
        if entity not in PERSON_FIELDS:
            raise BullhornError(f"unsupported entity {entity}")
        body = self._request("GET", f"entity/{entity}/{record_id}", params={"fields": PERSON_FIELDS[entity]})
        data = body.get("data")
        if not data or data.get("isDeleted"):
            raise BullhornError(f"{entity} {record_id} not found or deleted")
        return record_from_data(entity, data)

    def search_by_email(self, email: str) -> list[RecordRef]:
        """Exact (case-insensitive) email match across Candidate and ClientContact.

        Lucene does the lookup; the returned rows are then re-checked for exact
        equality so analyser quirks can never produce a partial match.
        """
        email = email.strip().lower()
        if not email:
            return []
        quoted = lucene_quote(email)
        query = " OR ".join(f"{f}:{quoted}" for f in EMAIL_FIELDS)
        results: list[RecordRef] = []
        for entity in ("Candidate", "ClientContact"):
            for data in self._search(entity, query):
                emails = {str(data.get(f) or "").strip().lower() for f in EMAIL_FIELDS}
                if email in emails:
                    results.append(record_from_data(entity, data))
        return results

    def search_by_name(self, full_name: str, limit: int = 10) -> list[RecordRef]:
        """Name search. Every result is low confidence by definition."""
        parts = [p for p in full_name.strip().split() if p]
        if not parts:
            return []
        if len(parts) >= 2:
            query = f"firstName:{lucene_quote(parts[0])} AND lastName:{lucene_quote(parts[-1])}"
        else:
            query = f"name:{lucene_quote(parts[0])}"
        results: list[RecordRef] = []
        for entity in ("Candidate", "ClientContact"):
            for data in self._search(entity, query, count=limit):
                results.append(record_from_data(entity, data))
        return results[: limit * 2]

    def _search(self, entity: str, query: str, count: int = 20) -> list[dict[str, Any]]:
        query = f"({query}) AND isDeleted:false"
        body = self._request(
            "GET",
            f"search/{entity}",
            params={"query": query, "fields": PERSON_FIELDS[entity], "count": count, "sort": "-dateLastModified"},
        )
        return [d for d in body.get("data", []) if not d.get("isDeleted")]

    def job_order_suggestions(self, record: RecordRef, limit: int = 10) -> list[dict[str, Any]]:
        """Job orders the record is attached to, for the confirmation view."""
        if record.entity == "Candidate":
            body = self._request(
                "GET",
                "query/JobSubmission",
                params={
                    "where": f"candidate.id={record.id} AND isDeleted=false",
                    "fields": "id,status,dateAdded,jobOrder(id,title,clientCorporation(name))",
                    "orderBy": "-dateAdded",
                    "count": limit,
                },
            )
            out = []
            for sub in body.get("data", []):
                jo = sub.get("jobOrder") or {}
                if not jo.get("id"):
                    continue
                corp = (jo.get("clientCorporation") or {}).get("name")
                out.append({"id": jo["id"], "title": jo.get("title") or "", "company": corp, "status": sub.get("status")})
            return out
        body = self._request(
            "GET",
            "query/JobOrder",
            params={
                "where": f"clientContact.id={record.id} AND isDeleted=false AND isOpen=true",
                "fields": "id,title,status,clientCorporation(name)",
                "orderBy": "-dateAdded",
                "count": limit,
            },
        )
        return [
            {"id": jo["id"], "title": jo.get("title") or "", "company": (jo.get("clientCorporation") or {}).get("name"), "status": jo.get("status")}
            for jo in body.get("data", [])
            if jo.get("id")
        ]

    def get_note(self, note_id: int) -> dict[str, Any]:
        body = self._request("GET", f"entity/Note/{note_id}", params={"fields": "id,action,comments,dateAdded,personReference(id),jobOrder(id)"})
        return body.get("data") or {}

    # ---- writes ----------------------------------------------------------

    def create_note(self, record: RecordRef, comments: str, action: str, job_order_id: int | None = None) -> int:
        """Create a Note attached to the person record (and optionally a job order).
        Returns the new note id."""
        if not comments.strip():
            raise BullhornError("refusing to write an empty note")
        payload: dict[str, Any] = {
            "action": action,
            "comments": comments,
            "personReference": {"id": record.id},
        }
        if job_order_id:
            payload["jobOrder"] = {"id": int(job_order_id)}
        body = self._request("PUT", "entity/Note", json=payload)
        note_id = body.get("changedEntityId")
        if not note_id:
            raise BullhornError(f"note write returned no changedEntityId: {body}")
        log.info("wrote Bullhorn note %s on %s (action=%s, jobOrder=%s)", note_id, record.ref, action, job_order_id)
        return int(note_id)
