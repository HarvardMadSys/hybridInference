"""Independent contract tests for Kubernetes proxy membership discovery."""

import asyncio
import ssl
from contextlib import asynccontextmanager

import httpx
import pytest

from serving.utils.kubernetes_proxy_trust import (
    KubernetesProxyConfig,
    KubernetesProxyDiscovery,
    KubernetesProxyTrustError,
)

NS, SVC = "synthetic-ns", "synthetic-svc"
SVC_UID = "00000000-0000-4000-8000-000000000001"
WORKLOAD_UID = "00000000-0000-4000-8000-000000000002"
OTHER_UID = "00000000-0000-4000-8000-000000000003"
POD_A, POD_B = "00000000-0000-4000-8000-000000000010", "00000000-0000-4000-8000-000000000011"
TOKEN, TOKEN_B = "synthetic-token-a", "synthetic-token-b"
API_URL = "https://192.0.2.10:6443"
SELECTOR = {"app": "proxy", "tier": "edge", "trust": "yes"}
CA_PEM = "-----BEGIN CERTIFICATE-----\nSYNTHETIC\n-----END CERTIFICATE-----\n"


def service(*, uid=SVC_UID, name=SVC, namespace=NS, selector=None):
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "spec": {"selector": dict(SELECTOR) if selector is None else selector},
        "metadata": {"name": name, "namespace": namespace, "uid": uid, "resourceVersion": "1"},
    }


def pod(
    name,
    uid,
    ips,
    *,
    labels=None,
    owner_uid=WORKLOAD_UID,
    namespace=NS,
    ready=True,
    host_network=False,
    phase="Running",
    deleting=False,
    controller=True,
):
    metadata = {
        "name": name,
        "namespace": namespace,
        "uid": uid,
        "labels": dict(SELECTOR) if labels is None else labels,
        "ownerReferences": [
            {
                "apiVersion": "apps/v1",
                "kind": "ReplicaSet",
                "name": "synthetic-workload",
                "uid": owner_uid,
                "controller": controller,
            }
        ],
    }
    if deleting:
        metadata["deletionTimestamp"] = "2026-01-01T00:00:00Z"
    conditions = (
        [] if ready is None else [{"type": "Ready", "status": "True" if ready else "False"}]
    )
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": metadata,
        "spec": {"hostNetwork": host_network},
        "status": {
            "phase": phase,
            "podIP": ips[0],
            "podIPs": [{"ip": ip} for ip in ips],
            "conditions": conditions,
        },
    }


def pod_list(items=None, metadata=None):
    return {
        "apiVersion": "v1",
        "kind": "PodList",
        "items": [] if items is None else items,
        "metadata": {"resourceVersion": "1"} if metadata is None else metadata,
    }


class Clock:
    def __init__(self, value=1000.0):
        self.value = value

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class API:
    def __init__(self, *, svc=None, pods=None, delay=0.0):
        self.svc = service() if svc is None else svc
        self.pods = pod_list() if pods is None else pods
        self.delay, self.svc_status, self.pods_status = delay, 200, 200
        self.svc_hook = self.pods_raw = None
        self.requests = []

    async def __call__(self, request):
        self.requests.append(request)
        if self.delay:
            await asyncio.sleep(self.delay)
        if request.url.path == f"/api/v1/namespaces/{NS}/services/{SVC}":
            return (
                self.svc_hook() if self.svc_hook else httpx.Response(self.svc_status, json=self.svc)
            )
        if self.pods_raw is not None:
            return httpx.Response(self.pods_status, content=self.pods_raw)
        return httpx.Response(self.pods_status, json=self.pods)


class TLS:
    def __init__(self):
        self.calls, self.contexts = [], []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        self.contexts.append(context)
        return context


@pytest.fixture(autouse=True)
def tls(monkeypatch):
    fake = TLS()
    monkeypatch.setattr(ssl, "create_default_context", fake)
    return fake


class Env:
    def __init__(self, root):
        self.token, self.ca = root / "token", root / "ca.crt"
        self.token.write_text(TOKEN)
        self.ca.write_text(CA_PEM)

    def make(self, **overrides):
        values = {
            "namespace": NS,
            "service_name": SVC,
            "service_uid": SVC_UID,
            "workload_uid": WORKLOAD_UID,
            "api_url": API_URL,
            "ca_file": str(self.ca),
            "token_file": str(self.token),
            "max_age_seconds": 1.0,
        }
        return KubernetesProxyConfig(**{**values, **overrides})


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


