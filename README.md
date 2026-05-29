# mcp-catalog

A curated, opinionated catalog of MCP (Model Context Protocol) servers. Quality over quantity — every entry has been reviewed, includes full environment variable documentation, and comes with a curator note explaining why it was chosen and what to watch out for.

This catalog powers the [mcp-inator](https://github.com/rayjohnson/mcp-inator) macOS app.

## Catalog files

| File | Description | Updated by |
|------|-------------|------------|
| `servers.json` | Curated catalog entries with full metadata | Human curator (PR required) |
| `stats.json` | GitHub stats, Reddit sentiment, and usage counts | Automated weekly (Monday 2am + 4am UTC) |
| `usage.json` | Internal telemetry accumulator | Cloudflare Worker (real-time, internal only) |
| `config.json` | Pipeline configuration (subreddit list, etc.) | AI-managed monthly |

## Submitting a server

Open an issue using the **Submit MCP Server** template. Provide one of:
- A GitHub, Bitbucket, or GitLab repository URL
- An npm package name
- A uvx/PyPI package name
- A Docker image name

The AI enrichment pipeline will automatically populate the catalog entry and open a draft PR for curator review. A maintainer will merge it once the entry looks good.

## Categories

- Code & Development
- Productivity
- Data & Analytics
- Communication
- Infrastructure
- AI & LLMs
- Web & Browser

## License

The catalog data in this repository is available under [CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/) — use it freely in your own projects.
