# Security and deployment limits

This alpha is a single configured-dataset retrieval library, not a tenant-isolation or PII-compliance
platform. Request filters are search constraints, not authorization. Use a trusted deployment layer
for identity, ACLs, TLS, rate limits, audit retention and encryption. Do not accept configuration,
regex patterns, provider base URLs, prompts, model paths or database credentials from untrusted users.

Optional redaction only applies configured rules before new indexing. IDs, source URLs, old snapshots,
external generations, caller context and unknown forms of PII need separate review. Query traces
contain query text; returned evidence and document status can be sensitive. Remote models receive
configured source/context data. Minimize it before sending and follow your organization's provider
and retention policies.

Prompt separation and citation ID checks reduce specific mistakes; they are not a complete prompt
injection defense or proof of factuality. No arbitrary model-generated SQL, Cypher, shell or Python
is executed. Models load with remote code disabled where supported. Faiss/model files should come
from trusted sources. API authentication uses an optional shared token; use a gateway for per-user
controls. MCP stdio inherits its parent process permissions.

No public disclosure address has been chosen yet. Before publishing, maintainers should enable a
private security-advisory channel in the repository and document it here. Do not put exploit details
or production secrets in a public issue.