@asynccontextmanager
async def opened(env, api, clock=None, **config_overrides):
    discovery = KubernetesProxyDiscovery(
        env.make(**config_overrides), transport=httpx.MockTransport(api), clock=clock or Clock()
    )
    try:
        yield discovery
    finally:
        await discovery.aclose()


def check_error(exc_info):
    text = str(exc_info.value)
    assert text.strip() and len(text) <= 200 and TOKEN not in text
    code = getattr(exc_info.value, "code", None)
    if code is not None:
        assert isinstance(code, str) and 0 < len(code) <= 100


def check_pair(api, *, offset=0, token=TOKEN):
    svc, pods = api.requests[offset : offset + 2]
    assert svc.method == pods.method == "GET"
    assert svc.url.scheme == pods.url.scheme == "https"
    assert svc.url.host == pods.url.host == "192.0.2.10"
    assert svc.url.port == pods.url.port == 6443
    assert svc.url.path == f"/api/v1/namespaces/{NS}/services/{SVC}"
    assert pods.url.path == f"/api/v1/namespaces/{NS}/pods"
    assert (
        svc.headers.get("authorization") == pods.headers.get("authorization") == f"Bearer {token}"
    )
    selector = pods.url.params.get("labelSelector")
    assert selector and dict(p.split("=", 1) for p in selector.split(",")) == SELECTOR
    assert int(pods.url.params.get("limit", "0")) > 0


@pytest.mark.asyncio
async def test_production_client_uses_ca_and_secure_options(env, tls, monkeypatch):
    api = API()
    real_client = httpx.AsyncClient
    recorded = []

    def client_factory(**kwargs):
        recorded.append(dict(kwargs))
        kwargs["transport"] = httpx.MockTransport(api)
        return real_client(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)
    discovery = KubernetesProxyDiscovery(env.make(), clock=Clock())
    try:
        await discovery.get_snapshot()
    finally:
        await discovery.aclose()
    assert any(kwargs.get("cafile") == str(env.ca) for _, kwargs in tls.calls)
    assert len(recorded) == 1
    assert recorded[0]["trust_env"] is False
    assert recorded[0]["follow_redirects"] is False
    context = recorded[0]["verify"]
    assert isinstance(context, ssl.SSLContext)
    assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED


@pytest.mark.asyncio
@pytest.mark.parametrize("ready", [False, None], ids=["ready-false", "ready-missing"])
@pytest.mark.parametrize(
    ("ips", "networks"),
    [
        (("192.0.2.20",), ("192.0.2.20/32",)),
        (("2001:db8::20",), ("2001:db8::20/128",)),
    ],
    ids=["ipv4", "ipv6"],
)
async def test_exact_networks_without_ready_requirement(env, ready, ips, networks):
    api = API(pods=pod_list([pod("proxy-a", POD_A, ips, ready=ready)]))
    async with opened(env, api) as discovery:
        snapshot = await discovery.get_snapshot()
    assert tuple(p.uid for p in snapshot.pods) == (POD_A,)
    assert isinstance(snapshot.pods, tuple) and isinstance(snapshot.networks, tuple)
    assert tuple(str(ip) for ip in snapshot.pods[0].ips) == ips
    assert tuple(str(n) for n in snapshot.networks) == networks
    assert len(api.requests) == 3
    check_pair(api)


