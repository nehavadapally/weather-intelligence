"""Create/update the Lakebase URL secret used by the app and notebook.

The National Weather Service API does not require an API key or secret.
"""

import getpass

from databricks.sdk import WorkspaceClient
from databricks.sdk.service import workspace

w = WorkspaceClient()

try:
    w.secrets.create_scope(scope="database")
except Exception:
    # The scope may already exist.
    pass

w.secrets.put_secret(
    scope="database",
    key="lakebase-url",
    string_value=getpass.getpass("Paste your Lakebase PostgreSQL URL: "),
)

w.secrets.put_acl(
    scope="database",
    principal="users",
    permission=workspace.AclPermission.READ,
)

print("Stored secret: database/lakebase-url")
