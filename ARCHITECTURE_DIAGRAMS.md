# ARCHITECTURE_DIAGRAMS.md

Mermaid diagrams extracted from ARCHITECTURE.md.
Each section label corresponds to the matching section in that document.

---

## From Section 4 — Request Flow: Single D.0 → F6 Conversion

```mermaid
sequenceDiagram
    participant Client
    participant main.py
    participant orchestrator.py
    participant rules_reader.py
    participant segment_parser.py
    participant transaction_detector.py
    participant field_mapper.py
    participant f6_assembler.py
    participant audit_builder.py
    participant db_ops.py

    Client->>main.py: POST /api/convert/stream {d0_text}
    main.py->>db_ops.py: create_conversion() + mark_processing()
    main.py->>orchestrator.py: convert_stream(d0_text)
    orchestrator.py->>rules_reader.py: load_all(RULES_DIR)
    rules_reader.py-->>orchestrator.py: RuleSet (11 files)
    orchestrator.py-->>Client: SSE step: reading_rules complete
    orchestrator.py->>segment_parser.py: parse_d0(d0_text)
    segment_parser.py-->>orchestrator.py: ParsedTransaction
    orchestrator.py-->>Client: SSE step: parsing complete
    orchestrator.py->>transaction_detector.py: detect(parsed, ruleset)
    transaction_detector.py-->>orchestrator.py: tx_type (e.g. RETAIL)
    orchestrator.py-->>Client: SSE step: detecting complete
    orchestrator.py->>rules_reader.py: ruleset.get_rules_for(tx_type)
    rules_reader.py-->>orchestrator.py: merged tx_rules dict
    orchestrator.py-->>Client: SSE step: planning complete
    orchestrator.py->>field_mapper.py: map_fields(parsed, tx_rules)
    field_mapper.py-->>orchestrator.py: MappingResult
    orchestrator.py-->>Client: SSE step: mapping complete
    orchestrator.py->>f6_assembler.py: assemble(mapping)
    f6_assembler.py-->>orchestrator.py: f6_output (string)
    orchestrator.py-->>Client: SSE step: assembling complete
    orchestrator.py->>audit_builder.py: build_audit(mapping)
    audit_builder.py-->>orchestrator.py: audit dict
    orchestrator.py-->>Client: SSE result event {tx_type, f6_output, audit}
    main.py->>db_ops.py: complete_conversion() + insert_audit_entries/findings/steps()
    main.py-->>Client: stream closed
```

---

## From Section 5 — Multi-Agent Engine Architecture

```mermaid
graph TD
    A[POST /api/batch/upload] -->|make_job BULK| Q[ConversionQueue<br/>asyncio.PriorityQueue]
    B[POST /api/convert/stream] -->|direct call| ORCH[orchestrator.py<br/>convert_stream]
    C[POST /api/convert] -->|direct call| ORCH

    Q --> AG0[ConversionAgent 0]
    Q --> AG1[ConversionAgent 1]
    Q --> AGN[ConversionAgent N<br/>total: 8 default]

    AG0 --> P1[Phase 1: _phase1<br/>asyncio.to_thread]
    P1 --> P2{LLM escalate?}
    P2 -->|errors / missing / UNKNOWN| LLM[engine/llm_resolver.py<br/>claude-sonnet-4-6]
    P2 -->|no escalation| DB
    LLM --> MERGE[engine/llm_merger.py]
    MERGE --> DB[db_ops.py<br/>complete_conversion<br/>insert_audit_entries]

    RC[RuleCache<br/>warm on startup<br/>refresh every 60s] --> AG0
    RC --> AG1
    RC --> AGN
```
