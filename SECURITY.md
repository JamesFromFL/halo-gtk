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

The November 16, 2026 review raised the dnspython minimum to **2.9.2** and updated
the lockfile. This fixes
[CVE-2026-114605](https://github.com/rthalley/dnspython/security/advisories/GHSA-45c3-73f7-pv4m)
(excessive DNS name decompression work) and
[CVE-2026-128089](https://github.com/rthalley/dnspython/security/advisories/GHSA-vvf4-9x63-8xvv)
(unbounded WKS service-number bitmap allocation). The wire-parser fix matters
to aioice's mDNS handling during camera connection setup. Offline tests cover
IPv4/IPv6 mDNS responses, malformed packets, timeout cleanup, and both parser
limits without connecting to Ring or real devices.

## Scoped advisory exceptions

The audit policy in `scripts/audit_dependencies.py` permits only the following
two reviewed advisory families, and only for **oauthlib==3.3.1**:

- [CVE-2026-49265 / GHSA-xpv3-w29h-x7cv / PYSEC-2026-4114](https://github.com/oauthlib/oauthlib/security/advisories/GHSA-xpv3-w29h-x7cv):
  timing comparisons in the authorization-server PKCE grant implementation.
- [CVE-2026-49272 / GHSA-3jp2-3mrg-2vxc](https://github.com/oauthlib/oauthlib/security/advisories/GHSA-3jp2-3mrg-2vxc):
  missing device-code validation in the authorization-server device grant.

Halo does not host an OAuth authorization server, validate PKCE verifiers, or
issue or validate device codes. Its pinned `ring-doorbell==0.9.14` dependency
uses OAuthlib's `LegacyApplicationClient` to authenticate against Ring's remote
server. These exceptions are based on that feature boundary, not on severity.

[Ring 0.9.14](https://pypi.org/pypi/ring-doorbell/0.9.14/json) requires
`oauthlib>=3,<4`. The PKCE advisory names a 3.3.2 fix, but that release remains
unavailable on PyPI as of November 16, 2026. The compatible resolver retains
3.3.1; the published fixes are in 4.0.0 and 4.0.1, outside Ring's declared range.
Do not override that range merely to silence an audit result.

CI retains the raw JSON report, including all findings, as a `dependency-audit`
artifact for 14 days. The policy requires a complete result for every active
package/version in the hashed requirements export and rejects
skipped packages, malformed results, or inconsistent tool exit statuses. A new
advisory, unreviewed alias, different package, or different version fails CI;
there is no blanket OAuthlib exclusion.

Recheck and remove these exceptions when a compatible fixed OAuthlib or Ring
release becomes available. Reassess them immediately if Halo gains an OAuth
server, authorization-code validation, PKCE-verifier handling, or a device-code
grant. All other advisories continue to fail CI.
