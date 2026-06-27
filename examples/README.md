# ARD example artifact

[`ai-catalog.json`](ai-catalog.json) shows what arrstack-mcp publishes for
[Agentic Resource Discovery (ARD)](https://agenticresourcediscovery.org/). It was
generated for the example host `https://arrstack.example.com` with **all**
services enabled and an opt-in `did:web` identity.

It's the ARD **capability manifest** hosted at `/.well-known/ai-catalog.json`,
advertising this server as one `application/mcp-server-card+json` entry with
`capabilities`, `representativeQueries`, a `urn:air` identifier, and (here) a
`did:web` host identity. This sample uses *reference* mode: the entry's `url`
points at the MCP **server card** rather than embedding it, to keep the file
small. The card itself (every tool + its `inputSchema`) is generated live and
isn't committed — generate it with `--print-server-card` (see below). When the
server hosts statically (e.g. the GitHub Pages workflow with `ARD_EMBED_CARD=true`)
the card is embedded inline instead, so the manifest is self-contained.

You normally don't host this by hand — the running server serves both
`/.well-known/` paths live, generated from whatever `ENABLED_SERVICES` advertises.
Regenerate a deployment-specific manifest for static hosting with:

```bash
ARD_DOMAIN=your-host ARD_EMBED_CARD=true python server.py --print-catalog > ai-catalog.json
ARD_DOMAIN=your-host python server.py --print-server-card > mcp-server-card.json
```

See the **Agentic Resource Discovery (ARD)** section of the top-level
[`README.md`](../README.md) for the full publishing guide.
