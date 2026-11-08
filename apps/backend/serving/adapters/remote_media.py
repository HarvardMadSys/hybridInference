"""Remote media on self-hosted routes: fetched by the gateway, sent on inline.

A chat message can name its media by URL: an ``image_url`` part, and for vLLM
and SGLang also ``video_url`` and ``audio_url``. The Anthropic surface turns a
``source.type: "url"`` image into one too. A provider API fetches such a URL
from its own network. A self-hosted engine fetches it from *ours*: vLLM and
SGLang download whatever a request names, from the GPU hosts, so any caller
could make them read internal services -- and learn from the errors which
addresses answer. SGLang also opens a local file for a "URL" that is a path
ending in an image extension.

So for a route the gateway classifies as self-hosted (``is_local_endpoint``,
the classification outbound admission uses), every media URL that is not a
``data:`` URL is fetched here, through a client that can reach only public
addresses, and replaced with a ``data:`` URL the engine decodes without any
network access. Cloud routes are left alone: their URLs travel as before.

The fetch accepts only ``http`` and ``https``. Every connection, redirects
included, is checked at connect time against the address actually being
connected -- the resolved one, so a name that resolves differently later gains
nothing -- and refused unless it is a public unicast address: loopback,
private, link-local, shared (CGNAT), reserved, multicast and cloud-metadata
addresses are refused, IPv4 embedded in IPv6 included. No proxy is taken from
the environment. The response must be 2xx, of the part's media type
(``image/*`` for an image, or a generic binary type whose first bytes are a
known image format), not content-encoded, and at most
:data:`MAX_MEDIA_BYTES`. A request's media together may be at most
:data:`MAX_TOTAL_MEDIA_BYTES` from at most :data:`MAX_REMOTE_MEDIA` URLs, all
fetched within :data:`FETCH_TIMEOUT_S`.

Anything else is refused with :class:`RemoteMediaRefused`, a 400 raised before
anything is sent upstream: the caller's input, never a fault of the endpoint.

What the fetcher may use of the process is bounded too, since the URLs are the
caller's choice. Host names are resolved on a small thread pool of its own
(:class:`_MediaResolver`): a name whose DNS never answers holds a thread for as
long as the system resolver waits, and on the event loop's default executor it
held a thread every other lookup in the process -- provider hosts, database
reconnects -- also needs. At most :data:`MAX_CALLER_FETCHES` fetches run at once
for one caller and :data:`MAX_GLOBAL_FETCHES` for all of them. What a request
fetched is kept for its later attempts (:class:`_MediaMemo`): a fallback to
another self-hosted route, or a hedge, does not fetch it again.
"""

from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import contextlib
import errno
import ipaddress
import re
import socket
import time
from collections import OrderedDict
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver, ResolveResult

from serving.utils import context as req_ctx
from serving.utils.logging import get_logger
from serving.utils.metadata_addresses import is_metadata_address

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

logger = get_logger(__name__)

__all__ = [
    "FETCH_TIMEOUT_S",
    "MAX_MEDIA_BYTES",
    "MAX_REMOTE_MEDIA",
    "MAX_TOTAL_MEDIA_BYTES",
    "RemoteMediaRefused",
    "inline_remote_media",
    "is_public_address",
]

#: The largest single media file fetched (the size OpenAI accepts per image).
MAX_MEDIA_BYTES = 20 * 1024 * 1024
#: All of one request's fetched media together: what the gateway's request body
#: limit lets a caller send inline itself.
MAX_TOTAL_MEDIA_BYTES = 32 * 1024 * 1024
#: Remote media URLs one request may name. A conversation repeats its images
#: every turn, so this is generous; it bounds how much fetching one small
#: request can cause.
MAX_REMOTE_MEDIA = 64
#: Time for all of one request's fetches.
FETCH_TIMEOUT_S = 30.0
#: Fetches running at once for one caller, across all of the caller's requests:
#: as many as one request runs.
MAX_CALLER_FETCHES = 4
#: Fetches running at once in the process.
MAX_GLOBAL_FETCHES = 16

