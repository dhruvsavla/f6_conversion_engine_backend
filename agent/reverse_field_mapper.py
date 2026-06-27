"""
agent/reverse_field_mapper.py

Maps each field in a ParsedTransaction (F6 input) to its D.0 equivalent,
applying reverse rules segment by segment.

Reverse action types:
  carry              — copy field unchanged
  drop               — omit (F6-only field)
  restore            — recover deprecated D.0 field from ~~strikethrough~~
  reverse_transform  — apply an inverted transform (e.g. strip leading zeros)
  warn_cannot_reverse — cannot invert; emit WARN, omit from output
"""
from __future__ import annotations
import logging
from dataclasses import dataclass, field as dc_field

from .segment_parser import ParsedTransaction, ParsedSegment
from .f6_parser import F6ParsedField
from .reverse_transformer import ReverseTransformer

logger = logging.getLogger(__name__)


@dataclass
class ReverseMappedField:
    field_id:       str
    field_name:     str
    reverse_action: str
    f6_value:       str
    d0_value:       str
    was_restored:   bool = False
    notes:          str  = ''
    warn_code:      str  = ''
    warn_severity:  str  = ''
    warn_message:   str  = ''
    occurrence:     int  = 1


@dataclass
class ReverseMappedSegment:
    segment_id:    str
    normalized_id: str
    occurrence:    int
    raw_index:     int
    # All fields in the order they should appear in D.0 output (carried + transformed)
    d0_fields:    list[ReverseMappedField] = dc_field(default_factory=list)
    # Restored deprecated fields (appended after active fields)
    restored:     list[ReverseMappedField] = dc_field(default_factory=list)
    # Audit-only lists
    dropped:      list[ReverseMappedField] = dc_field(default_factory=list)
    warned:       list[ReverseMappedField] = dc_field(default_factory=list)


@dataclass
class ReverseMappingResult:
    segments:      list[ReverseMappedSegment]
    detected_type: str
    parse_errors:  list = dc_field(default_factory=list)


class ReverseFieldMapper:

    def __init__(self):
        self.transformer = ReverseTransformer()

    def map(
        self,
        tx: ParsedTransaction,
        reverse_rules: dict,
        detected_type: str,
    ) -> ReverseMappingResult:
        mapped = [
            self._map_segment(seg, reverse_rules.get(seg.normalized_id, []))
            for seg in tx.segments
        ]
        return ReverseMappingResult(
            segments=mapped,
            detected_type=detected_type,
            parse_errors=tx.all_errors(),
        )

    def _map_segment(
        self,
        parsed_seg: ParsedSegment,
        seg_rules: list[dict],
    ) -> ReverseMappedSegment:
        ms = ReverseMappedSegment(
            segment_id=parsed_seg.segment_id,
            normalized_id=parsed_seg.normalized_id,
            occurrence=parsed_seg.occurrence,
            raw_index=parsed_seg.raw_index,
        )

        # Separate normal F6 fields from ~~strikethrough~~ restored fields
        f6_map: dict[str, F6ParsedField] = {}
        restored_map: dict[str, F6ParsedField] = {}
        for f in parsed_seg.fields:
            if isinstance(f, F6ParsedField) and f.is_restored_deprecated:
                restored_map[f.field_id] = f
            else:
                f6_map[f.field_id] = f

        handled: set[str] = set()
        occ = parsed_seg.occurrence

        for rule in seg_rules:
            fid   = rule.get('field_id', '')
            fname = rule.get('field_name', fid)
            ract  = rule.get('reverse_action', 'carry')
            handled.add(fid)

            f6f  = f6_map.get(fid)
            restf = restored_map.get(fid)

            if ract == 'carry':
                if f6f is None:
                    continue
                ms.d0_fields.append(ReverseMappedField(
                    field_id=fid, field_name=fname, reverse_action='carry',
                    f6_value=f6f.value, d0_value=f6f.value,
                    notes='Carried unchanged', occurrence=occ,
                ))

            elif ract == 'drop':
                if f6f is None:
                    continue
                ms.dropped.append(ReverseMappedField(
                    field_id=fid, field_name=fname, reverse_action='drop',
                    f6_value=f6f.value, d0_value='',
                    notes=rule.get('notes', 'Dropped — F6-only field'),
                    occurrence=occ,
                ))

            elif ract == 'restore':
                if restf is not None:
                    ms.restored.append(ReverseMappedField(
                        field_id=fid, field_name=fname, reverse_action='restore',
                        f6_value='', d0_value=restf.value,
                        was_restored=True,
                        notes=f'Restored from ~~{fid}={restf.value}~~ in F6 input',
                        occurrence=occ,
                    ))
                else:
                    ms.warned.append(ReverseMappedField(
                        field_id=fid, field_name=fname, reverse_action='warn_cannot_restore',
                        f6_value='', d0_value='',
                        notes=rule.get('notes', ''),
                        warn_code=rule.get('warn_code', f'NORESTORE_{fid}'),
                        warn_severity=rule.get('warn_severity', 'WARN'),
                        warn_message=rule.get('warn_message',
                            f'Field {fid} was deprecated in F6 and original '
                            f'D.0 value could not be recovered.'),
                        occurrence=occ,
                    ))

            elif ract == 'reverse_transform':
                if f6f is None:
                    continue
                d0_val, _, note = self.transformer.apply(rule, f6f.value)
                ms.d0_fields.append(ReverseMappedField(
                    field_id=fid, field_name=fname, reverse_action='reverse_transform',
                    f6_value=f6f.value, d0_value=d0_val,
                    notes=note, occurrence=occ,
                ))

            elif ract == 'warn_cannot_reverse':
                ms.warned.append(ReverseMappedField(
                    field_id=fid, field_name=fname, reverse_action='warn_cannot_reverse',
                    f6_value=f6f.value if f6f else '', d0_value='',
                    notes=rule.get('notes', ''),
                    warn_code=rule.get('warn_code', ''),
                    warn_severity=rule.get('warn_severity', 'WARN'),
                    warn_message=rule.get('warn_message', ''),
                    occurrence=occ,
                ))

        # Implicit carry: any F6 field with no rule is carried unchanged
        for f6f in parsed_seg.fields:
            if f6f.field_id in handled:
                continue
            if isinstance(f6f, F6ParsedField) and f6f.is_restored_deprecated:
                continue  # handled by restore rules above
            ms.d0_fields.append(ReverseMappedField(
                field_id=f6f.field_id, field_name=f6f.field_id,
                reverse_action='carry',
                f6_value=f6f.value, d0_value=f6f.value,
                notes='No reverse rule — implicit carry',
                occurrence=occ,
            ))

        return ms
