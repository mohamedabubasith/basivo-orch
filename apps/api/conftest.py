"""Test-session isolation from local developer overrides.

``basivo_orch.auth.settings.Settings`` reads ``.env`` from the working
directory. That is right for running the service and wrong for running tests:
a developer's local relaxations then decide what the test suite asserts.

The concrete case this fixes: local development needs ``COOKIE_SECURE=false``
because the dev server speaks plain HTTP, but
``tests/auth/test_settings.py::test_samesite_none_is_rejected_in_production``
builds production settings and expects the SameSite validator to be the one
that rejects them. With the local override in scope, the cookie-secure
validator fires first and the test fails on a correct product.

Real environment variables take precedence over ``.env`` in pydantic-settings,
so setting them here — at import time, before collection imports anything that
builds a Settings object — pins the security-relevant values back to their
defaults. Anything already exported by the caller is left alone.
"""

import os

for key, value in {
    "COOKIE_SECURE": "true",
    "COOKIE_SAMESITE": "lax",
}.items():
    os.environ.setdefault(key, value)