_CONNECT_TIMEOUT_S = 10.0
_MAX_REDIRECTS = 3
_CONCURRENT_FETCHES = 4
_READ_CHUNK_BYTES = 64 * 1024
_USER_AGENT = "hybridinference-media/1.0"

#: Threads resolving media host names: the fetcher's own, never the event loop's
#: default executor.
_DNS_THREADS = 4
#: How long a fetch waits for one host name to resolve.
_DNS_TIMEOUT_S = 10.0
#: Lookups one caller may have running -- including any whose fetch gave up and
#: whose thread still waits on a resolver that does not answer -- so one caller
#: cannot hold every thread.
_DNS_LOOKUPS_PER_CALLER = 2

#: Bytes of media base64-encoded between two turns of the event loop: a
#: multiple of 3, so the pieces' encodings join into the whole one's.
_B64_SLICE_BYTES = 3 * 256 * 1024

#: How long what a request fetched is kept for its later attempts, and how
#: much of all requests' media is kept at most (characters of ``data:`` URLs).
_MEMO_TTL_S = 120.0
_MEMO_MAX_CHARS = 64 * 1024 * 1024

#: Content-part fields whose URL an engine fetches, and the media type the
#: fetched content must have.
_MEDIA_FIELDS = {"image_url": "image", "video_url": "video", "audio_url": "audio"}

_MEDIA_TYPE_RE = re.compile(r"^(image|video|audio)/[a-z0-9][a-z0-9.+-]*$")
# Storage buckets often serve an image under a generic type -- or none, which
# aiohttp reads as application/octet-stream. Such an image is recognized by its
# first bytes instead.
_GENERIC_TYPES = frozenset({"application/octet-stream", "binary/octet-stream"})
_IMAGE_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)
# A bare base64 payload, which SGLang decodes as an image: the base64 alphabet
# has no '.', ':' or '\', so no URL or file path with an extension matches.
_RAW_BASE64_RE = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")
# SGLang opens a value ending like this as a local file.
_FILE_LIKE_SUFFIXES = ("png", "jpg", "jpeg", "webp", "gif")

# IPv6 ranges that embed an IPv4 address a gateway could be routed to, in their
# last 32 bits: NAT64, IPv4-compatible (deprecated, but a host may still route
# it) and IPv4-translated (stateless translation, RFC 2765). Python's
# ``is_global`` calls the last two global whatever they embed.
_EMBEDS_IPV4 = (
    ipaddress.ip_network("64:ff9b::/96"),
    ipaddress.ip_network("::/96"),
    ipaddress.ip_network("::ffff:0:0:0/96"),
)


class RemoteMediaRefused(aiohttp.ClientResponseError):
    """A media URL on a self-hosted route the gateway would not, or could not, fetch.

    A 400, raised before anything is sent upstream. An ``aiohttp``
    ``ClientResponseError`` so every caller handles it as the upstream 400 it
    stands in for: the breaker exempts it as a client error, the chat surfaces
    answer 400 with its message, ``/v1/messages`` an ``invalid_request_error``.
    The message names where in the request the URL was, never the URL itself.
    """

    def __init__(self, message: str) -> None:
        super().__init__(
            request_info=None,  # type: ignore[arg-type]
            history=(),
            status=400,
            message=message,
        )

    def __str__(self) -> str:
        # ``ClientResponseError.__str__`` reads the request's URL, and there is
        # no request.
        return f"400, message={self.message!r}"


def is_public_address(address: str) -> bool:
    """Whether ``address`` is a public unicast address a media fetch may connect to.

    IPv4 carried in IPv6 -- mapped, 6to4, NAT64, IPv4-compatible and
    IPv4-translated -- is judged as the IPv4 address it reaches; Teredo is
    refused outright. Not an IP address: False.
    """
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.teredo is not None:
            return False
        if ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        elif ip.sixtofour is not None:
            ip = ip.sixtofour
        elif any(ip in network for network in _EMBEDS_IPV4):
            ip = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    if is_metadata_address(ip):
        return False
    return ip.is_global and not ip.is_multicast


