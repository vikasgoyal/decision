# Microsoft Decision Samples

This workspace contains example applications for using
[Microsoft-Decision-1](https://learn.microsoft.com/azure/foundry/foundry-models/how-to/use-foundry-models-microsoft-decision)
with Microsoft Foundry.

## Samples

| Folder | Description |
|---|---|
| [`101`](./101/) | Standalone Python examples for customer support, sales, purchasing, and legal-review decisions, plus a repeatability evaluation. |

## Getting started

The samples require Python 3.10 or later, Azure CLI, and a deployed
Microsoft-Decision-1 model. They use only the Python standard library.

```powershell
Set-Location .\101
Copy-Item .env.example .env
az login
py .\1-customer-issue.py --dry-run
```

Add your Foundry endpoint and deployment name to `101\.env`, then omit
`--dry-run` to call the deployment.

See the [101 sample guide](./101/README.md) for configuration, usage, available
scenarios, output, and security guidance.

## Security

Local environment files, access tokens, generated reports, virtual
environments, caches, and editor-specific files are excluded by `.gitignore`.
Do not commit credentials or reports containing sensitive input.
