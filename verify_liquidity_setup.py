#!/usr/bin/env python3
"""Quick verification script for Liquidity Strategy (Phases 1-5) setup."""

import sys
from pathlib import Path

def check_files():
    """Verify all required files exist."""
    required = [
        "src/liquidity_provider.py",
        "src/liquidity_metrics.py",
        "src/strategies/liquidity.py",
        "tests/test_liquidity_provider.py",
        "tests/test_liquidity_metrics.py",
        "config/config.toml",
        "LIQUIDITY_STRATEGY_SPEC.md",
    ]

    missing = []
    for f in required:
        if not Path(f).exists():
            missing.append(f)

    if missing:
        print("❌ Missing files:")
        for f in missing:
            print(f"  - {f}")
        return False

    print("✅ All required files exist")
    return True


def check_imports():
    """Verify all imports work."""
    try:
        from src.liquidity_provider import LiquidityProvider, MarketPosition, QuoteOrder
        from src.liquidity_metrics import LiquidityMetrics, DailySnapshot
        from src.strategies.liquidity import LiquidityStrategy, LiquidityConfig
        print("✅ All imports successful")
        return True
    except ImportError as e:
        print(f"❌ Import error: {e}")
        return False


def check_config():
    """Verify config.toml has liquidity account."""
    try:
        with open("config/config.toml", "r") as f:
            content = f.read()

        # Check for liquidity account (works with Python 3.9+)
        if "[[accounts]]" not in content or "strategy_type = \"liquidity\"" not in content:
            print("❌ No liquidity account in config")
            return False

        # Check for required liquidity config parameters
        required_params = [
            "capital_per_market",
            "max_markets",
            "quote_refresh_s",
            "use_heartbeat",
            "heartbeat_interval",
        ]

        missing_params = []
        for param in required_params:
            if param not in content:
                missing_params.append(param)

        if missing_params:
            print(f"⚠️  Missing parameters in config: {missing_params}")
            return False

        print("✅ Config has liquidity account with all parameters")
        return True
    except Exception as e:
        print(f"❌ Config validation error: {e}")
        return False


def check_spec():
    """Verify LIQUIDITY_STRATEGY_SPEC.md is up-to-date."""
    with open("LIQUIDITY_STRATEGY_SPEC.md", "r") as f:
        content = f.read()

    required_sections = [
        "Fase 1: Reward Scanner",
        "Fase 2: Liquidity Provider",
        "Fase 3: Risk & Inventory",
        "Fase 3.5: Heartbeat, Scoring & Block Hiding",
        "Fase 4: Integration",
        "Fase 5: Metrics",
    ]

    missing = []
    for section in required_sections:
        if section not in content:
            missing.append(section)

    if missing:
        print("❌ Missing sections in LIQUIDITY_STRATEGY_SPEC.md:")
        for s in missing:
            print(f"  - {s}")
        return False

    print("✅ LIQUIDITY_STRATEGY_SPEC.md has all phase sections")
    return True


def main():
    """Run all checks."""
    print("\n=== Liquidity Strategy Setup Verification ===\n")

    checks = [
        ("Files", check_files),
        ("Imports", check_imports),
        ("Config", check_config),
        ("Spec", check_spec),
    ]

    results = []
    for name, check in checks:
        print(f"\n[{name}]")
        try:
            results.append(check())
        except Exception as e:
            print(f"❌ Error: {e}")
            results.append(False)

    print("\n" + "="*50)
    if all(results):
        print("✅ ALL CHECKS PASSED — Ready for paper/dry_run testing")
        print("\nNext steps:")
        print("1. Run unit tests: python3 -m pytest tests/test_liquidity*.py -v")
        print("2. Start bot in paper mode: docker compose up -d")
        print("3. Access panel: http://localhost:8080")
        print("4. Navigate to /panel/liquidity to monitor")
        return 0
    else:
        print("❌ SOME CHECKS FAILED — Fix issues before testing")
        return 1


if __name__ == "__main__":
    sys.exit(main())