def _public_socket(addr_info: tuple[Any, ...]) -> socket.socket:
    """An aiohttp ``socket_factory`` refusing every address that is not public."""
    family, type_, proto, _, sockaddr = addr_info
    if not is_public_address(str(sockaddr[0])):
        raise OSError(errno.EACCES, f"media fetch to {sockaddr[0]} refused: not a public address")
    return socket.socket(family=family, type=type_, proto=proto)


def _caller() -> str | None:
    """Return the identity the current request's media budgets are kept under, or None.

    The user, else the API key or client the request came from. None for a
    request with no caller (an internal one), which only the global budget bounds.
    """
    ctx = req_ctx.get()
    for key in ("user_id", "affinity_key", "auth_key_hash"):
        value = ctx.get(key)
        if isinstance(value, str) and value and value not in ("anonymous", "_anon"):
            return f"{key}:{value}"
    return None


class _MediaResolver(AbstractResolver):
    """aiohttp's threaded resolver, on a small thread pool of the media fetcher's own.

    ``getaddrinfo`` blocks its thread until the system resolver answers or gives
    up, which for a name whose DNS server never replies outlasts the fetch that
    asked. aiohttp's ``ThreadedResolver`` runs it on the event loop's default
    executor, which every other lookup in the process shares, so a few such
    names stalled them all. Here a lookup runs on :data:`_DNS_THREADS` threads
    no one else uses, and a fetch waits at most :data:`_DNS_TIMEOUT_S` for it.

    One caller has at most :data:`_DNS_LOOKUPS_PER_CALLER` lookups on those
    threads, counted until the thread is done with a lookup -- also one whose
    fetch has given up on it -- so names that never resolve hold no more than
    that many threads: the caller's next lookups wait instead, and everyone
    else's still run.

    Results are those of ``ThreadedResolver``, less scoped (link-local) IPv6
    addresses, which the connect-time guard refuses anyway.
    """

    def __init__(self) -> None:
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=_DNS_THREADS, thread_name_prefix="media-dns"
        )
        self._loop: asyncio.AbstractEventLoop | None = None
        # Per caller: its semaphore, and how many lookups hold or await it, so
        # the entry goes once none does.
        self._callers: dict[str, tuple[asyncio.Semaphore, list[int]]] = {}

    async def resolve(
        self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET
    ) -> list[ResolveResult]:
        """Resolve *host* on the fetcher's own threads."""
        try:
            async with asyncio.timeout(_DNS_TIMEOUT_S):
                done = await self._admit(_caller())
                try:
                    lookup = self._executor.submit(
                        socket.getaddrinfo,
                        host,
                        port,
                        family,
                        socket.SOCK_STREAM,
                        0,
                        socket.AI_ADDRCONFIG,
                    )
                except BaseException:
                    done()
                    raise
                # Run when the thread is done with the lookup, however long
                # after this fetch stopped waiting for it.
                lookup.add_done_callback(lambda _lookup: done())
                infos = await asyncio.wrap_future(lookup)
        except TimeoutError:
            # Not a bare TimeoutError: aiohttp lets that through unwrapped. An
            # OSError is wrapped as the DNS failure it is.
            raise OSError(errno.ETIMEDOUT, "media host lookup timed out") from None
        hosts: list[ResolveResult] = []
        for info_family, _, proto, _, address in infos:
            if info_family == socket.AF_INET6:
                if len(address) < 3 or address[3]:
                    continue
            elif info_family != socket.AF_INET:
                continue
            hosts.append(
                ResolveResult(
                    hostname=host,
                    host=address[0],
                    port=address[1],
                    family=info_family,
                    proto=proto,
                    flags=socket.AI_NUMERICHOST | socket.AI_NUMERICSERV,
                )
            )
        if not hosts:
            raise OSError(errno.EACCES, f"{host} has no address a media fetch may use")
        return hosts

    async def _admit(self, caller: str | None) -> Callable[[], None]:
        """Wait for one of *caller*'s lookups; return what gives it back, from any thread."""
        loop = asyncio.get_running_loop()
        if loop is not self._loop:
            self._loop = loop
            self._callers = {}
        if caller is None:
            return lambda: None
        entry = self._callers.get(caller)
        if entry is None:
            entry = self._callers[caller] = (asyncio.Semaphore(_DNS_LOOKUPS_PER_CALLER), [0])
        entry[1][0] += 1
        try:
            await entry[0].acquire()
        except BaseException:
            self._leave(caller, entry, release=False)
            raise

        def done() -> None:
            # Called on a resolver thread when the lookup ends there.
            with contextlib.suppress(RuntimeError):  # the loop has closed since
                loop.call_soon_threadsafe(self._leave, caller, entry, True)

        return done

    def _leave(
        self, caller: str, entry: tuple[asyncio.Semaphore, list[int]], release: bool
    ) -> None:
        if release:
            entry[0].release()
        entry[1][0] -= 1
        if entry[1][0] == 0 and self._callers.get(caller) is entry:
            del self._callers[caller]

    async def close(self) -> None:
        """Nothing to close: the resolver and its threads serve every fetch."""


