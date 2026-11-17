"""On-box native-MCP adapter (`fsr_soc_triage._live_mcp`) auth modes.

Regression for the soc-tool 401 root cause: the gateway replays the incoming
Authorization header *verbatim* onto downstream /api/3 calls, and CS-HMAC is
signed for a single URI (the /mcp/soc/ gateway URL) → the replay against
/api/3 fails signature validation → 401. Any non-URI-bound credential that maps
to a user survives the replay: an API key (`Authorization: API-KEY <key>`,
static) or a minted bearer JWT (service creds). Box-proven against 8.0; these
lock the mode-selection + header + retry logic hermetically.
"""
import fsr_soc_triage._live_mcp as L


def teardown_function():
    L._BEARER_CACHE.clear()


def test_build_client_api_key_mode_takes_priority():
    """An API key selects api-key mode and wins over bearer/HMAC when both are
    supplied — static header, no bearer provider, no HMAC keys."""
    c = L.build_client(soar_api_key="key-123", soar_username="svc",
                       soar_password="pw")
    assert c is not None
    assert c.mcp._api_key == "key-123"
    assert c.mcp._bearer_provider is None  # api-key wins over bearer
    assert not c.mcp._pub and not c.mcp._priv


def test_headers_api_key_is_static():
    """API-KEY header is the same regardless of URL or force_refresh (nothing to
    mint / refresh) and uses the FortiSOAR 'API-KEY <key>' scheme."""
    mcp = L._CrudhubMCP(api_key="key-123")
    h1 = mcp._headers("http://x/mcp/soc/")
    h2 = mcp._headers("http://x/mcp/utility/", force_refresh=True)
    assert h1 == h2 == {"Authorization": "API-KEY key-123"}


def test_build_client_bearer_mode_when_creds_present():
    """Creds → bearer-mode client, no appliance HMAC keys needed (works even
    with no Django settings, i.e. the off-box/test path)."""
    c = L.build_client(soar_username="svc", soar_password="pw")
    assert c is not None
    assert c.mcp._bearer_provider is not None
    assert not c.mcp._pub and not c.mcp._priv  # HMAC unused in bearer mode


def test_build_client_none_off_box_without_creds():
    """No creds + no Django settings (off-box) → None (dormant), never raises."""
    assert L.build_client() is None


def test_bearer_provider_caches_then_refreshes(monkeypatch):
    calls = []
    monkeypatch.setattr(L, "_mint_bearer", lambda u, p: f"tok-{len(calls)}" or calls.append(1))
    minted = []
    monkeypatch.setattr(L, "_mint_bearer", lambda u, p: (minted.append(1), f"tok{len(minted)}")[1])
    prov = L._bearer_provider("svc", "pw")
    a = prov()
    b = prov()                 # cached — no second mint
    assert a == b == "tok1"
    assert len(minted) == 1
    c = prov(force_refresh=True)  # forced — re-mints
    assert c == "tok2"
    assert len(minted) == 2


def test_headers_bearer_vs_hmac():
    # bearer mode
    mcp_b = L._CrudhubMCP(bearer_provider=lambda force=False: "abc123")
    assert mcp_b._headers("http://x/mcp/soc/") == {"Authorization": "Bearer abc123"}
    # no token → empty (degrade, don't crash)
    mcp_none = L._CrudhubMCP(bearer_provider=lambda force=False: None)
    assert mcp_none._headers("http://x/mcp/soc/") == {}


def test_headers_hmac_path_uses_cs(monkeypatch):
    seen = {}

    class _FakeHmac:
        def __init__(self, **kw):
            seen.update(kw)
        def __call__(self, req):
            req.headers["Authorization"] = "CS deadbeef"
            return req

    mcp_h = L._CrudhubMCP(pub="P", priv="K", HmacAuth=_FakeHmac)
    hdr = mcp_h._headers("http://x/mcp/utility/")
    assert hdr == {"Authorization": "CS deadbeef"}
    assert seen["public_key"] == "P" and seen["private_key"] == "K"


def test_bearer_run_retries_once_on_401(monkeypatch):
    """A 401 from the first attempt triggers exactly one force-refresh retry."""
    forces = []
    mcp = L._CrudhubMCP(bearer_provider=lambda force=False: (forces.append(force), "t")[1])

    attempts = []

    def fake_headers(url, *, force_refresh=False):
        attempts.append(force_refresh)
        mcp._bearer_provider(force_refresh)
        return {"Authorization": "Bearer t"}

    monkeypatch.setattr(mcp, "_headers", fake_headers)

    def coro_factory(session):  # never reached — session build raises
        raise AssertionError("should not run")

    # Make the session build raise a 401 both times; assert 2 attempts then raise.
    import fsr_soc_triage._live_mcp as mod

    def boom(*a, **k):
        raise RuntimeError("stream error: 401 Unauthorized")

    monkeypatch.setattr(mod.asyncio, "run", boom)
    try:
        mcp._run("soc", coro_factory)
        assert False, "expected the 401 to propagate after retry"
    except RuntimeError as ex:
        assert "401" in str(ex)
    assert attempts == [False, True]  # initial + one forced retry
