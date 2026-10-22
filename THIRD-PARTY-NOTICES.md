# Third-party notices

This project is MIT licensed (see `LICENSE`). It depends on, but does not bundle, the packages below; each keeps its own license. Licenses were read from the installed package metadata on the author's machine; re-check on every release (`pip-licenses` or `pip show`).

| Package | Used for | License (per package metadata) |
|---|---|---|
| mcp | MCP server framework | MIT |
| pythonnet | Calls Power BI Desktop's ADOMD.NET client (Windows) | MIT |
| pywin32 | Windows process and window lookups (Windows) | PSF |
| psutil | Finding Power BI Desktop processes | BSD-3-Clause |
| uvicorn | HTTP server (Copilot build only) | BSD-3-Clause |
| PyJWT (+ cryptography) | Entra token validation (Copilot build only) | MIT (cryptography: Apache-2.0 OR BSD-3-Clause) |
| jsonschema | Validating the Copilot app package (build and tests only) | MIT |

## Not redistributed

- **Microsoft ADOMD.NET** is loaded from the user's own Power BI Desktop installation; this repository does not contain it.
- **Python** for the plugin launcher is downloaded from python.org at first run (the "embeddable" distribution, SHA256-pinned); it is not stored here.
- **Dev tunnels / Azure CLI** (Copilot setup wizard only) are downloaded from Microsoft on request and not stored here.

## Trademarks

Microsoft, Power BI, Microsoft 365 Copilot, Copilot Studio, Entra, Azure and Teams are trademarks of Microsoft Corporation. Claude is a trademark of Anthropic. This project is independent and not endorsed by either company. The Copilot app package icons (`copilot/appPackage/*.png`, 192x192 and 32x32) are small generic images; they do not use Microsoft or Anthropic logos. Fork authors should replace them with their own artwork.