_RESOLVER = _MediaResolver()


class _FetchSlots:
    """How many media fetches may run at once: per caller, and in the process.

    A caller waits for one of its own slots first, so a caller at its limit
    queues behind itself without holding one of everyone's. Both waits count
    against the request's :data:`FETCH_TIMEOUT_S`. Rebuilt for a new event loop,
    as asyncio primitives belong to one.
    """

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._all = asyncio.Semaphore(MAX_GLOBAL_FETCHES)
        # Per caller: its semaphore and how many fetches hold or await it, so
        # the entry goes once nobody does.
        self._callers: dict[str, tuple[asyncio.Semaphore, list[int]]] = {}

    @contextlib.asynccontextmanager
    async def slot(self, caller: str | None) -> AsyncIterator[None]:
        """Hold one of *caller*'s slots and one of the process's for the block."""
        loop = asyncio.get_running_loop()
        if loop is not self._loop:
            self._loop = loop
            self._all = asyncio.Semaphore(MAX_GLOBAL_FETCHES)
            self._callers = {}
        if caller is None:
            async with self._all:
                yield
            return
        entry = self._callers.get(caller)
        if entry is None:
            entry = self._callers[caller] = (asyncio.Semaphore(MAX_CALLER_FETCHES), [0])
        own, users = entry
        users[0] += 1
        try:
            async with own, self._all:
                yield
        finally:
            users[0] -= 1
            if users[0] == 0 and self._callers.get(caller) is entry:
                del self._callers[caller]


_FETCH_SLOTS = _FetchSlots()


class _MediaMemo:
    """What each request fetched, kept for its later attempts.

    The router sends one request to another route when an attempt fails, and a
    hedge sends it to two; each attempt on a self-hosted route inlined the
    media again, fetching every URL anew. Kept per request and caller, for
    :data:`_MEMO_TTL_S`, and at most :data:`_MEMO_MAX_CHARS` of everyone's,
    oldest dropped first. A request has an id of its own only once a surface
    has published one, so without one nothing is kept.
    """

    def __init__(self) -> None:
        self._entries: OrderedDict[tuple[str, str, str, str], tuple[float, str]] = OrderedDict()
        self._chars = 0

    @staticmethod
    def request_key() -> tuple[str, str] | None:
        """Return the (request, caller) the current media are kept under, or None."""
        request_id = req_ctx.get().get("request_id")
        if not isinstance(request_id, str) or not request_id:
            return None
        return request_id, _caller() or ""

    def get(self, request: tuple[str, str], field: str, url: str) -> str | None:
        """Return the ``data:`` URL fetched for *url* in this request, if still kept."""
        self._expire()
        entry = self._entries.get((*request, field, url))
        return None if entry is None else entry[1]

    def put(self, request: tuple[str, str], field: str, url: str, data_url: str) -> None:
        """Keep *data_url*, dropping the oldest entries past the size limit."""
        key = (*request, field, url)
        old = self._entries.pop(key, None)
        if old is not None:
            self._chars -= len(old[1])
        if len(data_url) > _MEMO_MAX_CHARS:
            return
        self._entries[key] = (time.monotonic(), data_url)
        self._chars += len(data_url)
        while self._chars > _MEMO_MAX_CHARS:
            _, (_, dropped) = self._entries.popitem(last=False)
            self._chars -= len(dropped)

    def _expire(self) -> None:
        cutoff = time.monotonic() - _MEMO_TTL_S
        while self._entries:
            key, (stored, data_url) = next(iter(self._entries.items()))
            if stored > cutoff:
                break
            del self._entries[key]
            self._chars -= len(data_url)


