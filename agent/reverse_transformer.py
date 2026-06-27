"""
agent/reverse_transformer.py

Applies reverse transforms to F6 field values to produce D.0 values.
"""
from __future__ import annotations
import logging

logger = logging.getLogger(__name__)


class ReverseTransformer:

    def apply(self, reverse_rule: dict, f6_value: str) -> tuple[str, bool, str]:
        """
        Apply the reverse transform in reverse_rule to f6_value.
        Returns (d0_value, success, note).
        """
        rt = reverse_rule.get('reverse_transform', 'CARRY_AS_IS').upper()

        if rt == 'STRIP_LEADING_ZEROS':
            return self._strip_leading_zeros(f6_value, reverse_rule)

        if rt == 'SET_VALUE':
            rev_val = reverse_rule.get('reverse_value', f6_value)
            return rev_val, True, f'Set to "{rev_val}" (reverse of SET_VALUE)'

        if rt == 'REVERSE_MAP_CODE':
            rev_map = reverse_rule.get('reverse_map', {})
            d0_val  = rev_map.get(f6_value.strip(), f6_value)
            note = (
                f'Reverse MAP_CODE: "{f6_value}" → "{d0_val}"'
                if d0_val != f6_value
                else f'No reverse mapping for "{f6_value}" — carried'
            )
            return d0_val, True, note

        if rt in ('CARRY_AS_IS', 'CANNOT_REVERSE'):
            return f6_value, True, 'Carried as-is'

        logger.warning('Unknown reverse transform: %r — carrying value', rt)
        return f6_value, True, f'Unknown transform {rt!r} — carried'

    def _strip_leading_zeros(self, value: str, rule: dict) -> tuple[str, bool, str]:
        stripped = value.strip()
        target_len = rule.get('strip_to_length')

        if target_len:
            bare = stripped.lstrip('0')
            if not bare:
                bare = '0'
            if len(bare) < target_len:
                bare = bare.zfill(target_len)
            stripped = bare
        else:
            stripped = stripped.lstrip('0') or '0'

        note = f'STRIP_LEADING_ZEROS: "{value}" → "{stripped}"'
        if target_len:
            note += f' (target {target_len} digits)'
        return stripped, True, note