@pytest.mark.asyncio
async def test_cache_expiry_replacement_deletion_and_close(env):
    clock = Clock()
    api = API(pods=pod_list([pod("proxy-a", POD_A, ("192.0.2.21",))]))
    async with opened(env, api, clock) as discovery:
        first = await discovery.get_snapshot()
        assert first.expires_at == 1001.0 and first.observed_at <= first.expires_at
        assert (await discovery.get_snapshot()).expires_at == first.expires_at
        clock.advance(0.99)
        assert (await discovery.get_snapshot()).expires_at == first.expires_at
        assert len(api.requests) == 3
        api.pods = pod_list([pod("proxy-b", POD_B, ("192.0.2.22",))])
        clock.advance(0.02)
        second = await discovery.get_snapshot()
        assert tuple(p.uid for p in second.pods) == (POD_B,)
        check_pair(api, offset=3)
        api.pods = pod_list([])
        clock.advance(1.01)
        third = await discovery.get_snapshot()
        assert third.pods == () and third.networks == ()
        assert len(api.requests) == 9
        await discovery.aclose()
        await discovery.aclose()
        with pytest.raises(KubernetesProxyTrustError) as exc_info:
            await discovery.get_snapshot()
    check_error(exc_info)
    assert len(api.requests) == 9


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"owner_uid": OTHER_UID},
        {"labels": {"app": "proxy"}},
        {"namespace": "other-ns"},
        {"host_network": True},
        {"phase": "Pending"},
        {"deleting": True},
        {"controller": False},
    ],
    ids=[
        "owner-new-uid",
        "wrong-selector",
        "wrong-namespace",
        "host-network",
        "pending",
        "deleting",
        "not-controller",
    ],
)
async def test_ineligible_pods_never_grant_trust(env, overrides):
    api = API(pods=pod_list([pod("proxy-a", POD_A, ("192.0.2.30",), **overrides)]))
    async with opened(env, api) as discovery:
        if "namespace" in overrides or "labels" in overrides:
            with pytest.raises(KubernetesProxyTrustError):
                await discovery.get_snapshot()
        else:
            snapshot = await discovery.get_snapshot()
            assert snapshot.pods == () and snapshot.networks == ()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode",
    [
        "service-uid",
        "empty-selector",
        "wrong-kind",
        "malformed",
        "403",
        "302",
        "continue",
        "remaining",
    ],
)
async def test_bad_service_or_api_pagination_fails(env, mode):
    api = API()
    if mode == "service-uid":
        api.svc = service(uid=OTHER_UID)
    elif mode == "empty-selector":
        api.svc = service(selector={})
    elif mode == "wrong-kind":
        api.pods = {"apiVersion": "v1", "kind": "ServiceList", "metadata": {}, "items": []}
    elif mode == "malformed":
        api.pods_raw = b'{"apiVersion": "v1", "kind": "PodList",'
    elif mode == "403":
        api.svc_status = 403
    elif mode == "302":
        api.svc_hook = lambda: httpx.Response(302, headers={"location": "https://192.0.2.99/x"})
    elif mode == "continue":
        api.pods = pod_list(metadata={"resourceVersion": "1", "continue": "next"})
    else:
        api.pods = pod_list(metadata={"resourceVersion": "1", "remainingItemCount": 1})
    async with opened(env, api) as discovery:
        with pytest.raises(KubernetesProxyTrustError) as exc_info:
            await discovery.get_snapshot()
    check_error(exc_info)
    assert len(api.requests) == (
        1 if mode in ("service-uid", "empty-selector", "403", "302") else 2
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "items",
    [
        [pod("proxy-a", POD_A, ("192.0.2.40",)), pod("proxy-b", POD_B, ("192.0.2.40",))],
        [pod("proxy-a", POD_A, ("192.0.2.40",)), pod("proxy-b", POD_A, ("192.0.2.41",))],
    ],
    ids=["duplicate-ip", "duplicate-uid"],
)
async def test_duplicate_ip_or_uid_fails(env, items):
    api = API(pods=pod_list(items))
    async with opened(env, api) as discovery:
        with pytest.raises(KubernetesProxyTrustError) as exc_info:
            await discovery.get_snapshot()
    check_error(exc_info)
    assert len(api.requests) == 2


@pytest.mark.asyncio
async def test_token_rotation_and_old_snapshot_after_failure(env):
    clock = Clock()
    api = API(pods=pod_list([pod("proxy-a", POD_A, ("192.0.2.50",))]))
    async with opened(env, api, clock) as discovery:
        await discovery.get_snapshot()
        check_pair(api)
        env.token.write_text(TOKEN_B)
        clock.advance(1.01)
        await discovery.get_snapshot()
        check_pair(api, offset=3, token=TOKEN_B)
        clock.advance(1.01)
        api.svc_status = 503
        with pytest.raises(KubernetesProxyTrustError) as exc_info:
            await discovery.get_snapshot()
        check_error(exc_info)
        with pytest.raises(KubernetesProxyTrustError):
            await discovery.get_snapshot()
        assert len(api.requests) == 7
        clock.advance(0.26)
        api.svc_status = 200
        recovered = await discovery.get_snapshot()
        assert tuple(p.uid for p in recovered.pods) == (POD_A,)
        assert len(api.requests) == 10


