"""Health Check після прогону: оцінка драфтів → порівняння → звіт → алерт. Кроки йдуть підряд, кожен можна запустити окремо
(POST-ендпоінти в main.py або `python -m app.hc_flow <крок> <run_id>`); кожен пише результат у postgen одразу, тож обрив
деплоєм не губить зроблене, а повторний виклик дододає решту.

after_run не кидає винятків: прогін моделі на цей момент уже done, і збій оцінки (немає ключа Anthropic, ліміт, мережа)
не має перетворювати його на failed.
"""
from __future__ import annotations

import asyncio
import logging
import sys

from . import hc_eval

log = logging.getLogger("hc_flow")

_busy: set[str] = set()  # прогони, для яких кроки після прогону йдуть просто зараз


def is_busy(run_id: str) -> bool:
    return run_id in _busy


async def after_run(run_id: str, *, judge: hc_eval.Judge | None = None, force: bool = False) -> None:
    if run_id in _busy:
        return
    _busy.add(run_id)
    try:
        await hc_eval.evaluate_run(run_id, judge, force=force)
    except Exception:
        log.exception("health check %s: кроки після прогону не завершились", run_id)
    finally:
        _busy.discard(run_id)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    usage = "usage: python -m app.hc_flow evaluate <run_id> [--force]"
    args = sys.argv[1:]
    if len(args) < 2 or args[0] != "evaluate":
        sys.exit(usage)
    print(asyncio.run(hc_eval.evaluate_run(args[1], force="--force" in args)))
