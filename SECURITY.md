# Security maintenance

Halo is an independent desktop client for undocumented Ring interfaces. Its
Alarm integration is experimental and has not been validated against live
hardware. Keep the official Ring application or keypad available for critical
security workflows.

## Dependency checks

CI audits the hash-locked production requirements with `pip-audit`. Install with
the committed lockfile (`uv sync --frozen`), and run the test and installed-wheel
checks after dependency updates. Dependencies patched through private SDK APIs
require a separate compatibility review before their pins change.

The October 25, 2026 audit refreshed aiohttp, certifi, multidict, Pillow, urllib3,
and the required yarl dependency. The project does not treat every advisory in a
dependency as proof that Halo exposes the vulnerable feature.

## Scoped advisory exception

CI currently ignores exactly **CVE-2026-49265**, also identified as
**GHSA-xpv3-w29h-x7cv / PYSEC-2026-4114**. The
[upstream advisory](https://github.com/oauthlib/oauthlib/security/advisories/GHSA-xpv3-w29h-x7cv)
concerns timing comparisons in OAuthlib's authorization-server PKCE grant
implementation. Halo does not host an OAuth authorization server or validate
PKCE verifiers. Its pinned `ring-doorbell==0.9.14` dependency uses OAuthlib's
`LegacyApplicationClient` to authenticate against Ring's remote server.

Ring currently requires `oauthlib>=3,<4`. The upstream advisory names a 3.3.2
fix, but that release was unavailable on PyPI when checked on October 25, 2026;
the compatible resolver retains 3.3.1, while the published 4.0.0 release falls
outside Ring's declared range. Do not override that range merely to silence an
audit result.

Recheck and remove this exception when a compatible fixed OAuthlib or Ring
release becomes available. Reassess it immediately if Halo gains an OAuth
server, authorization-code validation, or PKCE-verifier handling. All other
advisories continue to fail CI.
