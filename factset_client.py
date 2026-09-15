#!/usr/bin/env python3
"""FactSet API client.

Authenticates with OAuth 2.0 client credentials (a FactSet *confidential
client*) and wraps the endpoints this repo cares about: Global Prices,
Fundamentals, and a generic escape hatch for anything else under
``api.factset.com``.

Credentials are the JSON config downloaded from developer.factset.com when an
application is registered. FactSet issues confidential clients in two flavours
and the portal's Type column says which you have; both are supported:

* **Key Pair** — the config carries an RSA private key in a ``jwk`` block, and
  the token request is a signed JWT. Needs FactSet's signing helper.
* **Client Secret** — the config carries a shared secret instead. The token
  request is a plain form post, so this flavour needs nothing beyond requests.
  The secret can live in the config file as ``clientSecret`` or, better, in
  ``$FACTSET_CLIENT_SECRET``, keeping it out of the file altogether.

Either way the config is a secret: keep it outside the repo, or at least
outside version control. Nothing here logs, prints or echoes its contents.

The file is located, in order, from:

1. the ``config_path`` argument,
2. ``$FACTSET_CONFIG_PATH``,
3. ``./factset.json``,
4. ``~/.factset/config.json``.

Typical use::

    from factset_client import FactSetClient, asx_symbols

    with FactSetClient() as fs:
        prices = fs.prices(asx_symbols(["SXE", "SLC", "IPG"]),
                           start_date="2024-01-01")

Every wrapper returns a pandas DataFrame and splits large id lists into
batches of 50, which is the Global Prices ceiling for multi-day requests, so
scanning a full universe is a single call.

A Key Pair application additionally needs FactSet's signing helper, which is
not part of the base install::

    pip install -r requirements-factset.txt

To check credentials, connectivity and entitlements in one go::

    python factset_client.py --check
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Sequence

import pandas as pd
import requests

__all__ = [
    "FactSetClient",
    "FactSetError",
    "FactSetConfigError",
    "FactSetAuthError",
    "FactSetAPIError",
    "load_config",
    "find_config_path",
    "credential_flavour",
    "resolve_client_secret",
    "build_token_provider",
    "confidential_token_provider",
    "client_secret_token_provider",
    "asx_symbol",
    "asx_symbols",
]

# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class FactSetError(Exception):
    """Base class for every error raised by this module."""


class FactSetConfigError(FactSetError):
    """The credentials file is missing, unreadable or incomplete."""


class FactSetAuthError(FactSetError):
    """Authentication or entitlement failure — no token, or a 401/403 reply."""


class FactSetAPIError(FactSetError):
    """FactSet returned a non-success status."""

    def __init__(self, status_code: int, message: str, url: str,
                 request_key: str | None = None) -> None:
        self.status_code = status_code
        self.message = message
        self.url = url
        self.request_key = request_key
        detail = f"FactSet {status_code} for {url}: {message}"
        if request_key:
            detail += f" (request key {request_key})"
        super().__init__(detail)


# --------------------------------------------------------------------------- #
# Credentials
# --------------------------------------------------------------------------- #

CONFIG_ENV_VAR = "FACTSET_CONFIG_PATH"
CLIENT_SECRET_ENV_VAR = "FACTSET_CLIENT_SECRET"

# FactSet registers a confidential client in one of two shapes, and the
# developer portal names which one you have under Type:
#
#   "... Machine Authorization (Key Pair)"      -> a 'jwk' block holding an
#                                                  RSA private key. The token
#                                                  request is a signed JWT.
#   "... Machine Authorization (Client Secret)" -> a shared secret string,
#                                                  sent to the token endpoint.
#
# Both are the OAuth 2.0 client-credentials grant and both are handled here.
# The key-pair flow needs fds.sdk.utils to do the signing; the client-secret
# flow is a plain form post, so it needs nothing beyond requests.
REQUIRED_JWK_KEYS = ("kty", "alg", "use", "kid", "n", "e", "d",
                     "p", "q", "dp", "dq", "qi")

DEFAULT_WELL_KNOWN_URI = "https://auth.factset.com/.well-known/openid-configuration"

# Renew a little before the server's expiry so a token cannot go stale in flight.
TOKEN_EXPIRY_MARGIN_SECS = 30


def _default_config_paths() -> tuple[Path, ...]:
    return (Path("factset.json"), Path.home() / ".factset" / "config.json")


def find_config_path(explicit: str | os.PathLike | None = None) -> Path:
    """Locate the credentials file. Raises FactSetConfigError if there isn't one."""
    if explicit is not None:
        path = Path(explicit)
        if not path.is_file():
            raise FactSetConfigError(f"No FactSet credentials file at {path}")
        return path

    from_env = os.environ.get(CONFIG_ENV_VAR)
    if from_env:
        path = Path(from_env)
        if not path.is_file():
            raise FactSetConfigError(
                f"{CONFIG_ENV_VAR} points at {path}, which is not a file"
            )
        return path

    for candidate in _default_config_paths():
        if candidate.is_file():
            return candidate

    searched = ", ".join(str(p) for p in _default_config_paths())
    raise FactSetConfigError(
        "No FactSet credentials found. Download the OAuth config for your "
        "application from developer.factset.com, then either set "
        f"{CONFIG_ENV_VAR} to its path or save it as one of: {searched}"
    )


