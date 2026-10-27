"""``aiteam attribution``: repair historical usage-attribution rows (dry run by default).

The logic and the write discipline live in :mod:`aiteam.services.attribution_repair`;
this module parses options and prints. Imports of the service happen inside the
command so ``aiteam --help`` stays light.
"""

from __future__ import annotations

from pathlib import Path

import typer

app = typer.Typer(
    help="用量归因的历史行修正：补 harness、标出 resume 空壳与重复 Leader、记下没有可计用量的行（默认只预览）",
    no_args_is_help=True,
)

EXIT_OK = 0
EXIT_REFUSED = 1

_KIND_LABEL = {"harness": "harness", "exclusion": "排除", "unmeasurable": "无可计用量"}


@app.callback()
def attribution() -> None:
    """用量归因的历史行修正。"""


def _echo_plan(plan, sample: int) -> None:
    from aiteam.services.attribution_repair import UNMARKED_CLOCK

    typer.echo(f"数据库：{plan.db}")
    if plan.user_version == UNMARKED_CLOCK:
        typer.echo(
            "时钟制式：PRAGMA user_version = 0（未标注）。"
            "按 UTC 与本机时区两种读法各判一次，只写两种读法一致的结论"
        )
    else:
        typer.echo(f"时钟制式：PRAGMA user_version = {plan.user_version}（UTC）")
    if plan.missing_columns:
        typer.echo(
            f"注意：agents 缺少列 {', '.join(plan.missing_columns)}（API 还没在新版本上重启）；"
            "预览按空值处理，--apply 会拒绝"
        )
    typer.echo(f"计划写入 {plan.cells} 格（{plan.rows} 行）")
    for key, count in plan.counts().items():
        typer.echo(f"  {key}: {count}")
    evidence = plan.harness_evidence()
    if evidence:
        typer.echo("harness 按证据规则：")
        for key, count in evidence.items():
            typer.echo(f"  {key}: {count}")
    shown = plan.decisions[:sample]
    if shown:
        typer.echo(f"样例（前 {len(shown)} 项）：")
        for decision in shown:
            cells = ", ".join(f"{column}={value}" for column, value in decision.cells)
            typer.echo(f"  - {decision.agent_id} [{_KIND_LABEL[decision.kind]}] {cells}  证据：{decision.evidence}")
        if len(plan.decisions) > len(shown):
            typer.echo(f"  …… 另有 {len(plan.decisions) - len(shown)} 项（--apply 时全部写进 journal）")
    typer.echo(f"在飞、本次不下结论的未测量行：{plan.in_flight}")
    typer.echo(f"认不出来源、保持未标注 harness 的行：{plan.unlabelled}")
    typer.echo(f"时间值读不了、本次不下时间相关结论的行：{plan.unreadable}（所在的重复组整组留待下次）")


def _echo_mismatches(mismatches: list[dict], limit: int = 10) -> None:
    for item in mismatches[:limit]:
        if "group" in item:
            typer.echo(f"  - 共用 transcript 的 Leader 组变了：{item['group']}")
        elif item.get("field") is None:
            typer.echo(f"  - {item['agent_id']}：行已不存在")
        else:
            typer.echo(f"  - {item['agent_id']}.{item['field']}：计划时 {item['before']!r}，现在 {item['now']!r}")
    if len(mismatches) > limit:
        typer.echo(f"  …… 另有 {len(mismatches) - limit} 处")


def _undo(db: Path, journal: Path, apply: bool, sample: int) -> int:
    from aiteam.services.attribution_repair import STATUS_PENDING, undo_repair

    outcome = undo_repair(db, journal, apply=apply)
    typer.echo(f"数据库：{outcome.journal['db']}")
    typer.echo(f"journal：{journal}（写入于 {outcome.journal.get('planned_at')}，{outcome.journal.get('cells')} 格）")
    if outcome.journal.get("status") == STATUS_PENDING:
        typer.echo("注意：这份 journal 没有记下提交结果（写入时进程可能中断）；下面按每格的现值判断")
    typer.echo(
        f"可撤回 {len(outcome.revert)} 项，已是原值 {len(outcome.already)} 项，"
        f"之后被改动而保留 {len(outcome.changed)} 项"
    )
    for change in outcome.changed[:sample]:
        label = _KIND_LABEL.get(change["kind"], change["kind"])
        typer.echo(f"  - 保留 {change['agent_id']} [{label}]：现值 {change['now']}")
    if outcome.status == "preview":
        typer.echo("预览：没有改动任何数据。加 --apply 撤回。")
        return EXIT_OK
    if outcome.status == "nothing":
        typer.echo("没有需要撤回的格。")
        return EXIT_OK
    if outcome.status == "undone":
        cells = sum(len(change["cells"]) for change in outcome.revert)
        where = outcome.record or "撤回记录没写成（数据库已撤回）"
        typer.echo(f"已撤回 {len(outcome.revert)} 项（{cells} 格），记录：{where}")
        return EXIT_OK
    typer.echo(f"撤回失败：{outcome.reason}", err=True)
    return EXIT_REFUSED


