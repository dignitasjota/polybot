#!/usr/bin/env python3
"""Migrate config from old single-strategy format to new multi-strategy format.

Old format:
  [[accounts]]
  name = "directional_main"
  strategy_type = "directional"
  execution_mode = "paper"
  [accounts.copy_trade]
  ...

New format:
  [[accounts]]
  name = "directional_main"
  [accounts.strategies.directional]
  mode = "paper"
  max_price = 0.70
  ...
  [accounts.strategies.copy_trade]
  mode = "paper"
  target_wallets = [...]
  ...

Usage:
  python scripts/migrate_config.py [--merge] [config_path]

Flags:
  --merge   Merge all accounts into one (requires same wallet)
  Default is to keep accounts separate (safest).
"""

import shutil
import sys
from datetime import datetime
from pathlib import Path

import tomli
import tomli_w


def migrate(config_path: Path, merge: bool = False):
    if not config_path.exists():
        print(f"Error: {config_path} not found")
        sys.exit(1)

    # Backup
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = config_path.with_suffix(f".toml.backup_{ts}")
    shutil.copy2(config_path, backup_path)
    print(f"Backup created: {backup_path}")

    # Read old format
    with open(config_path, "rb") as f:
        old = tomli.load(f)

    # Check if already migrated
    for acc in old.get("accounts", []):
        if "strategies" in acc:
            print("Config already in new format. Nothing to do.")
            return

    # Global strategy config (directional params)
    global_strategy = old.get("strategy", {})
    global_risk = old.get("risk", {})

    # Collect enabled accounts
    accounts_old = [a for a in old.get("accounts", []) if a.get("enabled", True)]

    if merge and len(accounts_old) > 1:
        new_accounts = [_merge_accounts(accounts_old, global_strategy, global_risk)]
        print(f"Merged {len(accounts_old)} accounts into 1")
    else:
        new_accounts = [
            _convert_account(acc, global_strategy, global_risk)
            for acc in accounts_old
        ]
        print(f"Converted {len(new_accounts)} accounts (kept separate)")

    # Build new config
    new = {}

    # Preserve non-account sections as-is
    for section in ("strategy", "risk", "data", "websocket", "logging"):
        if section in old:
            new[section] = old[section]

    new["accounts"] = new_accounts

    # Write
    with open(config_path, "wb") as f:
        tomli_w.dump(new, f)

    print(f"\nMigration complete: {config_path}")
    print(f"Backup at: {backup_path}")
    print("\nNext steps:")
    print("  1. Review the new config file")
    print("  2. docker compose down && docker compose up -d")


def _convert_account(acc: dict, global_strategy: dict, global_risk: dict) -> dict:
    """Convert a single old-format account to new format."""
    strategy_type = acc.get("strategy_type", "directional")
    execution_mode = acc.get("execution_mode", "paper")

    # Map execution_mode to per-strategy mode
    mode = "paper" if execution_mode == "paper" else execution_mode

    new_acc = {
        "name": acc.get("name", "default"),
        "enabled": acc.get("enabled", True),
    }

    # Credentials
    if "credentials" in acc:
        new_acc["credentials"] = acc["credentials"]

    # Risk (account-level overrides)
    if "risk" in acc:
        new_acc["risk"] = acc["risk"]

    # Build strategies section
    strategies = {}

    if strategy_type == "directional":
        dir_cfg = {}
        dir_cfg["mode"] = mode
        dir_cfg["priority"] = 10
        # Copy relevant fields from global [strategy]
        for key in ("max_price", "min_buffer_pct", "min_margin_net", "tag",
                     "max_concurrent_bets", "max_time_to_resolution",
                     "enabled", "name"):
            if key in global_strategy:
                dir_cfg[key] = global_strategy[key]
        # Crypto configs and probability tiers (inline)
        if "crypto_configs" in global_strategy:
            dir_cfg["crypto_configs"] = global_strategy["crypto_configs"]
        if "probability_tiers" in global_strategy:
            dir_cfg["probability_tiers"] = global_strategy["probability_tiers"]
        strategies["directional"] = dir_cfg

    elif strategy_type == "copy_trade":
        ct_cfg = {}
        ct_cfg["mode"] = mode
        ct_cfg["priority"] = 5
        # Copy from [accounts.copy_trade]
        if "copy_trade" in acc:
            ct_cfg.update(acc["copy_trade"])
        strategies["copy_trade"] = ct_cfg

    new_acc["strategies"] = strategies
    return new_acc


def _merge_accounts(accounts: list[dict], global_strategy: dict, global_risk: dict) -> dict:
    """Merge multiple accounts into one with multiple strategies."""
    merged = {
        "name": "main",
        "enabled": True,
        "strategies": {},
    }

    # Use first account's credentials and risk
    if "credentials" in accounts[0]:
        merged["credentials"] = accounts[0]["credentials"]
    if "risk" in accounts[0]:
        merged["risk"] = accounts[0]["risk"]

    for acc in accounts:
        strategy_type = acc.get("strategy_type", "directional")
        execution_mode = acc.get("execution_mode", "paper")
        mode = "paper" if execution_mode == "paper" else execution_mode

        if strategy_type == "directional":
            dir_cfg = {"mode": mode, "priority": 10}
            for key in ("max_price", "min_buffer_pct", "min_margin_net", "tag",
                         "max_concurrent_bets", "max_time_to_resolution",
                         "enabled", "name"):
                if key in global_strategy:
                    dir_cfg[key] = global_strategy[key]
            if "crypto_configs" in global_strategy:
                dir_cfg["crypto_configs"] = global_strategy["crypto_configs"]
            if "probability_tiers" in global_strategy:
                dir_cfg["probability_tiers"] = global_strategy["probability_tiers"]
            merged["strategies"]["directional"] = dir_cfg

        elif strategy_type == "copy_trade":
            ct_cfg = {"mode": mode, "priority": 5}
            if "copy_trade" in acc:
                ct_cfg.update(acc["copy_trade"])
            merged["strategies"]["copy_trade"] = ct_cfg

    return merged


if __name__ == "__main__":
    args = sys.argv[1:]
    do_merge = "--merge" in args
    args = [a for a in args if not a.startswith("--")]
    path = Path(args[0]) if args else Path("config/config.toml")
    migrate(path, merge=do_merge)
