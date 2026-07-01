import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class RuleSet:
    files: List[str]
    rules_by_tx: Dict[str, Any]
    global_config: Dict[str, Any]

    @property
    def total_field_rules(self) -> int:
        total = 0
        for rules in self.rules_by_tx.values():
            for seg_rules in rules.get("segments", {}).values():
                total += len(seg_rules)
        return total

    @property
    def transaction_types(self) -> List[str]:
        return list(self.rules_by_tx.keys())

    def get_rules_for(self, tx_type: str) -> Dict[str, Any]:
        retail = self.rules_by_tx.get("RETAIL", {})
        if tx_type == "RETAIL" or tx_type not in self.rules_by_tx:
            return retail
        tx_specific = self.rules_by_tx[tx_type]

        # Field-level merge: for each segment, RETAIL fields are the base.
        # tx-specific rules override individual fields by field_id; RETAIL fields
        # with no matching tx-specific field_id pass through unchanged. tx-specific
        # fields not present in RETAIL are appended. Segments only in tx-specific
        # (e.g. CMP, FAC, PA) are included as-is; segments only in RETAIL are
        # included as-is. This ensures mandatory RETAIL fields like 147-U7
        # (Pharmacy Service Type) are never silently dropped by tx-specific files
        # that don't redeclare every CLM field.
        retail_segs  = retail.get("segments", {})
        tx_segs      = tx_specific.get("segments", {})
        merged_segments: Dict[str, Any] = {}

        for seg_id in set(retail_segs) | set(tx_segs):
            retail_fields = retail_segs.get(seg_id, [])
            tx_fields     = tx_segs.get(seg_id, [])

            if not tx_fields:
                merged_segments[seg_id] = retail_fields
            elif not retail_fields:
                merged_segments[seg_id] = tx_fields
            else:
                retail_by_id = {r["field_id"]: r for r in retail_fields}
                tx_by_id     = {r["field_id"]: r for r in tx_fields}
                # RETAIL order first, tx-specific overrides where field_id matches
                merged = [
                    tx_by_id[r["field_id"]] if r["field_id"] in tx_by_id else r
                    for r in retail_fields
                ]
                # Append tx-specific fields not present in RETAIL
                for r in tx_fields:
                    if r["field_id"] not in retail_by_id:
                        merged.append(r)
                merged_segments[seg_id] = merged

        return {**retail, **tx_specific, "segments": merged_segments}


def load_all(rules_dir: str) -> RuleSet:
    """Load and merge all .json rule files from the given directory."""
    path = Path(rules_dir)
    files = sorted(path.glob("*.json"))

    loaded_files: List[str] = []
    rules_by_tx: Dict[str, Any] = {}
    global_config: Dict[str, Any] = {}

    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, IOError, OSError):
            continue  # skip invalid or unreadable files

        if data.get("type") == "global":
            global_config = data
        elif "transaction_type" in data:
            tx_type = data["transaction_type"]
            if tx_type not in rules_by_tx:
                rules_by_tx[tx_type] = data
            else:
                # Merge segments from multiple files with the same transaction_type
                existing_segs = rules_by_tx[tx_type].setdefault("segments", {})
                for seg_id, seg_rules in data.get("segments", {}).items():
                    if seg_id in existing_segs:
                        existing_segs[seg_id].extend(seg_rules)
                    else:
                        existing_segs[seg_id] = list(seg_rules)

        loaded_files.append(f.name)

    return RuleSet(files=loaded_files, rules_by_tx=rules_by_tx, global_config=global_config)


def load_all_from_db(rule_set_id: Optional[str] = None) -> RuleSet:
    """
    Build a RuleSet from the DB 'rules' table instead of rules/*.json files.

    Reconstructs the exact nested shape load_all() produces, so get_rules_for()
    and every caller work unchanged. Always scoped to a single rule_set_id —
    never reads across rule sets — to avoid silently merging in stale/inactive
    sets. Rows are ordered by rowid (insertion order) rather than field_id,
    since f6_assembler relies on rules-iteration order for action="add" fields;
    when the same field_id appears twice for a (transaction_type, segment_id)
    (e.g. an "ALL"-expanded base rule plus an explicit tx-specific override),
    the later row wins but keeps the position of the first occurrence.
    """
    import db_ops
    from database import db

    if rule_set_id is None:
        active = db_ops.get_active_rule_set()
        if not active:
            return RuleSet(files=[], rules_by_tx={}, global_config={})
        rule_set_id = active["id"]

    with db() as conn:
        rows = conn.execute(
            """
            SELECT transaction_type, segment_id, field_id, rule_json
            FROM rules
            WHERE rule_set_id = %s
            ORDER BY segment_id, field_id
            """,
            (rule_set_id,),
        ).fetchall()

    rules_by_tx: Dict[str, Any] = {}
    field_index: Dict[Any, Dict[str, int]] = {}

    for row in rows:
        tx_type  = row["transaction_type"]
        seg_id   = row["segment_id"]
        field_id = row["field_id"]
        rule     = json.loads(row["rule_json"])

        tx_entry = rules_by_tx.setdefault(tx_type, {"transaction_type": tx_type, "segments": {}})
        seg_list = tx_entry["segments"].setdefault(seg_id, [])
        idx_map  = field_index.setdefault((tx_type, seg_id), {})

        if field_id in idx_map:
            seg_list[idx_map[field_id]] = rule
        else:
            idx_map[field_id] = len(seg_list)
            seg_list.append(rule)

    return RuleSet(
        files=[f"db:rule_set={rule_set_id}"],
        rules_by_tx=rules_by_tx,
        global_config={},
    )