def resolve_client_secret(config: dict[str, Any]) -> str | None:
    """The client secret, from the config file or ``$FACTSET_CLIENT_SECRET``.

    The environment variable wins, so the secret can be kept out of the file
    entirely — the portal shows it once, separately from the JSON.
    """
    from_env = os.environ.get(CLIENT_SECRET_ENV_VAR)
    if from_env:
        return from_env
    secret = config.get("clientSecret") or config.get("client_secret")
    return str(secret) if secret else None


def credential_flavour(config: dict[str, Any]) -> str:
    """Which flow this config calls for: 'key-pair' or 'client-secret'."""
    return "key-pair" if config.get("jwk") else "client-secret"


def load_config(path: str | os.PathLike | None = None) -> dict[str, Any]:
    """Read and validate the OAuth config.

    Accepts either flavour. The returned dict holds credential material, so
    it is never logged or echoed.
    """
    config_path = find_config_path(path)
    try:
        with open(config_path, "r", encoding="utf-8") as handle:
            config = json.load(handle)
    except json.JSONDecodeError as exc:
        raise FactSetConfigError(f"{config_path} is not valid JSON: {exc}") from exc
    except OSError as exc:
        raise FactSetConfigError(f"Cannot read {config_path}: {exc}") from exc

    if not isinstance(config, dict):
        raise FactSetConfigError(f"{config_path} should contain a JSON object")

    if not config.get("clientId"):
        raise FactSetConfigError(f"{config_path} is missing 'clientId'")

    jwk = config.get("jwk")
    if jwk is not None:
        if not isinstance(jwk, dict):
            raise FactSetConfigError(f"{config_path} has a 'jwk' that is not an object")
        missing = [key for key in REQUIRED_JWK_KEYS if key not in jwk]
        if missing:
            raise FactSetConfigError(
                f"{config_path} has an incomplete 'jwk' block, missing: "
                f"{', '.join(missing)}"
            )
        return config

    if resolve_client_secret(config) is None:
        raise FactSetConfigError(
            f"{config_path} has neither a 'jwk' block nor a 'clientSecret'. "
            "Check the Type column on developer.factset.com's API Authentication "
            "page: a Key Pair application needs the 'jwk' block that FactSet "
            "includes in the config it generates at creation; a Client Secret "
            "application needs the secret, either as 'clientSecret' in this file "
            f"or in ${CLIENT_SECRET_ENV_VAR}."
        )

    return config


# --------------------------------------------------------------------------- #
# Token providers
# --------------------------------------------------------------------------- #


