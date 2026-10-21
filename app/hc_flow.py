"""Health Check після прогону: оцінка драфтів → порівняння → звіт → алерт. Кроки йдуть підряд, кожен можна запустити окремо
(POST-ендпоінти в main.py або `python -m app.hc_flow <крок> <run_id>`); кожен пише результат у postgen одразу, тож обрив
деплоєм не губить зроблене, а повторний виклик дододає решту.

after_run не кидає винятків: прогін моделі на цей момент уже done, і збій оцінки (немає ключа Anthropic, ліміт, мережа)
не має перетворювати його на failed.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys

from . import hc_compare, hc_eval

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


async def compare_runs(run_id: str, base_id: str | None = None, judge_key: str | None = None, *,
                       evaluate_missing: bool = False) -> hc_compare.Comparison | None:
    """Порівняти прогін з базою (за замовчуванням — попередній завершений) оцінками одного судді. None — порівнювати
    нема з чим: це перший завершений прогін. evaluate_missing: дооцінити бази й прогін поточним суддею (платно, але
    дешево: тексти драфтів уже в базі) — так робить ланцюжок після прогону; GET-ендпоінт цього не робить."""
    base_id = base_id or hc_compare.previous_run(run_id)
    if not base_id:
        return None
    key = judge_key or hc_eval.current_judge_key()
    if evaluate_missing and judge_key is None:
        for rid in (base_id, run_id):
            if hc_compare.pending_evals(rid, key):
                await hc_eval.evaluate_run(rid)
    c = hc_compare.compare(base_id, hc_compare.load_rows(base_id, key), run_id, hc_compare.load_rows(run_id, key))
    c.meta = {"base": hc_compare.run_info(base_id), "new": hc_compare.run_info(run_id), "judge": key}
    for rid in (base_id, run_id):
        if left := hc_compare.pending_evals(rid, key):
            c.warnings.append(f"{rid}: {left} драфтів без оцінки цього судді — порівняння неповне")
    for rid, scopes in ((base_id, c.base), (run_id, c.new)):
        cov = scopes["all"]["coverage"]
        if cov is not None and cov < hc_compare.MIN_COVERAGE:
            c.warnings.append(f"{rid}: оцінено лише {cov:.0%} драфтів — висновкам не варто довіряти")
    if not c.same_articles:
        c.warnings.append("прогони охоплюють різні статті — порівнюємо лише спільні")
    return c


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    usage = "usage: python -m app.hc_flow evaluate <run_id> [--force] | compare <run_id> [<base_run_id>]"
    args = sys.argv[1:]
    if len(args) < 2 or args[0] not in ("evaluate", "compare"):
        sys.exit(usage)
    if args[0] == "evaluate":
        print(asyncio.run(hc_eval.evaluate_run(args[1], force="--force" in args)))
    else:
        result = asyncio.run(compare_runs(args[1], args[2] if len(args) > 2 else None))
        print(json.dumps(result.as_dict() if result else None, ensure_ascii=False, indent=1, default=str))
