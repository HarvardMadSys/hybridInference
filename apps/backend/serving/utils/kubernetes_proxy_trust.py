"""Read bounded, short-lived proxy membership from an authenticated Kubernetes API.

This is address discovery, not per-connection authentication. Namespace RBAC,
protected workload ownership, ingress header handling and network isolation are
part of its trust boundary. No API objects or application settings are written.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import math
import re
import ssl
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlsplit

import httpx

if TYPE_CHECKING:
    from collections.abc import Callable

Network = ipaddress.IPv4Network | ipaddress.IPv6Network
_NAME = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_LABEL_NAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,61}[A-Za-z0-9])?\Z")
_UID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")


class KubernetesProxyTrustError(RuntimeError):
    """A bounded diagnostic code that never includes tokens or API response data."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class KubernetesProxyConfig:
    """Deployment-owned discovery wiring; never derived from an HTTP request."""

    namespace: str
    service_name: str
    service_uid: str
    workload_uid: str
    api_url: str = "https://kubernetes.default.svc"
    ca_file: str = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
    token_file: str = "/var/run/secrets/kubernetes.io/serviceaccount/token"
    max_age_seconds: float = 1.0
    timeout_seconds: float = 2.0
    max_response_bytes: int = 2_000_000
    max_pods: int = 500

    def __post_init__(self) -> None:
        if not all(_NAME.fullmatch(value) for value in (self.namespace, self.service_name)):
            raise ValueError("invalid proxy resource name")
        if not all(_UID.fullmatch(value) for value in (self.service_uid, self.workload_uid)):
            raise ValueError("invalid proxy resource identity")
        try:
            url = urlsplit(self.api_url)
            valid_url = (
                url.scheme == "https"
                and bool(url.hostname)
                and url.username is None
                and url.password is None
                and url.path in ("", "/")
                and not any(char.isspace() or char in "?#\\" for char in self.api_url)
                and (url.port is None or 1 <= url.port <= 65535)
            )
        except ValueError:
            valid_url = False
        if not valid_url or not self.ca_file.strip() or not self.token_file.strip():
            raise ValueError("invalid proxy API TLS wiring")
        for value in (self.max_age_seconds, self.timeout_seconds):
            if isinstance(value, bool) or not math.isfinite(value) or not 0 < value <= 5:
                raise ValueError("invalid proxy timeout or freshness bound")
        if type(self.max_pods) is not int or not 1 <= self.max_pods <= 1000:
            raise ValueError("invalid proxy Pod limit")
        if (
            type(self.max_response_bytes) is not int
            or not 1 <= self.max_response_bytes <= 2_000_000
        ):
            raise ValueError("invalid proxy response limit")


@dataclass(frozen=True)
class TrustedProxyPod:
    """A directly owned proxy Pod and its exact transport addresses."""

    uid: str
    name: str
    ips: tuple[str, ...]


@dataclass(frozen=True)
class ProxyTrustSnapshot:
    """Immutable membership with a local monotonic expiry, including empty sets."""

    pods: tuple[TrustedProxyPod, ...]
    networks: tuple[Network, ...]
    observed_at: float
    expires_at: float


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise KubernetesProxyTrustError("ambiguous_json")
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise KubernetesProxyTrustError("invalid_json")


