import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List


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
        return self.rules_by_tx.get(tx_type) or self.rules_by_tx.get("RETAIL", {})


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
            rules_by_tx[tx_type] = data

        loaded_files.append(f.name)

    return RuleSet(files=loaded_files, rules_by_tx=rules_by_tx, global_config=global_config)