@pytest.mark.asyncio
async def test_concurrent_failures_share_one_refresh(env):
    api = API(delay=0.01)
    api.svc_status = 500
    async with opened(env, api) as discovery:
        results = await asyncio.gather(
            *(discovery.get_snapshot() for _ in range(6)), return_exceptions=True
        )
    assert all(isinstance(r, KubernetesProxyTrustError) for r in results)
    assert len(api.requests) == 1


@pytest.mark.asyncio
async def test_concurrent_success_dedupes_refresh(env):
    api = API(pods=pod_list([pod("proxy-a", POD_A, ("192.0.2.52",))]), delay=0.01)
    async with opened(env, api) as discovery:
        results = await asyncio.gather(*(discovery.get_snapshot() for _ in range(5)))
    assert len(api.requests) == 3
    assert all(tuple(p.uid for p in r.pods) == (POD_A,) for r in results)


@pytest.mark.asyncio
async def test_deadline_measured_from_read_start(env):
    clock = Clock()
    api = API(pods=pod_list([pod("proxy-a", POD_A, ("192.0.2.53",))]))

    def slow_service():
        clock.advance(2.0)
        return httpx.Response(200, json=api.svc)

    api.svc_hook = slow_service
    async with opened(env, api, clock) as discovery:
        with pytest.raises(KubernetesProxyTrustError) as exc_info:
            await discovery.get_snapshot()
        check_error(exc_info)
        calls = len(api.requests)
        with pytest.raises(KubernetesProxyTrustError):
            await discovery.get_snapshot()
        assert len(api.requests) == calls


@pytest.mark.parametrize("max_age", [True, 0, -0.1, 5.0001, float("inf"), float("nan")])
def test_max_age_bounded_and_finite(env, max_age):
    with pytest.raises((ValueError, KubernetesProxyTrustError)):
        env.make(max_age_seconds=max_age)
    assert env.make(max_age_seconds=5.0).max_age_seconds == 5.0


@pytest.mark.parametrize(
    "url",
    [
        "http://192.0.2.10:6443",
        "https://user:pass@192.0.2.10:6443",
        "https://192.0.2.10:6443/api",
        "https://192.0.2.10:6443?x=1",
        "https://192.0.2.10:6443#fragment",
    ],
)
def test_https_base_url_validation(env, url):
    with pytest.raises((ValueError, KubernetesProxyTrustError)):
        env.make(api_url=url)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("address", "expected"),
    [
        ("2001:0DB8:0000:0000:0000:0000:0000:0070", "2001:db8::70/128"),
        ("::ffff:192.0.2.70", "192.0.2.70/32"),
    ],
)
async def test_primary_ip_is_canonicalized_like_pod_ips(env, address, expected):
    api = API(pods=pod_list([pod("proxy-a", POD_A, (address,))]))
    async with opened(env, api) as discovery:
        snapshot = await discovery.get_snapshot()
    assert tuple(map(str, snapshot.networks)) == (expected,)


@pytest.mark.asyncio
async def test_reused_address_with_untrusted_new_owner_revokes_old_membership(env):
    clock = Clock()
    api = API(pods=pod_list([pod("proxy-a", POD_A, ("192.0.2.71",))]))
    async with opened(env, api, clock) as discovery:
        assert (await discovery.get_snapshot()).networks
        api.pods = pod_list([pod("proxy-b", POD_B, ("192.0.2.71",), owner_uid=OTHER_UID)])
        clock.advance(1.01)
        assert (await discovery.get_snapshot()).networks == ()


