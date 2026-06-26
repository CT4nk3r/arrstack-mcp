# ARD example artifacts

These files show what arrstack-mcp publishes for [Agentic Resource Discovery
(ARD)](https://agenticresourcediscovery.org/). They were generated for the
example host `https://arrstack.example.com` with **all** services enabled.

| File | Hosted at | Purpose |
|------|-----------|---------|
| [`ai-catalog.json`](ai-catalog.json) | `/.well-known/ai-catalog.json` | The ARD capability manifest. Advertises this server as one `application/mcp-server-card+json` entry with `capabilities`, `representativeQueries`, and a `did:web` identity. |
| [`mcp-server-card.json`](mcp-server-card.json) | `/.well-known/mcp-server-card.json` | The MCP server card referenced by the catalog entry's `url`. Lists every advertised tool with its `inputSchema` plus the MCP connection endpoint. |

You normally don't host these by hand — the running server serves both paths
live, generated from whatever `ENABLED_SERVICES` advertises. Regenerate these
samples (or a deployment-specific manifest for static hosting) with:

```bash
ARD_PUBLIC_URL=https://your-host python server.py --print-catalog > ai-catalog.json
ARD_PUBLIC_URL=https://your-host python server.py --print-server-card > mcp-server-card.json
```

See the **Agentic Resource Discovery (ARD)** section of the top-level
[`README.md`](../README.md) for the full publishing guide.
