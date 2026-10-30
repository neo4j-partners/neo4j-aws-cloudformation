"""Test result tracking and summary output."""

from __future__ import annotations

import logging
import time

log = logging.getLogger(__name__)


class TestReporter:
    def __init__(self) -> None:
        self._results: list[tuple[str, bool, str, float]] = []

    def had_failures(self) -> bool:
        return any(not ok for _, ok, _, _ in self._results)

    def record(self, name: str, passed: bool, detail: str, elapsed: float) -> None:
        self._results.append((name, passed, detail, elapsed))
        status = "PASS" if passed else "FAIL"
        log.info("  %s: %s  (%.1fs)", status, detail, elapsed)

    def summary(self, *, stack_name: str, bastion_id: str) -> int:
        total = len(self._results)
        passed = sum(1 for _, ok, _, _ in self._results if ok)
        failed = total - passed
        total_elapsed = sum(e for _, _, _, e in self._results)

        log.info("")
        if failed == 0:
            log.info("  RESULT: All %d tests PASSED  (total: %.1fs)", total, total_elapsed)
        else:
            log.info(
                "  RESULT: %d of %d tests FAILED  (total: %.1fs)", failed, total, total_elapsed
            )
            log.info("")
            log.info("  Failed tests:")
            for name, ok, detail, _ in self._results:
                if not ok:
                    log.info("    - %s: %s", name, detail)
            log.info("")
            log.info("  Diagnose with: uv run preflight")
            log.info("  Bastion logs:  aws ssm send-command --instance-ids %s "
                     "--document-name AWS-RunShellScript "
                     "--parameters 'commands=[\"tail -50 /var/log/cloud-init-output.log\"]'",
                     bastion_id)

        return 0 if failed == 0 else 1