_MEMO = _MediaMemo()


class _Budget:
    """The bytes all of one request's fetches may still read."""

    def __init__(self, total: int) -> None:
        self.remaining = total

    def spend(self, size: int) -> None:
        self.remaining -= size
        if self.remaining < 0:
            raise RemoteMediaRefused(
                f"The request's remote media exceed {_mib(MAX_TOTAL_MEDIA_BYTES)} together; "
                "send them as data: URLs."
            )


def _mib(size: int) -> str:
    return f"{size // (1024 * 1024)} MiB"


def _url_of(value: Any) -> str | None:
    """The URL a media field holds: ``{"url": ...}`` or, as vLLM also takes, a string."""
    if isinstance(value, dict):
        value = value.get("url")
    return value if isinstance(value, str) else None


def _with_url(value: Any, url: str) -> Any:
    """``value`` with its URL replaced, keeping the rest (``detail`` and the like)."""
    if isinstance(value, dict):
        return {**value, "url": url}
    return url


def _scheme(url: str) -> str:
    """The URL's scheme, lowercased, parsed as the engines parse it ("" if it cannot be)."""
    try:
        return urlsplit(url).scheme.lower()
    except ValueError:
        return ""


def _passes_unfetched(url: str, kind: str) -> bool:
    """Whether a media URL needs no fetch: inline data the engine decodes itself.

    A ``data:`` URL; and for an image, a bare base64 payload, which SGLang
    decodes as one (vLLM refuses it) -- unless it ends like a file name, which
    SGLang would open instead.
    """
    if _scheme(url) == "data":
        return True
    return (
        kind == "image"
        and _RAW_BASE64_RE.fullmatch(url) is not None
        and not url.lower().endswith(_FILE_LIKE_SUFFIXES)
    )


def _remote_media(messages: list[dict[str, Any]]) -> list[tuple[int, int, str, str]]:
    """``(message, part, field, url)`` for each media URL the engine would fetch."""
    found: list[tuple[int, int, str, str]] = []
    for m_idx, message in enumerate(messages):
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for p_idx, part in enumerate(content):
            if not isinstance(part, dict):
                continue
            for field, kind in _MEDIA_FIELDS.items():
                url = _url_of(part.get(field))
                if url is not None and not _passes_unfetched(url, kind):
                    found.append((m_idx, p_idx, field, url))
    return found