@app.command("repair")
def repair(
    apply: bool = typer.Option(False, "--apply", help="真正写入（默认只预览，不写任何东西）"),
    undo: Path | None = typer.Option(
        None, "--undo", metavar="JOURNAL", help="撤回某次 --apply：按它的 journal 把写过的格改回原值（同样默认只预览）",
    ),
    db: Path | None = typer.Option(
        None, "--db",
        help="数据库路径；默认与 API 相同（AITEAM_DB_PATH，或 OS 数据目录里的 aiteam.db，aiteam paths 可查）",
    ),
    out_dir: Path | None = typer.Option(
        None, "--out-dir", help="--apply 的备份与 journal 存放目录；默认数据库旁的 attribution-repair/",
    ),
    sample: int = typer.Option(10, "--sample", min=0, help="输出里列出的样例条数"),
) -> None:
    """修正升级前留下的历史行，让用量归因页的数字可信。

    判定规则只有一份（aiteam.services.attribution_marks），统计侧只读这里写下的标记。

    - harness 为空的旧行：凭证据补标 claude-code 或 codex，认不出的保持未标注。
    - resume 空壳 Leader 行、同一份 transcript 上的重复 Leader 行：打排除标记，不删行。
    - 已结束、没有一次计费调用的行：记下 api_error_only 或 no_usage_lines。
    - 还在跑的行（busy，或 30 分钟内有活动）这次不下结论。

    默认只预览：列出要写的格数、分布与样例，不写任何东西。

    --apply：核对时钟制式，整库备份，写 journal，再单事务逐列比对 before-image 后写入。
    任何一列对不上就整事务回滚。只填空着的格，不删行，重跑不会再有改动。
    最终没有写入（回滚，或备份期间行变了、重新计划后无事可做）时删掉这份备份。

    --undo JOURNAL：预览撤回；加 --apply 才写。之后被改动过的格保留并列出。

    aiteam 不在 PATH 上时，用运行 OS 的解释器：python3 -m aiteam attribution repair

    退出码：0 完成（含无事可做）；1 拒绝或已回滚，数据库未被改动。
    """
    from aiteam.json_integrity import default_db_path
    from aiteam.services.attribution_repair import (
        STATUS_APPLIED,
        Probes,
        RepairRefusedError,
        apply_repair,
        canonical,
        preview,
    )

    target = canonical(db or default_db_path())
    try:
        if undo is not None:
            raise typer.Exit(_undo(target, undo.expanduser(), apply, sample))
        probes = Probes()
        if not apply:
            _echo_plan(preview(target, probes), sample)
            typer.echo("预览：没有写入任何数据。加 --apply 写入（先整库备份、写 journal，再单事务写入）。")
            raise typer.Exit(EXIT_OK)
        outcome = apply_repair(target, out_dir=(out_dir or target.parent / "attribution-repair").expanduser(),
                               probes=probes)
    except RepairRefusedError as exc:
        typer.echo(f"拒绝执行：{exc}", err=True)
        raise typer.Exit(EXIT_REFUSED) from exc

    _echo_plan(outcome.plan, sample)
    if outcome.backup_discarded:
        typer.echo(f"没有写入，备份已删除：{outcome.backup}")
    elif outcome.backup is not None:
        typer.echo(f"备份：{outcome.backup}")
    if outcome.status == "nothing":
        typer.echo("没有要写入的格（已经修正过，或没有符合规则的行）。")
        if outcome.reason:
            typer.echo(f"注意：{outcome.reason}", err=True)
        raise typer.Exit(EXIT_OK)
    typer.echo(f"journal：{outcome.journal}")
    if outcome.status == STATUS_APPLIED:
        typer.echo(f"已写入 {outcome.plan.cells} 格。撤回：aiteam attribution repair --undo {outcome.journal} --apply")
        if outcome.reason:
            typer.echo(f"注意：{outcome.reason}", err=True)
        raise typer.Exit(EXIT_OK)
    typer.echo(f"未写入：{outcome.reason}", err=True)
    _echo_mismatches(outcome.mismatches)
    typer.echo("数据库没有被改动；稍后重跑即可（会重新备份与计划）。", err=True)
    raise typer.Exit(EXIT_REFUSED)
