# Confluence connector documentation

| Document | Read it for |
|---|---|
| [Architecture](architecture.md) | How the connector is built: processes, layers, rules per module, routing, and the contract of every MCP tool (§8) |
| [Requirements](requirements.md) | What the connector must do, and the decisions behind it (D1–D10) |
| [Roadmap](roadmap.md) | Open work: implementation, decisions and live verification |
| [API research](api-research.md) | Dated evidence of what Atlassian does not document: caps and paging, search tokenization, CQL dates, write, move and trash behavior, and the private web routes |

Diagrams: [architecture](diagrams/architecture.svg) (overview) and [code map](diagrams/code-map.svg)
(classes and who calls whom; open it in a browser to see the tooltips).

Start with the [repository README](../README.md), then the architecture. The research has one section per
area, listed in its contents: read the section you need. Probes, the acceptance suite and the UI
investigation runbook are in the [research tools](../research/README.md).
