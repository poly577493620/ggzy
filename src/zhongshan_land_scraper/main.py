"""CLI entry point for Zhongshan land scraper."""

import asyncio
import json
import re
import sys
from pathlib import Path

import click
import structlog

from zhongshan_land_scraper.models import OutputRecord, ScrapedRecord
from zhongshan_land_scraper.output_writer import write_all_outputs
from zhongshan_land_scraper.scraper import run_phase1, run_scraper


def configure_logging() -> None:
    """配置结构化日志."""
    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.UnicodeDecoder(),
            structlog.dev.ConsoleRenderer(),
        ],
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )


def _extract_notice_id_from_url(url: str) -> str:
    """从详情链接中提取 noticeId 作为去重键."""
    match = re.search(r"noticeId=([a-f0-9-]+)", url)
    return match.group(1) if match else url


def _merge_with_existing(
    new_records: list[OutputRecord],
    output_dir: Path,
) -> list[OutputRecord]:
    """将新记录与已有输出文件合并，以 noticeId 去重（新记录优先）.

    Args:
        new_records: 本次采集的输出记录
        output_dir: 输出目录

    Returns:
        合并后的完整记录列表
    """
    existing_path = output_dir / "zhongshan_land_transactions.json"
    if not existing_path.exists():
        return new_records

    try:
        with open(existing_path, encoding="utf-8") as f:
            existing_data = json.load(f)
    except Exception:
        return new_records

    if not isinstance(existing_data, list):
        return new_records

    # 以 noticeId 为键构建合并字典
    merged: dict[str, OutputRecord] = {}
    for item in existing_data:
        if not isinstance(item, dict):
            continue
        url = item.get("公告详情链接", "")
        key = _extract_notice_id_from_url(url)
        merged[key] = OutputRecord(**item)

    for r in new_records:
        key = _extract_notice_id_from_url(r.公告详情链接)
        merged[key] = r

    return list(merged.values())


@click.command()
@click.option("--pages", default=10, type=int, help="最大采集页数（默认10）")
@click.option("--limit", default=None, type=int, help="最大采集条数（默认不限）")
@click.option("--output", default="output", type=str, help="输出目录（默认output）")
@click.option("--headless/--no-headless", default=False, help="是否使用无头模式")
@click.option("--browser-channel", default="chrome", type=str, help="浏览器渠道（默认chrome）")
@click.option("--resume/--no-resume", default=True, help="是否断点续传（默认启用）")
@click.option("--list-only", is_flag=True, default=False, help="仅采集列表，不进入详情")
@click.option("--delay", default=1.2, type=float, help="操作间隔秒数（默认1.2，不得小于1.0）")
@click.option("--phase", default=None, type=int, help="执行阶段（1=测试2条，留空=完整采集）")
def main(
    pages: int,
    limit: int | None,
    output: str,
    headless: bool,
    browser_channel: str,
    resume: bool,
    list_only: bool,
    delay: float,
    phase: int | None,
) -> None:
    """中山市土地交易结果公告采集工具."""
    configure_logging()
    log = structlog.get_logger()

    # 验证 delay
    if delay < 1.0:
        log.warning("delay_below_minimum", requested=delay, minimum=1.0)
        delay = 1.0
        click.echo("警告: delay 已调整为最小值 1.0 秒")

    output_dir = Path(output)
    log.info(
        "cli_started",
        pages=pages,
        limit=limit,
        output=str(output_dir),
        headless=headless,
        browser_channel=browser_channel,
        resume=resume,
        list_only=list_only,
        delay=delay,
        phase=phase,
    )

    if phase == 1:
        # 阶段1：测试前2条
        asyncio.run(run_phase1(headless=headless))
        return

    # 完整采集
    records: list[ScrapedRecord] = asyncio.run(
        run_scraper(
            pages=pages,
            limit=limit,
            output_dir=output_dir,
            headless=headless,
            browser_channel=browser_channel,
            resume=resume,
            list_only=list_only,
            delay=delay,
        )
    )

    if not records:
        log.error("no_records_collected")
        sys.exit(1)

    # 转换为输出记录
    new_output_records = [OutputRecord.from_scraped_record(r) for r in records]

    # 断点续传：与已有输出合并（resume 模式）
    if resume:
        output_records = _merge_with_existing(new_output_records, output_dir)
        log.info(
            "merged_with_existing",
            new=len(new_output_records),
            total=len(output_records),
        )
    else:
        output_records = new_output_records

    # 生成输出文件
    write_all_outputs(output_records, output_dir)

    # 汇总
    log.info(
        "scraping_completed",
        total=len(output_records),
        new=len(new_output_records),
        success=sum(1 for r in output_records if r.核验状态 == "已核验"),
        pending=sum(1 for r in output_records if r.核验状态 == "待核验"),
        output_dir=str(output_dir),
    )

    # 打印摘要
    click.echo(f"\n{'=' * 50}")
    click.echo("采集完成")
    click.echo(f"总记录数: {len(output_records)}")
    click.echo(f"本次新增: {len(new_output_records)}")
    click.echo(f"已核验: {sum(1 for r in output_records if r.核验状态 == '已核验')}")
    click.echo(f"待核验: {sum(1 for r in output_records if r.核验状态 == '待核验')}")
    click.echo(f"输出目录: {output_dir.resolve()}")
    click.echo(f"  - JSON: {output_dir / 'zhongshan_land_transactions.json'}")
    click.echo(f"  - CSV: {output_dir / 'zhongshan_land_transactions.csv'}")
    click.echo(f"  - Excel: {output_dir / 'zhongshan_land_transactions.xlsx'}")
    click.echo(f"{'=' * 50}")


if __name__ == "__main__":
    main()