async def inline_remote_media(
    messages: list[dict[str, Any]],
    *,
    before_fetch: Callable[[], None] | None = None,
) -> list[dict[str, Any]]:
    """Return ``messages`` with every remote media URL fetched and inlined as ``data:``.

    Copy-on-write: the messages and parts that change are new objects, so the
    caller's request -- logged, and sent as it is to any cloud route the router
    falls back to -- keeps its URLs. With no remote media, ``messages`` itself
    comes back. ``before_fetch`` is called once, before the first fetch; a later
    attempt at the same request finds what the first fetched, and calls nothing.

    Raises:
        RemoteMediaRefused: A URL was refused or could not be fetched.
    """
    found = _remote_media(messages)
    if not found:
        return messages
    # One fetch per distinct URL: a conversation sends its images again every
    # turn. Keyed by field too, since the media type expected differs.
    first_seen: dict[tuple[str, str], str] = {}
    for m_idx, p_idx, field, url in found:
        where = f"messages[{m_idx}].content[{p_idx}].{field}"
        if _scheme(url) not in ("http", "https"):
            raise RemoteMediaRefused(f"{where} must be an http(s) or data: URL.")
        first_seen.setdefault((field, url), where)
    if len(first_seen) > MAX_REMOTE_MEDIA:
        raise RemoteMediaRefused(
            f"The request names {len(first_seen)} remote media URLs; at most "
            f"{MAX_REMOTE_MEDIA} are fetched for this model. Send the rest as data: URLs."
        )

    request = _MEMO.request_key()
    inlined: dict[tuple[str, str], str] = {}
    if request is not None:
        for field, url in first_seen:
            kept = _MEMO.get(request, field, url)
            if kept is not None:
                inlined[(field, url)] = kept
    missing = {key: where for key, where in first_seen.items() if key not in inlined}
    if missing:
        if before_fetch is not None:
            before_fetch()
        fetched = await _fetch_all(missing)
        inlined.update(fetched)
        if request is not None:
            for (field, url), data_url in fetched.items():
                _MEMO.put(request, field, url, data_url)

    out = list(messages)
    for m_idx, p_idx, field, url in found:
        message = out[m_idx]
        if message is messages[m_idx]:
            message = out[m_idx] = {**message, "content": list(message["content"])}
        part = message["content"][p_idx]
        message["content"][p_idx] = {
            **part,
            field: _with_url(part[field], inlined[(field, url)]),
        }
    return out


async def _fetch_all(wanted: dict[tuple[str, str], str]) -> dict[tuple[str, str], str]:
    """Fetch each ``(field, url)`` of *wanted* (valued with where the request names it)."""
    budget = _Budget(MAX_TOTAL_MEDIA_BYTES)
    gate = asyncio.Semaphore(_CONCURRENT_FETCHES)
    caller = _caller()
    fetches: dict[tuple[str, str], asyncio.Task[str]] = {}
    async with aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(
            socket_factory=_public_socket, limit=_CONCURRENT_FETCHES, resolver=_RESOLVER
        ),
        timeout=aiohttp.ClientTimeout(total=None, sock_connect=_CONNECT_TIMEOUT_S),
        trust_env=False,
        auto_decompress=False,
        headers={"User-Agent": _USER_AGENT, "Accept-Encoding": "identity"},
    ) as session:
        try:
            async with asyncio.timeout(FETCH_TIMEOUT_S):
                for (field, url), where in wanted.items():
                    fetches[(field, url)] = asyncio.create_task(
                        _fetch(session, url, _MEDIA_FIELDS[field], where, budget, gate, caller)
                    )
                await asyncio.gather(*fetches.values())
        except TimeoutError:
            raise RemoteMediaRefused(
                f"The request's remote media could not be fetched within {FETCH_TIMEOUT_S:g} s; "
                "send them as data: URLs."
            ) from None
        finally:
            # A failed fetch ends the rest; let them unwind before the session closes.
            for task in fetches.values():
                task.cancel()
            if fetches:
                await asyncio.wait(fetches.values())
    return {key: task.result() for key, task in fetches.items()}