def _selector(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or not value or len(value) > 64:
        raise KubernetesProxyTrustError("invalid_service_selector")
    for key, label in value.items():
        if not isinstance(key, str) or not isinstance(label, str):
            raise KubernetesProxyTrustError("invalid_service_selector")
        parts = key.split("/")
        valid = len(parts) <= 2 and _LABEL_NAME.fullmatch(parts[-1])
        if len(parts) == 2:
            valid = (
                valid
                and len(parts[0]) <= 253
                and all(_NAME.fullmatch(part) for part in parts[0].split("."))
            )
        if not valid or (label and not _LABEL_NAME.fullmatch(label)):
            raise KubernetesProxyTrustError("invalid_service_selector")
    return value


def _pod_ips(status: dict[str, Any]) -> tuple[str, ...]:
    primary = status.get("podIP")
    raw = status.get("podIPs", [{"ip": primary}])
    if not isinstance(raw, list) or not 1 <= len(raw) <= 2:
        raise KubernetesProxyTrustError("invalid_pod_addresses")
    values = []
    for entry in raw:
        value = entry.get("ip") if isinstance(entry, dict) else None
        if not isinstance(value, str) or "%" in value:
            raise KubernetesProxyTrustError("invalid_pod_addresses")
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            raise KubernetesProxyTrustError("invalid_pod_addresses") from None
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
            address = address.ipv4_mapped
        if (
            address.is_loopback
            or address.is_link_local
            or address.is_multicast
            or address.is_unspecified
            or address.is_reserved
        ):
            raise KubernetesProxyTrustError("invalid_pod_addresses")
        values.append(str(address))
    try:
        primary_address = ipaddress.ip_address(primary)
        if (
            isinstance(primary_address, ipaddress.IPv6Address)
            and primary_address.ipv4_mapped is not None
        ):
            primary_address = primary_address.ipv4_mapped
    except (ValueError, TypeError):
        raise KubernetesProxyTrustError("invalid_pod_addresses") from None
    if len(set(values)) != len(values) or str(primary_address) not in values:
        raise KubernetesProxyTrustError("invalid_pod_addresses")
    return tuple(values)


class KubernetesProxyDiscovery:
    """Serialize bounded reads and discard expired or failed discovery results."""

    def __init__(
        self,
        config: KubernetesProxyConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self._transport = transport
        self._clock = clock
        self._client: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()
        self._snapshot: ProxyTrustSnapshot | None = None
        self._retry_at = 0.0
        self._error_code = "discovery_unavailable"
        self._closed = False

    def _http_client(self) -> httpx.AsyncClient:
        if self._client is None:
            verify = (
                True
                if self._transport is not None
                else ssl.create_default_context(cafile=self.config.ca_file)
            )
            self._client = httpx.AsyncClient(
                verify=verify,
                transport=self._transport,
                trust_env=False,
                follow_redirects=False,
                timeout=self.config.timeout_seconds,
            )
        return self._client

    async def _get(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        # Projected tokens rotate. Never cache a token in the client or snapshot.
        with Path(self.config.token_file).open("rb") as source:
            raw_token = source.read(16385)
        if len(raw_token) > 16384:
            raise KubernetesProxyTrustError("invalid_token_file")
        token = raw_token.decode("ascii").strip()
        if not token or any(char.isspace() or ord(char) < 33 or ord(char) > 126 for char in token):
            raise KubernetesProxyTrustError("invalid_token_file")
        url = self.config.api_url.rstrip("/") + path
        async with self._http_client().stream(
            "GET",
            url,
            params=params,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "Accept-Encoding": "identity",
            },
        ) as response:
            if response.status_code != 200:
                raise KubernetesProxyTrustError(f"api_status_{response.status_code}")
            if response.headers.get("content-encoding", "identity").lower() not in ("", "identity"):
                raise KubernetesProxyTrustError("encoded_api_response")
            data = bytearray()
            async for chunk in response.aiter_bytes():
                if len(data) + len(chunk) > self.config.max_response_bytes:
                    raise KubernetesProxyTrustError("api_response_too_large")
                data.extend(chunk)
        try:
            result = json.loads(
                data.decode("utf-8"),
                object_pairs_hook=_unique_object,
                parse_constant=_invalid_constant,
            )
        except (ValueError, UnicodeError, RecursionError):
            raise KubernetesProxyTrustError("invalid_json") from None
        if not isinstance(result, dict):
            raise KubernetesProxyTrustError("invalid_api_object")
        return result

    def _service(self, value: dict[str, Any]) -> tuple[str, dict[str, str]]:
        meta, spec = value.get("metadata", {}), value.get("spec", {})
        if not isinstance(meta, dict) or not isinstance(spec, dict):
            raise KubernetesProxyTrustError("invalid_service")
        if (
            value.get("kind") != "Service"
            or value.get("apiVersion") != "v1"
            or meta.get("namespace") != self.config.namespace
            or meta.get("name") != self.config.service_name
            or meta.get("uid") != self.config.service_uid
            or meta.get("deletionTimestamp") is not None
            or not isinstance(meta.get("resourceVersion"), str)
            or not meta["resourceVersion"]
            or spec.get("type", "ClusterIP") not in ("ClusterIP", "NodePort", "LoadBalancer")
        ):
            raise KubernetesProxyTrustError("service_identity_mismatch")
        return meta["resourceVersion"], _selector(spec.get("selector"))

    def _pods(self, value: dict[str, Any], selector: dict[str, str]) -> tuple[TrustedProxyPod, ...]:
        meta, items = value.get("metadata", {}), value.get("items")
        if (
            value.get("kind") != "PodList"
            or value.get("apiVersion") != "v1"
            or not isinstance(meta, dict)
            or not isinstance(items, list)
            or not isinstance(meta.get("resourceVersion"), str)
            or not meta["resourceVersion"]
            or meta.get("continue") not in (None, "")
            or (
                meta.get("remainingItemCount") is not None
                and (type(meta["remainingItemCount"]) is not int or meta["remainingItemCount"] != 0)
            )
            or len(items) > self.config.max_pods
        ):
            raise KubernetesProxyTrustError("incomplete_pod_list")
        trusted = []
        seen_uids: set[str] = set()
        seen_ips: set[str] = set()
        for item in items:
            if (
                not isinstance(item, dict)
                or item.get("kind") != "Pod"
                or item.get("apiVersion") != "v1"
            ):
                raise KubernetesProxyTrustError("invalid_pod")
            metadata, spec, status = (
                item.get("metadata", {}),
                item.get("spec", {}),
                item.get("status", {}),
            )
            if not all(isinstance(part, dict) for part in (metadata, spec, status)):
                raise KubernetesProxyTrustError("invalid_pod")
            if metadata.get("namespace") != self.config.namespace:
                raise KubernetesProxyTrustError("pod_namespace_mismatch")
            labels = metadata.get("labels", {})
            if not isinstance(labels, dict) or any(labels.get(k) != v for k, v in selector.items()):
                raise KubernetesProxyTrustError("pod_selector_mismatch")
            if status.get("phase") != "Running" or metadata.get("deletionTimestamp") is not None:
                continue
            ips = _pod_ips(status)
            if seen_ips.intersection(ips):
                raise KubernetesProxyTrustError("duplicate_pod_address")
            seen_ips.update(ips)
            owners = metadata.get("ownerReferences", [])
            if not isinstance(owners, list) or any(not isinstance(owner, dict) for owner in owners):
                raise KubernetesProxyTrustError("invalid_pod_owner")
            controllers = [owner for owner in owners if owner.get("controller") is True]
            if len(controllers) != 1 or controllers[0].get("uid") != self.config.workload_uid:
                continue
            if spec.get("hostNetwork", False) is not False:
                continue
            uid, name = metadata.get("uid"), metadata.get("name")
            if (
                not isinstance(uid, str)
                or not _UID.fullmatch(uid)
                or not isinstance(name, str)
                or not name
            ):
                raise KubernetesProxyTrustError("invalid_pod_identity")
            if uid in seen_uids:
                raise KubernetesProxyTrustError("duplicate_pod_identity")
            seen_uids.add(uid)
            trusted.append(TrustedProxyPod(uid=uid, name=name, ips=ips))
        return tuple(trusted)

    async def get_snapshot(self) -> ProxyTrustSnapshot:
        """Return a fresh set; failures revoke the old set and briefly back off."""
        async with self._lock:
            if self._closed:
                raise KubernetesProxyTrustError("discovery_closed")
            now = self._clock()
            if self._snapshot is not None and now < self._snapshot.expires_at:
                return self._snapshot
            self._snapshot = None
            if now < self._retry_at:
                raise KubernetesProxyTrustError(self._error_code)
            observed_at = now
            prefix = f"/api/v1/namespaces/{quote(self.config.namespace, safe='')}/"
            service_path = prefix + "services/" + quote(self.config.service_name, safe="")
            try:
                async with asyncio.timeout(
                    min(self.config.timeout_seconds * 3, self.config.max_age_seconds)
                ):
                    service = self._service(await self._get(service_path))
                    selector = service[1]
                    pod_list = await self._get(
                        prefix + "pods",
                        params={
                            "labelSelector": ",".join(
                                f"{k}={v}" for k, v in sorted(selector.items())
                            ),
                            "limit": str(self.config.max_pods + 1),
                        },
                    )
                    pods = self._pods(pod_list, selector)
                    if self._service(await self._get(service_path)) != service:
                        raise KubernetesProxyTrustError("service_changed_during_read")
                expires_at = observed_at + self.config.max_age_seconds
                if self._clock() >= expires_at:
                    raise KubernetesProxyTrustError("snapshot_expired_during_read")
                if self._closed:
                    raise KubernetesProxyTrustError("discovery_closed")
                networks = tuple(ipaddress.ip_network(ip) for pod in pods for ip in pod.ips)
                self._snapshot = ProxyTrustSnapshot(pods, networks, observed_at, expires_at)
            except asyncio.CancelledError:
                self._error_code = "discovery_cancelled"
                self._retry_at = self._clock() + 0.25
                raise
            except Exception as exc:
                self._error_code = (
                    exc.code
                    if isinstance(exc, KubernetesProxyTrustError)
                    else "discovery_unavailable"
                )
                self._retry_at = self._clock() + 0.25
                raise KubernetesProxyTrustError(self._error_code) from None
            return self._snapshot

    async def aclose(self) -> None:
        """Drop membership and close pooled TLS connections."""
        self._closed = True
        self._snapshot = None
        async with self._lock:
            if self._client is not None:
                await self._client.aclose()
                self._client = None
