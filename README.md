<!-- mcp-name: io.github.carsonroell-debug/leadenrich-mcp -->

# LeadEnrich MCP Server

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![MCP](https://img.shields.io/badge/MCP-compatible-6366f1.svg)](https://modelcontextprotocol.io)

Waterfall lead enrichment for AI agents. Cascades through Apollo, Clearbit, and Hunter to build the most complete lead profile in a single call.

LeadEnrich MCP exposes lead and company enrichment through the Model Context Protocol (MCP), so tools like Claude and Cursor can run enrichment workflows directly. Give it an email, domain, or name and it returns a merged profile with field attribution showing which provider contributed each data point.

## Quick Connect

### Claude Desktop

Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "leadenrich": {
      "url": "http://localhost:8300/mcp"
    }
  }
}
```

### Claude Code

```bash
claude mcp add leadenrich --transport http http://localhost:8300/mcp
```

## Tools

| Tool | Description |
|------|-------------|
| `enrich_lead` | Full waterfall enrichment for a single lead (email, domain, or name+domain) |
| `find_email` | Discover an email from first name + last name + company domain |
| `enrich_company` | Company firmographic data by domain (industry, size, revenue, etc.) |
| `enrich_batch` | Batch enrich up to 25 leads concurrently |
| `check_usage` | Quota, cost tracking, and remaining lookups |
| `health_check` | Server status, configured providers, and cache stats |

## How It Works

LeadEnrich uses a waterfall strategy: each provider fills gaps left by the previous one. When email is known, all providers run concurrently for speed. When only name+domain is provided, Apollo discovers the email first, then Clearbit and Hunter run in parallel.

```
Input (email / domain / name+domain)
         |
         v
  +-----------+     +-----------+     +----------+
  |   Apollo  | --> |  Clearbit | --> |  Hunter  |
  +-----------+     +-----------+     +----------+
  |  Contact  |     |  Company  |     |  Email   |
  |  Company  |     |  Person   |     |  Verify  |
  |  LinkedIn |     |  Firmo    |     |  Domain  |
  +-----------+     +-----------+     +----------+
         |               |                |
         v               v                v
  +------------------------------------------+
  |        Merged Profile                    |
  |  16+ fields with per-field attribution   |
  |  Confidence score + lookup cost          |
  +------------------------------------------+
```

Each field in the result includes attribution so you know exactly which provider it came from. No duplicate API calls thanks to built-in caching.

## Pricing

| Tier | Cost | Details |
|------|------|---------|
| Free | $0.00 | 50 lookups/month |
| 1 provider hit | $0.05/lookup | Single provider returned data |
| 2 providers hit | $0.10/lookup | Two providers contributed fields |
| 3 providers hit | $0.15/lookup | Full waterfall, maximum coverage |

## Requirements

- Python 3.11+
- `pip`

## Quick Start

```bash
git clone https://github.com/carsonroell-debug/leadenrich-mcp.git
cd leadenrich-mcp
pip install -r requirements.txt
python main.py
```

MCP endpoint:

- `http://localhost:8300/mcp`

## Environment Variables

| Variable | Description | Required |
|----------|-------------|----------|
| `APOLLO_API_KEY` | Apollo.io API key | No |
| `CLEARBIT_API_KEY` | Clearbit API key | No |
| `HUNTER_API_KEY` | Hunter.io API key | No |
| `LEADENRICH_API_KEY` | Client auth key for usage metering | No |
| `LEADENRICH_FREE_TIER_LIMIT` | Free tier limit (default: 50) | No |
| `PORT` | Server port (default: 8300) | No |

All provider keys are optional. The server uses whichever providers are configured and skips the rest.

Example:

```bash
export APOLLO_API_KEY="your-apollo-key"
export CLEARBIT_API_KEY="your-clearbit-key"
export HUNTER_API_KEY="your-hunter-key"
python main.py
```

## Running Options

Run directly:

```bash
python main.py
```

Run via FastMCP CLI:

```bash
fastmcp run main.py --transport streamable-http --port 8300
```

## Tool Details

### `enrich_lead`

Inputs:

- `email` (optional): Contact email address (best identifier)
- `domain` (optional): Company domain (e.g. "stripe.com")
- `first_name` / `last_name` (optional): Contact name (combine with domain)
- `providers` (optional): Limit which providers to use
- `api_key` (optional): Your LeadEnrich API key

Returns merged lead profile with field attribution, confidence score, and lookup cost.

### `find_email`

Inputs:

- `first_name` (required): Contact's first name
- `last_name` (required): Contact's last name
- `domain` (required): Company domain

Returns discovered email with confidence score and verification status.

### `enrich_company`

Input:

- `domain` (required): Company domain

Returns company-level firmographic data: industry, size, revenue, description, location.

### `enrich_batch`

Inputs:

- `leads` (required): List of lead objects (max 25), each with optional email/domain/name
- `providers` (optional): Limit which providers to use
- `api_key` (optional): Your LeadEnrich API key

Returns list of enriched profiles with batch summary.

### `check_usage`

Input:

- `api_key` (optional): Your LeadEnrich API key

Returns usage stats: lookup count, cost, tier, remaining quota, and cache stats.

### `health_check`

No input. Returns server status, configured providers, cache stats, and version info.

## Try It

```bash
fastmcp list-tools main.py
fastmcp call-tool main.py health_check '{}'
fastmcp call-tool main.py enrich_lead '{"email":"jane@stripe.com"}'
fastmcp call-tool main.py find_email '{"first_name":"Jane","last_name":"Smith","domain":"stripe.com"}'
fastmcp call-tool main.py enrich_company '{"domain":"stripe.com"}'
```

## Deployment

### Smithery

This repo includes `smithery.yaml` for [Smithery](https://smithery.ai/) deployment.

1. Push repository to GitHub
2. Create/add server in Smithery
3. Point Smithery to this repository

### Docker / Hosting Platforms

A `Dockerfile` is included for Railway, Fly.io, and other container hosts.

```bash
# Railway
railway up

# Fly.io
fly launch
fly deploy
```

Set your provider API keys in your host environment.

## Architecture

```text
Agent (Claude, Cursor, etc.)
  -> MCP
LeadEnrich MCP Server (this repo)
  -> Apollo API    (contact + company data)
  -> Clearbit API  (person + firmographic data)
  -> Hunter API    (email finding + verification)
```

This server is a translation layer between MCP tool calls and multiple enrichment provider APIs, with built-in caching, usage metering, and waterfall merge logic.

---

Built by [Freedom Engineers](https://freedomengineers.tech)