async def _fetch(
    session: aiohttp.ClientSession,
    url: str,
    kind: str,
    where: str,
    budget: _Budget,
    gate: asyncio.Semaphore,
    caller: str | None = None,
) -> str:
    """Fetch one media URL and return it as a ``data:`` URL.

    Holds one of the request's own fetch slots, one of the caller's and one of
    the process's (:class:`_FetchSlots`) while it fetches.
    """
    async with gate, _FETCH_SLOTS.slot(caller):
        try:
            async with session.get(
                url,
                headers={"Accept": f"{kind}/*"},
                allow_redirects=True,
                max_redirects=_MAX_REDIRECTS,
            ) as resp:
                if not 200 <= resp.status < 300:
                    raise RemoteMediaRefused(f"{where} could not be fetched (HTTP {resp.status}).")
                encoding = resp.headers.get("Content-Encoding", "identity").strip().lower()
                if encoding not in ("", "identity"):
                    raise RemoteMediaRefused(
                        f"{where} was served with Content-Encoding {encoding}, "
                        "which is not accepted."
                    )
                media_type = (resp.content_type or "").lower()
                not_media = RemoteMediaRefused(
                    f"{where} is not {_article(kind)} {kind} (content type {media_type})."
                )
                generic = kind == "image" and media_type in _GENERIC_TYPES
                match = _MEDIA_TYPE_RE.fullmatch(media_type)
                if not generic and (match is None or match.group(1) != kind):
                    raise not_media
                body = await _read_media(resp, where, budget)
                if generic:
                    media_type = _sniff_image(body) or ""
                    if not media_type:
                        raise not_media
        except RemoteMediaRefused:
            raise
        except aiohttp.TooManyRedirects:
            raise RemoteMediaRefused(
                f"{where} redirected more than {_MAX_REDIRECTS} times."
            ) from None
        except aiohttp.ClientConnectorError as exc:
            reason = getattr(exc.os_error, "errno", None)
            if reason == errno.EACCES:
                raise RemoteMediaRefused(
                    f"{where} could not be fetched: its host is not a public address."
                ) from None
            raise RemoteMediaRefused(f"{where} could not be fetched: connection failed.") from None
        except (aiohttp.ClientError, ValueError) as exc:
            # InvalidURL is a ValueError; a redirect to a scheme other than
            # http(s) is a ClientError.
            logger.info(
                "remote_media_fetch_failed",
                extra={"event": "remote_media_fetch_failed", "error_type": type(exc).__name__},
            )
            raise RemoteMediaRefused(f"{where} could not be fetched.") from None
    return await _data_url(media_type, body)


async def _data_url(media_type: str, body: bytes) -> str:
    """Return *body* as a ``data:`` URL, base64-encoding it a slice at a time.

    ``b64encode`` holds the GIL throughout, so a thread would not free the event
    loop: 20 MiB took 50 ms in one piece. The loop runs between slices of
    :data:`_B64_SLICE_BYTES`, about 2 ms each, which join into the same text.
    """
    head = f"data:{media_type};base64,"
    if len(body) <= _B64_SLICE_BYTES:
        return head + base64.b64encode(body).decode("ascii")
    pieces = [head]
    view = memoryview(body)
    for start in range(0, len(body), _B64_SLICE_BYTES):
        pieces.append(base64.b64encode(view[start : start + _B64_SLICE_BYTES]).decode("ascii"))
        await asyncio.sleep(0)
    return "".join(pieces)


async def _read_media(resp: aiohttp.ClientResponse, where: str, budget: _Budget) -> bytes:
    """Read a media body within the per-file and per-request limits."""
    too_large = RemoteMediaRefused(f"{where} is larger than {_mib(MAX_MEDIA_BYTES)}.")
    declared = resp.content_length
    if declared is not None and declared > MAX_MEDIA_BYTES:
        raise too_large
    chunks: list[bytes] = []
    size = 0
    async for chunk in resp.content.iter_chunked(_READ_CHUNK_BYTES):
        size += len(chunk)
        if size > MAX_MEDIA_BYTES:
            raise too_large
        budget.spend(len(chunk))
        chunks.append(chunk)
    return b"".join(chunks)


def _sniff_image(body: bytes) -> str | None:
    """The image type ``body`` starts like, for one served under a generic type."""
    for signature, media_type in _IMAGE_SIGNATURES:
        if body.startswith(signature):
            return media_type
    if body[:4] == b"RIFF" and body[8:12] == b"WEBP":
        return "image/webp"
    return None


def _article(kind: str) -> str:
    return "an" if kind[0] in "aeiou" else "a"
