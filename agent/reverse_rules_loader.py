"""
agent/reverse_rules_loader.py

Loads forward rules (from the rules/ JSON files) and inverts them into
a reverse ruleset for F6 → D.0 conversion.

Forward action → reverse action:
  carry      → carry           (field passes through unchanged)
  transform  → reverse_transform (invert the specific transform)
  add        → drop            (F6-only field, no D.0 equivalent)
  remove     → restore         (deprecated D.0 field, recover from ~~strikethrough~~)
  modify     → carry           (code set changes are non-trivial to invert; carry as-is)
  cases      → drop            (derived field, cannot reverse reliably)
"""
from __future__ import annotations
import logging
from pathlib import Path
from typing import Optional

from . import rules_reader

logger = logging.getLogger(__name__)

RULES_DIR = Path(__file__).parent.parent / "rules"

# Map forward transform names to their reverse counterparts
REVERSE_TRANSFORM_MAP = {
    'ZERO_PAD_LEFT':      'STRIP_LEADING_ZEROS',
    'SET_VALUE':          'SET_VALUE',
    'REMOVE_HYPHENS':     'CARRY_AS_IS',   # information lost — carry unchanged
    'UPPERCASE':          'CARRY_AS_IS',   # cannot reliably un-uppercase
    'MAP_CODE':           'REVERSE_MAP_CODE',
    'DATE_REFORMAT':      'CARRY_AS_IS',
}

D0_VERSION = 'D0'
F6_VERSION  = 'F6'


class ReverseRulesLoader:

    def load(self, transaction_type: str) -> dict:
        """
        Load forward rules for transaction_type and invert them.
        Returns: {segment_id: [reverse_rule_dict, ...]}
        """
        ruleset   = rules_reader.load_all_from_db()
        tx_rules  = ruleset.get_rules_for(transaction_type)
        segments  = tx_rules.get('segments', {})

        reverse_ruleset: dict[str, list[dict]] = {}

        for seg_id, forward_rules in segments.items():
            reversed_list = []
            for rule in (forward_rules or []):
                rev = self._invert_rule(rule, seg_id)
                if rev is not None:
                    reversed_list.append(rev)
            if reversed_list:
                reverse_ruleset[seg_id] = reversed_list

        return reverse_ruleset

    def _invert_rule(self, rule: dict, segment_id: str) -> Optional[dict]:
        field_id = rule.get('field_id', '')
        action   = rule.get('action', 'carry')

        base = {
            'field_id':   field_id,
            'field_name': rule.get('field_name', field_id),
            'segment_id': segment_id,
        }

        if action == 'carry':
            return {**base, 'reverse_action': 'carry', 'notes': 'Carry unchanged'}

        if action == 'transform':
            return self._invert_transform(rule, base)

        if action == 'add':
            return {
                **base,
                'reverse_action': 'drop',
                'notes': f'Drop — {field_id} was added in F6 and has no D.0 equivalent.',
            }

        if action == 'remove':
            return {
                **base,
                'reverse_action': 'restore',
                'notes': f'Restore — {field_id} was deprecated in F6; recover from ~~strikethrough~~ or WARN.',
                'warn_if_unrecoverable': True,
                'warn_code':    f'NORESTORE_{field_id.replace("-","_")}',
                'warn_severity': 'WARN',
                'warn_message': (
                    f'Field {field_id} ({rule.get("field_name","")}) was deprecated in F6. '
                    f'Original D.0 value could not be recovered — field omitted from D.0 output.'
                ),
            }

        if action in ('modify', 'cases'):
            # Modify: code set changes are non-trivial to invert reliably — carry as-is.
            # Cases: derived value cannot be safely reversed.
            return {
                **base,
                'reverse_action': 'drop' if action == 'cases' else 'carry',
                'notes': (
                    'Drop — field was derived by conditional logic in F6.'
                    if action == 'cases'
                    else 'Carry as-is — code set expansion cannot be reliably inverted.'
                ),
            }

        logger.warning('Unknown forward action %r for %s — will carry', action, field_id)
        return {**base, 'reverse_action': 'carry', 'notes': f'Unknown action {action!r} — carried'}

    def invert_from_cache(self, cache_rules: dict[str, list[dict]]) -> dict:
        """
        Build reverse ruleset from a pre-loaded forward rule dict.
        Used by agents that already have rules from the shared cache.

        Args:
            cache_rules: { segment_id: [list of forward rule dicts] }

        Returns:
            { segment_id: [list of reverse rule dicts] }
        """
        reverse_ruleset: dict[str, list[dict]] = {}

        for seg_id, rules in cache_rules.items():
            for rule in rules:
                reverse_rule = self._invert_rule(rule, seg_id)
                if reverse_rule is None:
                    continue
                if seg_id not in reverse_ruleset:
                    reverse_ruleset[seg_id] = []
                reverse_ruleset[seg_id].append(reverse_rule)

        return reverse_ruleset

    def _invert_transform(self, rule: dict, base: dict) -> dict:
        fwd = rule.get('transform', '').upper()
        rev = REVERSE_TRANSFORM_MAP.get(fwd, 'CARRY_AS_IS')
        field_id = base['field_id']

        result: dict = {**base, 'reverse_action': 'reverse_transform', 'reverse_transform': rev}

        if fwd == 'ZERO_PAD_LEFT':
            target_len  = rule.get('params', {}).get('length', 8)
            # Typical D.0 BIN is 6 digits; F6 pads to target_len.
            # We strip back to the un-padded length.
            original_len = target_len - 2 if target_len > 2 else None
            result['strip_to_length'] = original_len
            result['notes'] = (
                f'STRIP_LEADING_ZEROS: reverse ZERO_PAD_LEFT '
                f'(was padded to {target_len} digits)'
            )

        elif fwd == 'SET_VALUE':
            fwd_value = rule.get('value', '')
            if fwd_value == F6_VERSION:
                result['reverse_value'] = D0_VERSION
                result['notes'] = 'Set version back to D0'
            else:
                # Value was overwritten — original is gone. Drop and WARN.
                result['reverse_action'] = 'warn_cannot_reverse'
                result['warn_code'] = f'NOREV_{field_id.replace("-","_")}'
                result['warn_severity'] = 'WARN'
                result['warn_message'] = (
                    f'Field {field_id} was overwritten by SET_VALUE="{fwd_value}" in F6. '
                    f'Original D.0 value cannot be recovered. Field dropped from output.'
                )
                result['notes'] = f'Cannot reverse SET_VALUE="{fwd_value}"'

        elif fwd == 'REMOVE_HYPHENS':
            result['notes'] = (
                'Hyphens were removed in F6 and cannot be restored. Carrying value unchanged.'
            )

        elif fwd == 'MAP_CODE':
            fwd_map = rule.get('map', {})
            result['reverse_map'] = {v: k for k, v in fwd_map.items()}
            result['notes'] = 'Reverse MAP_CODE lookup'

        else:
            result['notes'] = f'No reversal defined for {fwd} — carrying unchanged'

        return result