def _discover_token_endpoint(
    session: requests.Session, well_known_uri: str, timeout: float
) -> str:
    """Read the authorization server metadata to find the token endpoint."""
    try:
        response = session.get(well_known_uri, timeout=timeout)
    except requests.RequestException as exc:
        raise FactSetAuthError(
            f"Could not reach the FactSet authorization server at "
            f"{well_known_uri}: {exc}"
        ) from exc

    if response.status_code != 200:
        raise FactSetAuthError(
            f"FactSet {response.status_code} from {well_known_uri}: "
            f"{_error_message(response)}"
        )

    try:
        metadata = response.json()
    except ValueError as exc:
        raise FactSetAuthError(
            f"{well_known_uri} did not return JSON metadata"
        ) from exc

    endpoint = metadata.get("token_endpoint")
    if not endpoint:
        raise FactSetAuthError(
            f"{well_known_uri} metadata has no 'token_endpoint'"
        )
    return str(endpoint)


def client_secret_token_provider(
    config: dict[str, Any] | None = None,
    config_path: str | os.PathLike | None = None,
    session: requests.Session | None = None,
    timeout: float = 30.0,
    clock: Callable[[], float] = time.time,
) -> Callable[[], str]:
    """Build a bearer-token callable for a *client secret* confidential client.

    A plain client-credentials form post — no JWT signing, so this path has no
    dependency beyond requests. The token is cached and renewed shortly before
    it expires, so calling the result per request is cheap.

    FactSet's authorization server accepts the secret either in the form body
    (``client_secret_post``) or as HTTP Basic (``client_secret_basic``)
    depending on how the application was registered. This tries the form body
    first and falls back to Basic on a 401, so either registration works.
    """
    if config is None:
        config = load_config(config_path)

    client_id = config.get("clientId")
    if not client_id:
        raise FactSetConfigError("Configuration is missing 'clientId'")

    secret = resolve_client_secret(config)
    if secret is None:
        raise FactSetConfigError(
            "No client secret found. Add 'clientSecret' to the configuration "
            f"or set ${CLIENT_SECRET_ENV_VAR}."
        )

    well_known_uri = config.get("wellKnownUri") or DEFAULT_WELL_KNOWN_URI
    http = session or requests.Session()
    cache: dict[str, Any] = {}
    endpoint: dict[str, str] = {}

    def _fetch(use_basic: bool) -> requests.Response:
        body = {"grant_type": "client_credentials"}
        auth = None
        if use_basic:
            auth = (client_id, secret)
        else:
            body["client_id"] = client_id
            body["client_secret"] = secret
        return http.post(
            endpoint["url"],
            data=body,
            auth=auth,
            headers={"Accept": "application/json"},
            timeout=timeout,
        )

    def get_token() -> str:
        if cache and clock() < cache["expires_at"] - TOKEN_EXPIRY_MARGIN_SECS:
            return cache["access_token"]

        if "url" not in endpoint:
            endpoint["url"] = _discover_token_endpoint(
                http, well_known_uri, timeout
            )

        try:
            response = _fetch(use_basic=False)
            if response.status_code == 401:
                response = _fetch(use_basic=True)
        except requests.RequestException as exc:
            raise FactSetAuthError(
                f"Could not reach the FactSet token endpoint: {exc}"
            ) from exc

        if response.status_code != 200:
            raise FactSetAuthError(
                f"FactSet refused the client-credentials request "
                f"({response.status_code}): {_error_message(response)}. "
                "Check the client id and secret, and that the application is "
                "still present on developer.factset.com."
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise FactSetAuthError(
                "The FactSet token endpoint did not return JSON"
            ) from exc

        token = payload.get("access_token")
        if not token:
            raise FactSetAuthError(
                "The FactSet token response contained no 'access_token'"
            )

        expires_in = payload.get("expires_in")
        try:
            lifetime = float(expires_in)
        except (TypeError, ValueError):
            lifetime = 300.0

        cache["access_token"] = str(token)
        cache["expires_at"] = clock() + lifetime
        return cache["access_token"]

    get_token.expires_at = lambda: cache.get("expires_at")  # type: ignore[attr-defined]
    return get_token


def confidential_token_provider(
    config: dict[str, Any] | None = None,
    config_path: str | os.PathLike | None = None,
    proxy: str | None = None,
    ssl_ca_cert: str | None = None,
) -> Callable[[], str]:
    """Build a bearer-token callable for a *key pair* confidential client.

    Wraps ``fds.sdk.utils``' ConfidentialClient, which signs a JWS with the
    private key from the config and exchanges it for an access token. That
    client caches the token and renews it 30 seconds before expiry.
    """
    try:
        from fds.sdk.utils.authentication import ConfidentialClient
    except ImportError as exc:  # pragma: no cover - depends on install state
        raise FactSetConfigError(
            "This configuration uses a key pair, which needs FactSet's signing "
            "helper. Run: pip install -r requirements-factset.txt"
        ) from exc

    if config is None:
        config = load_config(config_path)

    try:
        client = ConfidentialClient(
            config=config, proxy=proxy, ssl_ca_cert=ssl_ca_cert
        )
    except Exception as exc:
        raise FactSetAuthError(
            f"Could not set up the FactSet OAuth client: {exc}"
        ) from exc

    def get_token() -> str:
        try:
            return client.get_access_token()
        except Exception as exc:
            raise FactSetAuthError(
                f"Could not obtain a FactSet access token: {exc}"
            ) from exc

    return get_token


def build_token_provider(
    config: dict[str, Any] | None = None,
    config_path: str | os.PathLike | None = None,
    proxy: str | None = None,
    ssl_ca_cert: str | None = None,
    session: requests.Session | None = None,
) -> Callable[[], str]:
    """Pick the right token provider for whichever credential flavour is configured."""
    if config is None:
        config = load_config(config_path)

    if credential_flavour(config) == "key-pair":
        return confidential_token_provider(
            config=config, proxy=proxy, ssl_ca_cert=ssl_ca_cert
        )
    return client_secret_token_provider(config=config, session=session)


# --------------------------------------------------------------------------- #
# Symbols
# --------------------------------------------------------------------------- #


def asx_symbol(code: str) -> str:
    """Turn an ASX code into a FactSet ticker-region symbol: 'BHP' -> 'BHP-AU'."""
    text = str(code).strip().upper()
    if not text:
        raise ValueError("Empty ASX code")
    if "-" in text:
        return text
    return f"{text}-AU"


def asx_symbols(codes: Iterable[str]) -> list[str]:
    """Map a list of ASX codes to FactSet symbols, preserving order."""
    return [asx_symbol(code) for code in codes]


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #

DEFAULT_BASE_URL = "https://api.factset.com"

# FactSet's content APIs sit under a /content prefix. Without it the gateway
# has no route to match and answers with an HTML error page rather than a JSON
# API error, which is what a 404 carrying markup means here.
GLOBAL_PRICES = "/content/factset-global-prices/v1"
FUNDAMENTALS = "/content/factset-fundamentals/v2"

# Global Prices caps a multi-day request at 50 ids.
MAX_IDS_PER_REQUEST = 50

RETRY_STATUS = frozenset({429, 500, 502, 503, 504})


def _chunked(items: Sequence[str], size: int) -> Iterator[list[str]]:
    for start in range(0, len(items), size):
        yield list(items[start:start + size])


def _as_id_list(ids: str | Iterable[str]) -> list[str]:
    if isinstance(ids, str):
        return [ids]
    listed = [str(item) for item in ids]
    if not listed:
        raise ValueError("No identifiers given")
    return listed


def _drop_none(body: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in body.items() if value is not None}


class FactSetClient:
    """A session against api.factset.com with OAuth bearer auth.

    ``token_provider`` and ``session`` exist to be injected in tests; leave them
    unset and the client builds a confidential-client provider from the
    credentials file and a plain requests session.
    """

    def __init__(
        self,
        *,
        config_path: str | os.PathLike | None = None,
        config: dict[str, Any] | None = None,
        token_provider: Callable[[], str] | None = None,
        session: requests.Session | None = None,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 60.0,
        max_retries: int = 3,
        backoff: float = 1.0,
        proxy: str | None = None,
        ssl_ca_cert: str | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff = backoff
        self._sleep = sleep
        self._owns_session = session is None
        self._session = session or requests.Session()
        if proxy:
            self._session.proxies.update({"http": proxy, "https": proxy})
        if ssl_ca_cert:
            self._session.verify = ssl_ca_cert

        if token_provider is not None:
            self._token_provider = token_provider
        else:
            self._token_provider = build_token_provider(
                config=config,
                config_path=config_path,
                proxy=proxy,
                ssl_ca_cert=ssl_ca_cert,
            )

    # -- lifecycle ---------------------------------------------------------- #

    def close(self) -> None:
        if self._owns_session:
            self._session.close()

    def __enter__(self) -> "FactSetClient":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # -- transport ---------------------------------------------------------- #

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Issue one request and return the decoded JSON body.

        Retries 429 and 5xx up to ``max_retries``, honouring Retry-After when
        the server sends one. 401 and 403 are raised immediately — they mean
        the credentials or entitlements are wrong, and retrying won't fix it.
        """
        url = f"{self.base_url}{path}"
        headers = {
            "Authorization": f"Bearer {self._token_provider()}",
            "Accept": "application/json",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"

        last_error: FactSetAPIError | None = None
        for attempt in range(self.max_retries + 1):
            response = self._session.request(
                method,
                url,
                params=params,
                json=body,
                headers=headers,
                timeout=self.timeout,
            )
            status = response.status_code

            if 200 <= status < 300:
                if status == 204 or not response.content:
                    return {}
                try:
                    return response.json()
                except ValueError as exc:
                    raise FactSetAPIError(
                        status, f"response was not JSON: {exc}", url,
                        _request_key(response),
                    ) from exc

            if status in (401, 403):
                raise FactSetAuthError(
                    f"FactSet {status} for {url}: {_error_message(response)}. "
                    "Check the credentials file and that your account is "
                    "entitled to this endpoint."
                )

            message = _error_message(response)
            if status == 404:
                message += (" — check the endpoint path; FactSet content APIs "
                            "live under /content")
            last_error = FactSetAPIError(
                status, message, url, _request_key(response)
            )

            if status in RETRY_STATUS and attempt < self.max_retries:
                self._sleep(_retry_delay(response, attempt, self.backoff))
                continue
            raise last_error

        raise last_error  # pragma: no cover - loop always returns or raises

    def get(self, path: str, **params: Any) -> dict[str, Any]:
        """GET any FactSet path, e.g. ``get("/factset-global-prices/v1/prices", ids="BHP-AU")``."""
        return self.request("GET", path, params=_drop_none(params))

    def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        """POST any FactSet path with a JSON body."""
        return self.request("POST", path, body=body)

    # -- batching ----------------------------------------------------------- #

    def _batched_post(
        self, path: str, ids: list[str], payload: dict[str, Any]
    ) -> pd.DataFrame:
        """POST once per batch of 50 ids and stack the ``data`` arrays."""
        frames: list[pd.DataFrame] = []
        for batch in _chunked(ids, MAX_IDS_PER_REQUEST):
            body = {"data": {**_drop_none(payload), "ids": batch}}
            rows = self.post(path, body).get("data") or []
            if rows:
                frames.append(pd.json_normalize(rows))
        if not frames:
            return pd.DataFrame()
        return pd.concat(frames, ignore_index=True)

    # -- Global Prices ------------------------------------------------------ #

    def prices(
        self,
        ids: str | Iterable[str],
        start_date: str,
        end_date: str | None = None,
        frequency: str = "D",
        currency: str | None = None,
        adjust: str = "SPLIT",
        calendar: str | None = None,
        fields: Sequence[str] | None = None,
    ) -> pd.DataFrame:
        """Open/high/low/close/volume history.

        Dates are YYYY-MM-DD. ``frequency`` is D, W, M, AM, AQ, CQ, AY or CY;
        ``adjust`` is SPLIT, SPLIT_SPINOFF, DIV_SPIN_SPLITS or UNSPLIT. Fields
        default to everything: price, priceOpen, priceHigh, priceLow, volume,
        turnover, vwap, tradeCount, currency.
        """
        return self._batched_post(
            f"{GLOBAL_PRICES}/prices",
            _as_id_list(ids),
            {
                "startDate": start_date,
                "endDate": end_date,
                "frequency": frequency,
                "currency": currency,
                "adjust": adjust,
                "calendar": calendar,
                "fields": list(fields) if fields else None,
            },
        )

    def returns(
        self,
        ids: str | Iterable[str],
        start_date: str,
        end_date: str,
        frequency: str = "D",
        currency: str | None = None,
        dividend_adjust: str | None = None,
    ) -> pd.DataFrame:
        """Period-by-period total returns. One row per id per period."""
        return self._batched_post(
            f"{GLOBAL_PRICES}/returns",
            _as_id_list(ids),
            {
                "startDate": start_date,
                "endDate": end_date,
                "frequency": frequency,
                "currency": currency,
                "dividendAdjust": dividend_adjust,
            },
        )

    def corporate_actions(
        self,
        ids: str | Iterable[str],
        start_date: str | None = None,
        end_date: str | None = None,
        event_category: str = "ALL",
        currency: str | None = None,
    ) -> pd.DataFrame:
        """Dividends, splits, spinoffs and rights.

        ``event_category`` is ALL, CASH_DIVS, STOCK_DIST, RIGHTS, SPINOFFS or
        SPLITS.
        """
        return self._batched_post(
            f"{GLOBAL_PRICES}/corporate-actions",
            _as_id_list(ids),
            {
                "startDate": start_date,
                "endDate": end_date,
                "eventCategory": event_category,
                "currency": currency,
            },
        )

    def shares_outstanding(
        self,
        ids: str | Iterable[str],
        start_date: str | None = None,
        end_date: str | None = None,
        frequency: str | None = None,
        calendar: str | None = None,
    ) -> pd.DataFrame:
        """Historical share counts — the other half of a market cap."""
        return self._batched_post(
            f"{GLOBAL_PRICES}/shares-outstanding",
            _as_id_list(ids),
            {
                "startDate": start_date,
                "endDate": end_date,
                "frequency": frequency,
                "calendar": calendar,
            },
        )

    # -- Fundamentals ------------------------------------------------------- #

    def fundamentals(
        self,
        ids: str | Iterable[str],
        metrics: Sequence[str],
        periodicity: str = "ANN",
        fiscal_period_start: str | None = None,
        fiscal_period_end: str | None = None,
        currency: str | None = None,
        update_type: str | None = None,
    ) -> pd.DataFrame:
        """Financial statement data for FF_* metric codes.

        Look codes up with :meth:`fundamentals_metrics` rather than guessing —
        an unknown code is rejected for the whole request. ``periodicity`` is
        ANN, QTR, SEMI, LTM or YTD, optionally with an ``_R`` restatement suffix.
        """
        if not metrics:
            raise ValueError("At least one metric code is required")
        return self._batched_post(
            f"{FUNDAMENTALS}/fundamentals",
            _as_id_list(ids),
            {
                "metrics": list(metrics),
                "periodicity": periodicity,
                "fiscalPeriodStart": fiscal_period_start,
                "fiscalPeriodEnd": fiscal_period_end,
                "currency": currency,
                "updateType": update_type,
            },
        )

    def fundamentals_metrics(
        self,
        category: str | None = None,
        subcategory: str | None = None,
    ) -> pd.DataFrame:
        """The FF_* metric catalogue, for looking up codes before a query."""
        payload = self.get(
            f"{FUNDAMENTALS}/metrics", category=category, subcategory=subcategory
        )
        rows = payload.get("data") or []
        return pd.json_normalize(rows) if rows else pd.DataFrame()


# --------------------------------------------------------------------------- #
# Response helpers
# --------------------------------------------------------------------------- #


def _request_key(response: requests.Response) -> str | None:
    """FactSet's per-request id, worth quoting when raising a support ticket."""
    headers = getattr(response, "headers", None) or {}
    return headers.get("x-datadirect-request-key")


def _looks_like_html(text: str) -> bool:
    head = text.lstrip()[:200].lower()
    return head.startswith(("<!doctype", "<html", "<head")) or "<html" in head


def _error_message(response: requests.Response) -> str:
    """Pull a readable message out of a FactSet error body.

    An HTML body means the gateway answered instead of the API — almost always
    a path it has no route for — so say that rather than quoting the markup.
    """
    try:
        payload = response.json()
    except Exception:
        text = (getattr(response, "text", "") or "").strip()
        if _looks_like_html(text):
            return ("the gateway returned an HTML error page rather than a JSON "
                    "API response, which usually means this path is not a "
                    "recognised endpoint")
        return text[:300] or "no response body"

    if isinstance(payload, dict):
        errors = payload.get("errors")
        if isinstance(errors, list) and errors:
            parts = []
            for error in errors:
                if not isinstance(error, dict):
                    continue
                title = error.get("title") or error.get("code") or ""
                detail = error.get("detail") or ""
                joined = ": ".join(part for part in (title, detail) if part)
                if joined:
                    parts.append(joined)
            if parts:
                return "; ".join(parts)
        for key in ("detail", "title", "message", "error_description"):
            if payload.get(key):
                return str(payload[key])

    return (getattr(response, "text", "") or "").strip()[:300] or "no response body"


def _retry_delay(response: requests.Response, attempt: int, backoff: float) -> float:
    """Retry-After when the server sets one, else exponential backoff."""
    headers = getattr(response, "headers", None) or {}
    retry_after = headers.get("Retry-After")
    if retry_after:
        try:
            return max(0.0, float(retry_after))
        except (TypeError, ValueError):
            pass
    return backoff * (2 ** attempt)


# --------------------------------------------------------------------------- #
# Self check
# --------------------------------------------------------------------------- #


def check(config_path: str | os.PathLike | None = None,
          symbol: str = "BHP-AU") -> int:
    """Verify credentials, connectivity and entitlements. Returns an exit code.

    Prints what it found at each step. The access token is never printed — only
    the fact that one was obtained, and when it expires.
    """
    import datetime as _dt

    def ok(message: str) -> None:
        print(f"  ok    {message}")

    def fail(message: str) -> None:
        print(f"  FAIL  {message}")

    print("FactSet connection check")

    # 1. Credentials
    print("\n1. Credentials")
    try:
        path = find_config_path(config_path)
        config = load_config(path)
    except FactSetConfigError as exc:
        fail(str(exc))
        return 1
    flavour = credential_flavour(config)
    ok(f"config at {path}")
    ok(f"client id {config['clientId']}")
    ok(f"type: {flavour}")
    if flavour == "client-secret":
        source = ("$" + CLIENT_SECRET_ENV_VAR
                  if os.environ.get(CLIENT_SECRET_ENV_VAR) else "the config file")
        ok(f"secret found in {source}")

    # 2. Token
    print("\n2. Authentication")
    try:
        provider = build_token_provider(config=config)
        token = provider()
    except (FactSetAuthError, FactSetConfigError) as exc:
        fail(str(exc))
        return 1
    if not token:
        fail("no access token returned")
        return 1
    ok("access token obtained")
    expires_at = getattr(provider, "expires_at", lambda: None)()
    if expires_at:
        when = _dt.datetime.fromtimestamp(expires_at).strftime("%H:%M:%S")
        ok(f"expires at {when} ({int(expires_at - time.time())}s)")

    # 3. Entitlements
    print(f"\n3. Data access ({symbol})")
    start = (_dt.date.today() - _dt.timedelta(days=10)).isoformat()
    try:
        with FactSetClient(config=config) as client:
            frame = client.prices(symbol, start_date=start)
    except FactSetAuthError as exc:
        fail(str(exc))
        print("\nAuthentication worked but this endpoint is not entitled. "
              "Ask your FactSet account team about Global Prices.")
        return 1
    except FactSetError as exc:
        fail(str(exc))
        return 1

    if frame.empty:
        fail(f"no rows for {symbol} since {start} — try another symbol")
        return 1
    ok(f"{len(frame)} rows returned for {symbol}")

    print("\nAll checks passed.")
    return 0


def probe(config_path: str | os.PathLike | None = None) -> int:
    """Report which endpoints this account is entitled to.

    Authentication is shared, so a 403 here is a licensing answer rather than a
    credentials one: the token was accepted and the endpoint refused it. A 400
    still counts as entitled — the API read the request and disliked its
    arguments, which it could only do after letting us in.
    """
    import datetime as _dt

    try:
        config = load_config(config_path)
    except FactSetConfigError as exc:
        print(f"error: {exc}")
        return 1

    today = _dt.date.today()
    recent = (today - _dt.timedelta(days=7)).isoformat()
    ident = ["IBM-US"]

    checks: list[tuple[str, str, str, dict[str, Any] | None]] = [
        ("Global Prices: prices", "POST", f"{GLOBAL_PRICES}/prices",
         {"data": {"ids": ident, "startDate": recent,
                   "endDate": today.isoformat(), "frequency": "D"}}),
        ("Global Prices: returns", "POST", f"{GLOBAL_PRICES}/returns",
         {"data": {"ids": ident, "startDate": recent,
                   "endDate": today.isoformat(), "frequency": "D"}}),
        ("Global Prices: corporate actions", "POST",
         f"{GLOBAL_PRICES}/corporate-actions",
         {"data": {"ids": ident, "startDate": recent,
                   "endDate": today.isoformat()}}),
        ("Global Prices: shares outstanding", "POST",
         f"{GLOBAL_PRICES}/shares-outstanding", {"data": {"ids": ident}}),
        ("Fundamentals: metrics", "GET", f"{FUNDAMENTALS}/metrics", None),
        ("Fundamentals: fundamentals", "POST", f"{FUNDAMENTALS}/fundamentals",
         {"data": {"ids": ident, "metrics": ["FF_SALES"],
                   "periodicity": "ANN"}}),
    ]

    print(f"Entitlement probe for {config['clientId']}\n")
    entitled: list[str] = []
    refused: list[str] = []

    with FactSetClient(config=config, max_retries=0) as client:
        for label, method, path, body in checks:
            try:
                client.request(method, path, body=body)
                verdict, bucket = "entitled", entitled
            except FactSetAuthError:
                # 401/403 after a good token means this endpoint is not licensed.
                verdict, bucket = "NOT entitled", refused
            except FactSetAPIError as exc:
                if exc.status_code == 400:
                    verdict, bucket = "entitled", entitled
                elif exc.status_code == 404:
                    verdict, bucket = "no such path", refused
                else:
                    verdict, bucket = f"error {exc.status_code}", refused
            except FactSetError as exc:
                print(f"  {label:36} could not check: {exc}")
                continue
            bucket.append(label)
            mark = "ok  " if bucket is entitled else "--  "
            print(f"  {mark}{label:36} {verdict}")

    print(f"\n{len(entitled)} of {len(checks)} endpoints available.")
    if refused and not entitled:
        print("\nAuthentication works, so this is a licensing question, not a "
              "credentials one. Quote the exact endpoint paths above to your "
              "FactSet account team.")
    return 0 if entitled else 1


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="FactSet API client. Run --check to verify a connection."
    )
    parser.add_argument("--check", action="store_true",
                        help="verify credentials, authentication and entitlements")
    parser.add_argument("--probe", action="store_true",
                        help="report which endpoints this account is entitled to")
    parser.add_argument("--config", default=None,
                        help="path to the FactSet OAuth config "
                             f"(default: ${CONFIG_ENV_VAR}, ./factset.json, "
                             "~/.factset/config.json)")
    parser.add_argument("--symbol", default="BHP-AU",
                        help="symbol used for the data-access check "
                             "(default: BHP-AU)")
    args = parser.parse_args(argv)

    if args.probe:
        return probe(config_path=args.config)
    if not args.check:
        parser.print_help()
        return 0
    return check(config_path=args.config, symbol=args.symbol)


if __name__ == "__main__":
    import sys as _sys
    _sys.exit(main())
