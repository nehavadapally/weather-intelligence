"""Create/update the Lakebase connection secret used by the Databricks App."""

import getpass

from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import ResourceAlreadyExists
from databricks.sdk.service import workspace

SCOPE = "database"
KEY = "lakebase-url"

client = WorkspaceClient()
try:
    client.secrets.create_scope(scope=SCOPE)
    print(f"Created secret scope: {SCOPE}")
except ResourceAlreadyExists:
    print(f"Secret scope already exists: {SCOPE}")

client.secrets.put_secret(
    scope=SCOPE,
    key=KEY,
    string_value=getpass.getpass("Paste the Lakebase PostgreSQL URL: "),
)
client.secrets.put_acl(
    scope=SCOPE,
    principal="users",
    permission=workspace.AclPermission.READ,
)
print(f"Stored secret {SCOPE}/{KEY}")
