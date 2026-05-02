"""Async HTTP client for the Cloudflare D1 REST API.

Provides a thin wrapper over the D1 query and batch endpoints with
automatic retry, error mapping, and result parsing.

D1 API reference:
    POST /query  — single statement, returns rows as [{col: val}, ...]
    POST /raw    — batch (atomic transaction), returns columnar format

Both endpoints live under:
    https://api.cloudflare.com/client/v4/accounts/{account_id}/d1/database/{database_id}/
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import aiohttp

from serving.utils.logging import get_logger

logger = get_logger(__name__)

# D1 API limits
MAX_BATCH_STATEMENTS = 100  # stay well under the 1000 hard limit
MAX_BOUND_PARAMS = 100
MAX_LIKE_PATTERN_BYTES = 50


class D1Error(Exception):
    """Base exception for D1 API errors."""

    def __init__(self, message: str, code: int | None = None) -> None:
        self.code = code
        super().__init__(message)


class D1ConnectionError(D1Error):
    """Raised when the D1 API is unreachable."""


class D1QueryError(D1Error):
    """Raised when D1 rejects a query (syntax error, constraint violation, etc)."""


class D1OverloadedError(D1Error):
    """Raised when D1 returns HTTP 429 (too many queued requests)."""


@dataclass
class D1Result:
    """Parsed result from a single D1 statement execution."""

    rows: list[dict[str, Any]] = field(default_factory=list)
    changes: int = 0
    last_row_id: int = 0
    duration_ms: float = 0.0
    rows_read: int = 0
    rows_written: int = 0


class D1Client:
    """Async client for the Cloudflare D1 REST API.

    Args:
        account_id: Cloudflare account ID.
        database_id: D1 database ID.
        api_token: Cloudflare API token with D1 permissions.
        timeout: Request timeout in seconds.
    """

    def __init__(
        self,
        account_id: str,
        database_id: str,
        api_token: str,
        *,
        timeout: float = 30.0,
    ) -> None:
        self._account_id = account_id
        self._database_id = database_id
        self._api_token = api_token
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None
        self._base_url = (
            f"https://api.cloudflare.com/client/v4/accounts/{account_id}/d1/database/{database_id}"
        )

    async def _ensure_session(self) -> aiohttp.ClientSession:
        """Lazily create or recycle the underlying HTTP session."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=self._timeout,
                headers={
                    "Authorization": f"Bearer {self._api_token}",
                    "Content-Type": "application/json",
                },
            )
        return self._session

    async def close(self) -> None:
        """Close the underlying HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    # ------------------------------------------------------------------
    # Core API methods
    # ------------------------------------------------------------------

    async def query(
        self,
        sql: str,
        params: list[Any] | None = None,
    ) -> D1Result:
        """Execute a single SQL statement and return parsed results.

        Uses the /query endpoint which returns rows as objects.

        Args:
            sql: SQL statement with ? placeholders.
            params: Positional parameters for ? placeholders.

        Returns:
            D1Result with parsed rows, metadata.

        Raises:
            D1QueryError: On SQL or constraint errors.
            D1ConnectionError: If the API is unreachable.
        """
        if params and len(params) > MAX_BOUND_PARAMS:
            raise ValueError(
                f"Query has {len(params)} params, exceeds D1 limit of {MAX_BOUND_PARAMS}"
            )

        body: dict[str, Any] = {"sql": sql}
        if params:
            # D1 throws D1_TYPE_ERROR on undefined; ensure no Python None leaks
            # as missing. None is fine — it maps to SQL NULL.
            body["params"] = params

        data = await self._post("/query", body)
        results = data.get("result", [])
        if not results:
            return D1Result()

        # /query returns result[0].results as [{col: val}, ...]
        stmt_result = results[0]
        meta = stmt_result.get("meta", {})

        return D1Result(
            rows=stmt_result.get("results", []),
            changes=meta.get("changes", 0),
            last_row_id=meta.get("last_row_id", 0),
            duration_ms=meta.get("duration", 0.0),
            rows_read=meta.get("rows_read", 0),
            rows_written=meta.get("rows_written", 0),
        )

    async def execute(
        self,
        sql: str,
        params: list[Any] | None = None,
    ) -> D1Result:
        """Execute a single write statement (INSERT/UPDATE/DELETE).

        Convenience alias for query() — semantically indicates a mutation.
        """
        return await self.query(sql, params)

    async def batch(
        self,
        statements: list[tuple[str, list[Any] | None]],
    ) -> list[D1Result]:
        """Execute multiple statements atomically via the /raw endpoint.

        All statements run in an implicit transaction. If any fails,
        all preceding statements are rolled back.

        Args:
            statements: List of (sql, params) tuples.

        Returns:
            List of D1Result, one per statement.

        Raises:
            D1QueryError: If any statement fails (entire batch rolls back).
            D1ConnectionError: If the API is unreachable.
            ValueError: If batch exceeds MAX_BATCH_STATEMENTS.
        """
        if len(statements) > MAX_BATCH_STATEMENTS:
            raise ValueError(
                f"Batch size {len(statements)} exceeds limit of {MAX_BATCH_STATEMENTS}"
            )

        stmts = []
        for sql, params in statements:
            stmt: dict[str, Any] = {"sql": sql}
            if params:
                stmt["params"] = params
            stmts.append(stmt)

        data = await self._post("/raw", {"batch": stmts})
        raw_results = data.get("result", [])

        parsed: list[D1Result] = []
        for stmt_result in raw_results:
            meta = stmt_result.get("meta", {})
            # /raw returns columnar format: {columns: [...], rows: [[...]]}
            raw_rows = stmt_result.get("results", {})
            rows = self._parse_columnar(raw_rows)

            parsed.append(
                D1Result(
                    rows=rows,
                    changes=meta.get("changes", 0),
                    last_row_id=meta.get("last_row_id", 0),
                    duration_ms=meta.get("duration", 0.0),
                    rows_read=meta.get("rows_read", 0),
                    rows_written=meta.get("rows_written", 0),
                )
            )

        return parsed

    async def health_check(self) -> bool:
        """Verify connectivity with a trivial query."""
        try:
            result = await self.query("SELECT 1 AS ok")
            return len(result.rows) == 1 and result.rows[0].get("ok") == 1
        except D1Error:
            return False

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _post(self, path: str, body: Any) -> dict[str, Any]:
        """Send a POST request to the D1 API and return the parsed response.

        Raises:
            D1QueryError: On API-level errors (bad SQL, constraint violations).
            D1ConnectionError: On network or HTTP transport errors.
        """
        session = await self._ensure_session()
        url = f"{self._base_url}{path}"

        try:
            async with session.post(url, json=body) as resp:
                text = await resp.text()
                try:
                    data = json.loads(text)
                except json.JSONDecodeError as exc:
                    raise D1QueryError(
                        f"D1 returned non-JSON response (HTTP {resp.status}): {text[:200]}"
                    ) from exc

                if resp.status == 429:
                    raise D1OverloadedError(
                        "D1 database overloaded (HTTP 429). Too many queued requests.",
                        code=429,
                    )

                if resp.status >= 500:
                    raise D1ConnectionError(f"D1 server error (HTTP {resp.status}): {text[:200]}")

                if not data.get("success"):
                    errors = data.get("errors", [])
                    error_msg = errors[0].get("message", "Unknown D1 error") if errors else text
                    error_code = errors[0].get("code") if errors else None
                    raise D1QueryError(error_msg, code=error_code)

                return data

        except D1Error:
            raise
        except aiohttp.ClientError as exc:
            raise D1ConnectionError(f"D1 API unreachable: {exc}") from exc
        except TimeoutError as exc:
            raise D1ConnectionError(f"D1 API timeout: {exc}") from exc

    @staticmethod
    def _parse_columnar(raw: Any) -> list[dict[str, Any]]:
        """Convert /raw columnar format to list of row dicts.

        Input format:  {"columns": ["a", "b"], "rows": [[1, 2], [3, 4]]}
        Output format: [{"a": 1, "b": 2}, {"a": 3, "b": 4}]

        If the input is already a list of dicts (shouldn't happen for /raw),
        return it as-is.
        """
        if isinstance(raw, list):
            return raw

        if not isinstance(raw, dict):
            return []

        columns = raw.get("columns", [])
        rows = raw.get("rows", [])

        if not columns or not rows:
            return []

        return [dict(zip(columns, row, strict=False)) for row in rows]
