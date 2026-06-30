"""
backend/models/schemas.py

Shared dataclasses for the mapping pipeline.
These are distinct from the parser dataclasses (ParsedField, ParsedSegment, etc.)
which live in agent/segment_parser.py.
"""
from __future__ import annotations
from dataclasses import dataclass, field as dc_field


@dataclass
class MappedField:
    field_id: str
    field_name: str
    change_type: str          # 'carried' | 'transformed' | 'added' | 'removed' | 'modified' | 'missing'
    old_value: str
    new_value: str
    rule_applied: str
    notes: str
    occurrence: int = 1       # from parent segment
    raw_index: int = 0        # original D.0 position — used to restore D.0 field order in the assembler
    condition_evaluated: bool = False
    condition_passed: bool = True
    condition_expression: str = ""


@dataclass
class MappedSegment:
    segment_id: str
    normalized_id: str
    occurrence: int
    raw_index: int
    carried: list[MappedField] = dc_field(default_factory=list)
    transformed: list[MappedField] = dc_field(default_factory=list)
    added: list[MappedField] = dc_field(default_factory=list)
    removed: list[MappedField] = dc_field(default_factory=list)
    modified: list[MappedField] = dc_field(default_factory=list)
    missing: list[MappedField] = dc_field(default_factory=list)

    def all_fields(self) -> list[MappedField]:
        """All fields in this segment across all change types."""
        return (self.carried + self.transformed + self.added +
                self.removed + self.modified + self.missing)

    def in_place_fields(self) -> list[MappedField]:
        """
        Fields that stay in the output at their original position.
        Sorted by raw_index to restore D.0 field order.
        """
        return sorted(
            self.carried + self.transformed + self.modified,
            key=lambda f: f.raw_index
        )


@dataclass
class AuditEntry:
    segment: str
    occurrence: int
    from_field_id: str
    to_field_id: str
    field_name: str
    change_type: str
    old_value: str
    new_value: str
    rule_applied: str
    notes: str
    condition_evaluated: bool = False
    condition_result: bool = True
    condition_expression: str = ""


@dataclass
class AuditFinding:
    severity: str         # 'WARN' | 'ERROR'
    code: str
    message: str
    segment: str
    field_id: str
    occurrence: int = 1


@dataclass
class AuditSummary:
    added: int = 0
    carried: int = 0
    transformed: int = 0
    removed: int = 0
    modified: int = 0
    missing: int = 0
    warnings: int = 0
    errors: int = 0


@dataclass
class MappingResult:
    segments: list[MappedSegment]
    detected_type: str
    findings: list[dict] = dc_field(default_factory=list)
    parse_errors: list = dc_field(default_factory=list)

    @property
    def segment_order(self) -> list[str]:
        """Unique normalized segment IDs in original order (backward compat)."""
        seen: dict = {}
        for seg in self.segments:
            seen.setdefault(seg.normalized_id, True)
        return list(seen.keys())
