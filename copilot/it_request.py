"""The message to send an IT admin when the signed-in user is not allowed to register an Entra app
(or upload Teams custom apps). Plain text so it can be pasted into an email or ticket."""
from __future__ import annotations

TOKEN_STORE_CLIENT_ID = "ab3be6b7-f5df-413d-ac2d-abf1e3fd9c0b"
CONSENT_REDIRECT = "https://teams.microsoft.com/api/platform/v1.0/oAuthConsentRedirect"


def render(*, app_name: str, scope: str, user: str, tenant_id: str, mcp_url: str) -> str:
    who = user or "(my account)"
    return f"""\
Subject: Request: Entra app registration + Teams custom-app upload for a read-only Power BI analysis agent

Hello,

I'm setting up a small agent for Microsoft 365 Copilot that analyzes Power BI Desktop files on my own PC.
Creating the Entra app failed for my account with "Insufficient privileges to complete the operation",
so I need your help with the items below. Nothing here needs a client secret.

WHAT IT IS
- A declarative agent with an MCP plugin (Microsoft's documented extensibility model for Copilot).
- It only READS: model structure, DAX, Power Query, report layout, and small DAX query results (max 500 rows).
  It cannot change or refresh any file.
- Copilot reaches a small server on my PC through a Microsoft dev tunnel. The server rejects every request
  that does not carry a valid Entra token from THIS tenant ({tenant_id or 'our tenant'}) for the {scope} scope AND belong to
  {who}. Nobody else is on its allow-list.
- Data caution: results are sent to Microsoft 365 Copilot. If the files I analyze contain student or staff
  records, please tell me whether that is permitted, and I will limit it to metadata/aggregates.

WHAT I NEED FROM YOU

1) Create an Entra app registration (Entra admin center > Applications > App registrations > New registration):
   a. Name: {app_name}     Supported account types: this organization only (single tenant)
   b. Authentication > Add a platform > Web > Redirect URI:
        {CONSENT_REDIRECT}
   c. Expose an API > Application ID URI: keep the default  api://<client-id>
   d. Expose an API > Add a scope:
        Scope name: access_as_user      Who can consent: Admins and users      State: Enabled
        Display name / description: "Read Power BI Desktop analysis" / "Allows Microsoft 365 Copilot to analyze
        Power BI files on the user's PC on their behalf."
   e. Expose an API > Add a client application:
        Client ID: {TOKEN_STORE_CLIENT_ID}   (Microsoft Enterprise token store)   tick the access_as_user scope
   f. Manifest: set the access token version to 2 (accessTokenAcceptedVersion / requestedAccessTokenVersion = 2)
   g. Make sure an Enterprise application (service principal) exists for the app so users in the tenant can sign in.
   h. Owners > add {who}, so I can add one more Application ID URI that Teams generates (step 2 below).
      (If you'd rather not make me an owner: after I send you that URI, add it to the app's identifierUris.)

2) Reply with the Application (client) ID. I will then register the sign-in in the Teams developer portal
   (Tools > Microsoft Entra SSO client ID registration), which gives me an "Application ID URI" to add to the app.

3) Teams: allow me to upload custom apps (Teams admin center > Teams apps > Setup policies > "Upload custom apps"
   = On for my account), OR upload/approve the app package yourself in the Microsoft 365 admin center
   (Settings > Integrated apps / Agents). I will send you the .zip.

4) If user consent is restricted in our tenant, please grant admin consent for the app's access_as_user scope.

REFERENCE (Microsoft Learn)
- https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/plugin-authentication-entra-sso
- https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/prerequisites
- Server address (for your records): {mcp_url or '(created during setup)'}

Thank you,
{who}
"""
