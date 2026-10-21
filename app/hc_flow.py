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

from . import hc_alert, hc_compare, hc_eval, hc_report

log = logging.getLogger("hc_flow")

_busy: set[str] = set()  # прогони, для яких кроки після прогону йдуть просто зараз


def is_busy(run_id: str) -> bool:
    return run_id in _busy


async def after_run(run_id: str, *, judge: hc_eval.Judge | None = None, force: bool = False) -> None:
    """Оцінка драфтів → звіт (порівняння з попереднім і еталонним прогоном; зберегти й опублікувати)."""
    if run_id in _busy:
        return
    _busy.add(run_id)
    try:
        await hc_eval.evaluate_run(run_id, judge, force=force)
        await make_report(run_id, judge_key=judge.key if judge else None, evaluate_missing=judge is None, save=True)
    except Exception as e:
        log.exception("health check %s: кроки після прогону не завершились", run_id)
        await _report_problem(f"Health Check {run_id}: оцінка чи звіт не завершились", e)
    finally:
        _busy.discard(run_id)


async def _report_problem(subject: str, error: Exception) -> None:
    """Без цього збій оцінки (ключ, кошти, мережа) лишався б у журналі, а тижні минали б без звітів."""
    try:
        await hc_alert.notify_problem(subject, f"{type(error).__name__}: {error}")
    except Exception:
        log.exception("не вдалося повідомити про збій Health Check")


def followup_pending(run_id: str) -> bool:
    """Оцінку чи звіт прогону не завершено (їх обірвав деплой). Драфти, де суддя вже збоїв, не рахуємо: їх повторюють
    лише ручний запуск і наступний прогін, а не щогодинний супровід."""
    key = hc_eval.current_judge_key()
    return hc_compare.pending_evals(run_id, key, retry_errors=False) > 0 or hc_report.get_report(run_id, key) is None


async def resume_followups() -> None:
    """Дозавершити кроки останнього завершеного прогону. Викликається на старті бекенду й щогодини розкладом."""
    if not hc_eval.enabled():
        return
    try:
        run_id = hc_compare.latest_run("done")
        if run_id and not is_busy(run_id) and followup_pending(run_id):
            log.info("health check %s: дозавершую оцінку й звіт", run_id)
            await after_run(run_id)
    except Exception:
        log.exception("health check: супровід останнього прогону")


async def rebuild_report(run_id: str, republish: bool = False) -> None:
    """Зібрати звіт наново (дооцінивши те, чого бракує), зберегти й опублікувати; не кидає винятків."""
    if run_id in _busy:
        return
    _busy.add(run_id)
    try:
        await make_report(run_id, evaluate_missing=hc_eval.enabled(), save=True, republish=republish)
    except Exception:
        log.exception("health check %s: звіт не зібрано", run_id)
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


async def make_report(run_id: str, base_id: str | None = None, judge_key: str | None = None, *,
                      evaluate_missing: bool = False, save: bool = False, republish: bool = False) -> str | None:
    """Звіт по прогону (markdown): з попереднім завершеним прогоном і, якщо він інший, з еталонним. None — прогону нема.
    save: зберегти в hc_reports і опублікувати в Notion (якщо задано NOTION_HEALTHCHECK_PAGE_ID); збій Notion звіт не губить.
    Сторінку в Notion створюємо один раз на прогін: перебудова звіту нових сторінок не плодить, republish — навмисно ще одну."""
    run = hc_compare.run_info(run_id)
    if not run:
        return None
    key = judge_key or hc_eval.current_judge_key()
    prev = await compare_runs(run_id, base_id, judge_key, evaluate_missing=evaluate_missing)
    baseline_id = hc_compare.baseline_run(run_id)
    baseline = None
    if baseline_id and (prev is None or baseline_id != prev.base_run):
        baseline = await compare_runs(run_id, baseline_id, judge_key, evaluate_missing=evaluate_missing)
    ref = prev or baseline
    summary = ref.new if ref else hc_compare.summarize_scopes(hc_compare.load_rows(run_id, key))
    health = hc_alert.assess(prev, baseline, summary)  # без бази й без збоїв це «info»: перший прогін сам стає базою
    markdown = hc_report.render(run, summary, key, prev=prev, baseline=baseline, level=health.level, reasons=health.reasons)
    if save:
        existing = hc_report.get_report(run_id, key)
        url = existing["notion_url"] if existing else None
        if republish or not url:
            try:
                url = await hc_report.publish(run_id, markdown, health.level) or url
            except Exception:
                log.exception("health check %s: звіт не опубліковано в Notion", run_id)
        hc_report.save_report(run_id, key, markdown=markdown, summary=summary, base_run=prev.base_run if prev else None,
                              baseline_run=baseline.base_run if baseline else None, level=health.level,
                              reasons=health.reasons, notion_url=url)
        try:
            stored = hc_report.get_report(run_id, key)
            await hc_alert.deliver(run_id, key, health, run, url or (stored["notion_url"] if stored else None))
        except Exception:
            log.exception("health check %s: алерт не надіслано", run_id)
    return markdown


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    usage = ("usage: python -m app.hc_flow evaluate <run_id> [--force] | compare <run_id> [<base_run_id>] | "
             "report <run_id> [<base_run_id>] [--save]")
    args = sys.argv[1:]
    if len(args) < 2 or args[0] not in ("evaluate", "compare", "report"):
        sys.exit(usage)
    ids = [a for a in args[1:] if not a.startswith("--")]
    if args[0] == "evaluate":
        print(asyncio.run(hc_eval.evaluate_run(ids[0], force="--force" in args)))
    elif args[0] == "compare":
        result = asyncio.run(compare_runs(ids[0], ids[1] if len(ids) > 1 else None))
        print(json.dumps(result.as_dict() if result else None, ensure_ascii=False, indent=1, default=str))
    else:
        print(asyncio.run(make_report(ids[0], ids[1] if len(ids) > 1 else None, save="--save" in args)))