@pytest.mark.asyncio
async def test_token_rotation_between_service_and_pod_get_is_observed(env):
    api = API()

    def rotate_token():
        env.token.write_text(TOKEN_B)
        return httpx.Response(200, json=api.svc)

    api.svc_hook = rotate_token
    async with opened(env, api) as discovery:
        await discovery.get_snapshot()
    assert [request.headers["authorization"] for request in api.requests] == [
        f"Bearer {TOKEN}",
        f"Bearer {TOKEN_B}",
        f"Bearer {TOKEN_B}",
    ]


@pytest.mark.asyncio
async def test_service_identity_fence_detects_change_during_pod_read(env):
    api = API()
    count = 0

    def changing_service():
        nonlocal count
        count += 1
        payload = service()
        payload["metadata"]["resourceVersion"] = str(count)
        return httpx.Response(200, json=payload)

    api.svc_hook = changing_service
    async with opened(env, api) as discovery:
        with pytest.raises(KubernetesProxyTrustError):
            await discovery.get_snapshot()
    assert len(api.requests) == 3


@pytest.mark.asyncio
async def test_pod_items_must_really_be_v1_pods(env):
    wrong = pod("proxy-a", POD_A, ("192.0.2.72",))
    wrong["kind"] = "Service"
    api = API(pods=pod_list([wrong]))
    async with opened(env, api) as discovery:
        with pytest.raises(KubernetesProxyTrustError):
            await discovery.get_snapshot()


@pytest.mark.asyncio
async def test_duplicate_json_keys_fail_closed(env):
    api = API()
    api.pods_raw = b'{"kind":"PodList","kind":"PodList","apiVersion":"v1","metadata":{"resourceVersion":"1"},"items":[]}'
    async with opened(env, api) as discovery:
        with pytest.raises(KubernetesProxyTrustError):
            await discovery.get_snapshot()


@pytest.mark.asyncio
async def test_oversized_streamed_body_is_not_accepted(env):
    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"apiVersion":"v1","kind":"PodList","metadata":{"resourceVersion":"1"},"items":[],"padding":"'
            for _ in range(16):
                yield b"x" * 128
            yield b'"}'

    api = API()

    async def transport(request):
        if request.url.path.endswith("/pods"):
            return httpx.Response(200, stream=Body())
        return await api(request)

    async with opened(env, transport, max_response_bytes=1024) as discovery:
        with pytest.raises(KubernetesProxyTrustError):
            await discovery.get_snapshot()


@pytest.mark.asyncio
async def test_total_refresh_timeout_fails_closed(env):
    api = API(delay=0.1)
    async with opened(env, api, max_age_seconds=0.02, timeout_seconds=0.02) as discovery:
        with pytest.raises(KubernetesProxyTrustError):
            await discovery.get_snapshot()


@pytest.mark.asyncio
async def test_close_started_during_refresh_prevents_publication(env):
    api = API()
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked(request):
        entered.set()
        await release.wait()
        return await api(request)

    discovery = KubernetesProxyDiscovery(
        env.make(), transport=httpx.MockTransport(blocked), clock=Clock()
    )
    refresh = asyncio.create_task(discovery.get_snapshot())
    try:
        await asyncio.wait_for(entered.wait(), 0.5)
        closing = asyncio.create_task(discovery.aclose())
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(KubernetesProxyTrustError):
            await refresh
        await closing
        with pytest.raises(KubernetesProxyTrustError):
            await discovery.get_snapshot()
    finally:
        release.set()
        if not refresh.done():
            refresh.cancel()
            await asyncio.gather(refresh, return_exceptions=True)
        await discovery.aclose()


@pytest.mark.asyncio
async def test_cancelled_refresh_backoff_prevents_caller_driven_api_storm(env):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def blocked(request):
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return httpx.Response(200, json=service())

    discovery = KubernetesProxyDiscovery(
        env.make(), transport=httpx.MockTransport(blocked), clock=Clock()
    )
    refresh = asyncio.create_task(discovery.get_snapshot())
    try:
        await asyncio.wait_for(entered.wait(), 0.5)
        refresh.cancel()
        with pytest.raises(asyncio.CancelledError):
            await refresh
        release.set()
        with pytest.raises(KubernetesProxyTrustError):
            await discovery.get_snapshot()
        assert calls == 1
    finally:
        release.set()
        await discovery.aclose()
