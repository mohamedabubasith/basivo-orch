"""Every table in the service, re-exported in one place.

Alembic imports this module so ``--autogenerate`` sees the full metadata. A
model that is not reachable from here is invisible to migrations, and the next
autogenerate will cheerfully emit a ``DROP TABLE`` for it.
"""

from __future__ import annotations

from basivo_orch.auth import models as auth_models  # noqa: F401  (registers auth tables)
from basivo_orch.credentials import models as credential_models  # noqa: F401
from basivo_orch.db import Base
from basivo_orch.flows import models as flow_models  # noqa: F401  (registers flow tables)
from basivo_orch.skills import models as skill_models  # noqa: F401

__all__ = ["Base", "auth_models", "credential_models", "flow_models", "skill_models"]
