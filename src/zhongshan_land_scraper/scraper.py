"""Playwright scraper for Zhongshan land transaction results — full multi-page."""

import asyncio
import json
import re
import sys
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Any

import structlog
from bs4 import BeautifulSoup
from playwright.async_api import Page, async_playwright

from zhongshan_land_scraper.models import DetailFields, ScrapedRecord
from zhongshan_land_scraper.resume import ResumeManager

log = structlog.get_logger()

BASE_URL = "https://ygp.gdzwfw.gov.cn/#/442000/jygg"
LIST_API_URL = "https://ygp.gdzwfw.gov.cn/ggzy-portal/search/v2/items"
DETAIL_API_PATTERN = "trading-notice/new/detail"
MAX_RESTARTS = 1
GOTO_TIMEOUT = 60_000
LIST_API_TIMEOUT = 30_000
DETAIL_API_TIMEOUT = 20_000


async def _sleep_between_actions(delay: float) -> None:
    """操作间隔，避免触发反爬.

    Args:
        delay: 间隔秒数
    """
    await asyncio.sleep(delay)


def _ensure_dirs(output_dir: Path) -> Path:
    """确保输出目录结构存在.

    Args:
        output_dir: 输出根目录

    Returns:
        raw 子目录路径
    """
    raw_dir = output_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    (raw_dir / "details").mkdir(exist_ok=True)
    (raw_dir / "screenshots").mkdir(exist_ok=True)
    return raw_dir


def _is_api_error_90004(data: dict[str, Any]) -> bool:
    """检查 API 返回是否 90004 登录凭证失效."""
    code = data.get("code") or data.get("errcode")
    msg = str(data.get("msg", "")) + str(data.get("errmsg", ""))
    return code == 90004 or "登录凭证失效" in msg


async def _goto_page(page: Page, url: str) -> None:
    """带超时的页面导航."""
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=GOTO_TIMEOUT)
        await asyncio.sleep(2)
    except Exception as e:
        log.warning("goto_timeout", url=url, error=str(e))


async def _click_filter(page: Page, text: str, delay: float) -> None:
    """点击单个筛选条件.

    Args:
        page: Playwright Page
        text: 筛选条件文本
        delay: 操作间隔秒数
    """
    try:
        el = page.locator(f"text={text}").first
        if await el.count() > 0 and await el.is_visible(timeout=5000):
            await el.click()
            log.info("filter_clicked", text=text)
            await _sleep_between_actions(delay)
    except Exception as e:
        log.warning("filter_click_failed", text=text, error=str(e))


async def _apply_first_filter(page: Page, delay: float) -> None:
    """点击第一个筛选条件"土地矿业"，不监听接口响应.

    Args:
        page: Playwright Page
        delay: 操作间隔秒数
    """
    log.info("applying_first_filter")
    await asyncio.sleep(2)
    await _click_filter(page, "土地矿业", delay)
    await asyncio.sleep(2)


async def _apply_second_filter(page: Page, delay: float) -> None:
    """点击第二个筛选条件"土地使用权交易结果公告".

    此函数应在响应监听器注册后调用，以捕获正确筛选后的响应。

    Args:
        page: Playwright Page
        delay: 操作间隔秒数
    """
    log.info("applying_second_filter")
    await _click_filter(page, "土地使用权交易结果公告", delay)
    await asyncio.sleep(3)
    await _sleep_between_actions(delay)


async def _capture_list_api_with_listener(
    page: Page,
    delay: float,
) -> dict[str, Any] | None:
    """使用响应监听器捕获筛选后的最终列表 API 数据.

    Vue 前端点击筛选条件时可能触发多次 API 请求，
    取最后一个响应作为正确结果。

    Args:
        page: Playwright Page
        delay: 操作间隔秒数

    Returns:
        列表 API JSON 数据，或 None
    """
    responses: list[Any] = []

    def on_response(response: Any) -> None:
        if LIST_API_URL in response.url and response.request.method == "POST":
            responses.append(response)

    page.on("response", on_response)

    try:
        await _apply_first_filter(page, delay)
        await _apply_second_filter(page, delay)
        await asyncio.sleep(5)

        if not responses:
            log.warning("no_list_responses_collected")
            return None

        last_response = responses[-1]
        data: dict[str, Any] = await last_response.json()
        log.info(
            "list_api_captured_via_listener",
            total_responses=len(responses),
            errcode=data.get("errcode"),
        )
        return data
    except Exception as e:
        log.warning("list_api_capture_error", error=str(e))
        return None
    finally:
        page.remove_listener("response", on_response)


def _extract_items(list_data: dict[str, Any]) -> list[dict[str, Any]]:
    """从列表 API 响应中提取 pageData 记录列表."""
    if not isinstance(list_data, dict):
        return []
    data = list_data.get("data", {})
    if isinstance(data, dict):
        for key in ("pageData", "list", "items", "records"):
            if key in data:
                result = data[key]
                if isinstance(result, list):
                    return result
    return []


def _validate_list_items(items: list[dict[str, Any]], page: int) -> tuple[bool, str]:
    """校验列表页数据是否符合筛选条件.

    检查项:
    - 每条记录的 tradingProcess 必须为 "3742"
    - 每条记录的 noticeTitle 必须包含"结果公告"

    Args:
        items: 列表页记录
        page: 页码（用于日志）

    Returns:
        (是否通过, 错误信息)
    """
    bad_items: list[tuple[str, str, str]] = []  # (doc_id, title, reason)

    for item in items:
        doc_id = str(item.get("docId", ""))
        notice_title = str(item.get("noticeTitle", ""))
        trading_process = str(item.get("tradingProcess", ""))

        if trading_process != "3742":
            bad_items.append((doc_id, notice_title, f"tradingProcess={trading_process}"))

        if "结果公告" not in notice_title:
            bad_items.append((doc_id, notice_title, "title_missing_结果公告"))

    if bad_items:
        error_detail = "; ".join(
            f"[{doc_id[:20]}] {title[:40]} ({reason})"
            for doc_id, title, reason in bad_items[:5]
        )
        log.error("list_validation_failed", page=page, bad_count=len(bad_items), detail=error_detail)
        return False, f"Page {page}: {len(bad_items)} items failed validation"

    log.info("list_validation_passed", page=page, count=len(items))
    return True, ""


# ---- field parsing (unchanged from verified version) ----

FIELD_ALIASES: dict[str, list[str]] = {
    "project_name": ["项目名称", "宗地名称"],
    "project_code": ["项目编号", "宗地编号", "地块编号", "公告编号", "宗地号"],
    "transaction_date": ["成交时间", "成交日期", "交易日期"],
    "winner": ["竞得人", "受让人", "受让人名称", "成交单位", "竞买人", "竞得单位"],
    "land_area": ["用地面积", "宗地面积", "土地面积", "出让面积", "面积"],
    "transaction_price": ["成交总价", "成交价", "成交金额", "成交地价", "总价"],
    "land_location": ["宗地位置", "地块位置", "土地位置", "坐落", "位置"],
    "land_usage": ["土地用途", "规划用途", "用地性质", "用途"],
    "transaction_agency": ["交易机构名称", "交易机构", "交易中心"],
    "notice_nature": ["公告性质", "公告类型", "性质"],
}


def _match_field_name(label: str) -> str | None:
    """根据标签文本匹配字段名."""
    label = label.strip()
    for field_name, aliases in FIELD_ALIASES.items():
        for alias in aliases:
            if alias in label:
                return field_name
    return None


def _parse_richtext_html(richtext_html: str) -> dict[str, str]:
    """解析 richtext HTML 表格，提取字段."""
    result: dict[str, str] = {}
    soup = BeautifulSoup(richtext_html, "html.parser")

    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        for row in rows:
            cells = row.find_all(["th", "td"])
            if not cells:
                continue

            cell_texts = []
            for cell in cells:
                text = cell.get_text(strip=True)
                text = re.sub(r"[：:]\s*$", "", text).strip()
                cell_texts.append(text)

            # 模式1: 一行两列
            if len(cell_texts) == 2:
                label = cell_texts[0]
                value = cell_texts[1]
                field = _match_field_name(label)
                if field and value and value not in ("/", "—", "-", ""):
                    result[field] = value

            # 模式2: 一行四列
            elif len(cell_texts) == 4:
                for i in range(0, 4, 2):
                    label = cell_texts[i]
                    value = cell_texts[i + 1] if i + 1 < len(cell_texts) else ""
                    field = _match_field_name(label)
                    if field and value and value not in ("/", "—", "-", ""):
                        result[field] = value

    return result


def _extract_detail_from_api(detail_api_data: dict[str, Any]) -> dict[str, Any]:
    """从详情 API 响应中提取字段."""
    fields: dict[str, str] = {}
    richtext_html = ""
    column_found = False

    if not isinstance(detail_api_data, dict):
        return {"fields": fields, "richtext": richtext_html, "column_found": column_found}

    data = detail_api_data.get("data", {})
    if not isinstance(data, dict):
        return {"fields": fields, "richtext": richtext_html, "column_found": column_found}

    columns = data.get("tradingNoticeColumnModelList", [])
    if not isinstance(columns, list):
        return {"fields": fields, "richtext": richtext_html, "column_found": column_found}

    content_column = None
    for col in columns:
        if not isinstance(col, dict):
            continue
        name = col.get("name", "")
        if isinstance(name, str) and "公告内容" in name:
            content_column = col
            column_found = True
            break

    if content_column is None and len(columns) >= 2:
        content_column = columns[1]
        column_found = True
        log.warning("detail_column_fallback_to_index", index=1)

    if content_column and isinstance(content_column, dict):
        richtext_html = content_column.get("richtext", "") or ""
        if richtext_html:
            fields = _parse_richtext_html(richtext_html)

    return {"fields": fields, "richtext": richtext_html, "column_found": column_found}


def _extract_dom_fields(html: str) -> dict[str, str]:
    """从 DOM HTML 的"公告信息"表格中提取字段."""
    result: dict[str, str] = {}
    soup = BeautifulSoup(html, "html.parser")

    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        for row in rows:
            ths = row.find_all("th")
            tds = row.find_all("td")
            if ths and tds:
                for th, td in zip(ths, tds, strict=False):
                    label = th.get_text(strip=True)
                    value = td.get_text(strip=True)
                    field = _match_field_name(label)
                    if field and value and value not in ("/", "—", "-", ""):
                        result[field] = value
    return result


def _build_detail_fields(
    api_fields: dict[str, str],
    dom_fields: dict[str, str],
    item: dict[str, Any],
) -> DetailFields:
    """合并 API 字段和 DOM 字段，构建 DetailFields."""
    fields = DetailFields()
    field_sources: dict[str, str] = {}

    # API 字段优先
    for field_name, value in api_fields.items():
        if hasattr(fields, field_name) and value:
            setattr(fields, field_name, value)
            field_sources[field_name] = "richtext_api"

    # DOM 字段补充
    for field_name, value in dom_fields.items():
        if not hasattr(fields, field_name):
            continue
        existing = getattr(fields, field_name, "")
        if not existing or existing in ("/", "—", "-", ""):
            setattr(fields, field_name, value)
            field_sources[field_name] = "dom_table"
        elif existing != value:
            log.warning("field_mismatch", field=field_name, api=existing, dom=value)

    # 标题补充
    notice_title = str(item.get("noticeTitle", ""))
    if not fields.land_usage:
        brackets = re.findall(r"\[([^\]]+)\]", notice_title)
        if len(brackets) >= 2:
            fields.land_usage = brackets[1]
            field_sources["land_usage"] = "title_bracket"
        elif len(brackets) == 1:
            fields.land_usage = brackets[0]
            field_sources["land_usage"] = "title_bracket"

    if fields.land_usage in ("网上挂牌", "网上拍卖", "现场挂牌"):
        brackets = re.findall(r"\[([^\]]+)\]", notice_title)
        if len(brackets) >= 2:
            fields.land_usage = brackets[1]
            field_sources["land_usage"] = "title_bracket_corrected"
        else:
            fields.land_usage = ""

    if not fields.project_code:
        code_match = re.search(r"[A-Z]\d{2}-\d{4}-\d{4}", notice_title)
        if code_match:
            fields.project_code = code_match.group(0)
            field_sources["project_code"] = "title_extract"

    if not fields.project_name:
        fields.project_name = notice_title
        field_sources["project_name"] = "list_title"

    fields.field_sources = field_sources

    required = ["transaction_date", "winner", "land_area", "transaction_price", "land_location", "land_usage"]
    for field in required:
        value = getattr(fields, field, "")
        if not value or value in ("/", "—", "-", ""):
            fields.missing_fields.append(field)

    return fields


# ---- detail capture (direct navigation) ----


def _build_detail_url(item: dict[str, Any]) -> str:
    """从列表项数据构建详情页 URL.

    Args:
        item: 列表 API 返回的单条记录

    Returns:
        详情页完整 URL
    """
    notice_id = str(item.get("noticeId", ""))
    project_code = str(item.get("projectCode", ""))
    trading_process = str(item.get("tradingProcess", ""))
    site_code = str(item.get("siteCode", ""))
    publish_date = str(item.get("publishDate", ""))
    project_type = str(item.get("projectType", ""))

    source = urllib.parse.quote("中山市公共资源交易平台")
    title_details = urllib.parse.quote("土地矿业")

    return (
        f"https://ygp.gdzwfw.gov.cn/#/442000/new/jygg/v3/B?"
        f"noticeId={notice_id}"
        f"&projectCode={project_code}"
        f"&bizCode={trading_process}"
        f"&siteCode={site_code}"
        f"&publishDate={publish_date}"
        f"&source={source}"
        f"&titleDetails={title_details}"
        f"&classify={project_type}"
    )


async def _capture_detail_page(
    list_page: Page,
    item: dict[str, Any],
    output_dir: Path,
    index: int,
    delay: float,
) -> tuple[DetailFields, str, str, str, dict[str, Any] | None] | None:
    """在新标签页直接导航到详情页，监听 API 响应并解析字段.

    Args:
        list_page: 列表页 Playwright Page（保持不动）
        item: 列表项数据
        output_dir: 输出根目录
        index: 当前处理序号
        delay: 操作间隔秒数

    Returns:
        (DetailFields, html_path, txt_path, detail_url, detail_api_data) 或 None
    """
    doc_id = str(item.get("docId", ""))
    safe_doc_id = doc_id.replace("-", "_")
    context = list_page.context

    detail_url = _build_detail_url(item)
    log.info("detail_navigating", index=index, url=detail_url[:120])

    detail_api_data: dict[str, Any] | None = None

    # 创建新标签页并导航
    detail_page = await context.new_page()
    try:
        async with detail_page.expect_response(
            lambda r: DETAIL_API_PATTERN in r.url,
            timeout=15_000,
        ) as response_info:
            await detail_page.goto(detail_url, wait_until="domcontentloaded", timeout=30_000)
            log.info("detail_page_loaded", index=index)

        response = await response_info.value
        detail_api_data = await response.json()
        log.info("detail_api_captured", index=index, errcode=detail_api_data.get("errcode"))
    except Exception as e:
        log.warning("detail_api_capture_failed", index=index, error=str(e))
        # 继续：即使没有 API 数据，也可以从 DOM 提取

    # 等待页面稳定
    await asyncio.sleep(2)

    # 检查参数错误
    try:
        body_text = await detail_page.locator("body").inner_text(timeout=5000)
        if "参数错误" in body_text or "未找到" in body_text:
            log.error("detail_param_error", index=index)
            try:
                await detail_page.screenshot(
                    path=str(output_dir / "screenshots" / f"{safe_doc_id}_error.png"),
                    full_page=True,
                )
            except Exception:
                pass
            await detail_page.close()
            return None
    except Exception:
        pass

    # 保存 DOM
    actual_url = detail_page.url
    dom_html = await detail_page.content()
    dom_text = await detail_page.locator("body").inner_text(timeout=5000)

    html_file = output_dir / "details" / f"{safe_doc_id}.html"
    with open(html_file, "w", encoding="utf-8") as f:
        f.write(dom_html)
    html_path = str(html_file)

    txt_file = output_dir / "details" / f"{safe_doc_id}.txt"
    with open(txt_file, "w", encoding="utf-8") as f:
        f.write(dom_text)
    txt_path = str(txt_file)

    try:
        await detail_page.screenshot(
            path=str(output_dir / "screenshots" / f"{safe_doc_id}.png"),
            full_page=True,
        )
    except Exception:
        pass

    # 解析
    dom_fields = _extract_dom_fields(dom_html)
    api_fields: dict[str, str] = {}
    richtext_html = ""
    if detail_api_data:
        api_result = _extract_detail_from_api(detail_api_data)
        api_fields = api_result.get("fields", {})
        richtext_html = api_result.get("richtext", "")
        log.info(
            "api_fields_parsed",
            index=index,
            count=len(api_fields),
            richtext_len=len(richtext_html),
            fields=list(api_fields.keys()),
        )

    if detail_api_data:
        api_file = output_dir / "details" / f"{safe_doc_id}.json"
        with open(api_file, "w", encoding="utf-8") as f:
            json.dump(detail_api_data, f, ensure_ascii=False, indent=2)

    if richtext_html:
        richtext_file = output_dir / "details" / f"{safe_doc_id}_richtext.html"
        with open(richtext_file, "w", encoding="utf-8") as f:
            f.write(richtext_html)

    detail_fields = _build_detail_fields(api_fields, dom_fields, item)

    # 关闭详情页，返回列表页继续
    await detail_page.close()
    log.info("closed_detail_page", index=index)
    await _sleep_between_actions(delay)

    return detail_fields, html_path, txt_path, actual_url, detail_api_data


# ---- pagination helpers ----


async def _click_next_page(page: Page, delay: float) -> bool:
    """点击下一页.

    分页结构: <button icon-left>[hidden]</button> <input> / N <button>跳转</button> <button icon-right></button>

    Args:
        page: Playwright Page
        delay: 操作间隔

    Returns:
        是否成功点击
    """
    # 策略 1: 点击右箭头 (icon-right) — 当前页有下一页时可用
    try:
        next_btn = page.locator(".gd-pagination button:has(.icon-right)").first
        if await next_btn.count() > 0:
            is_hidden = await next_btn.evaluate("(el) => el.style.display === 'none'")
            if not is_hidden:
                await next_btn.click()
                log.info("next_page_clicked_via_arrow")
                await _sleep_between_actions(delay)
                return True
    except Exception as e:
        log.debug("arrow_click_failed", error=str(e))

    # 策略 2: 输入页码 + 点击跳转按钮
    try:
        pag_text = await page.locator(".gd-pagination").first.inner_text()
        m = re.search(r"/\s*(\d+)", pag_text)
        if not m:
            log.warning("next_page_no_total")
            return False
        total_pages = int(m.group(1))
        current_text = await page.locator(".gd-pagination input[type='number']").first.input_value()
        try:
            current = int(current_text or "1")
        except ValueError:
            current = 1
        nxt = current + 1
        if nxt > total_pages:
            log.info("next_page_reached_end", current=current, total=total_pages)
            return False
        await page.locator(".gd-pagination input[type='number']").first.fill(str(nxt))
        await _sleep_between_actions(0.3)
        # 点击"跳转"按钮 (不含 icon 的那个 gd-btn)
        jump_btns = page.locator(".gd-pagination-to button.gd-btn")
        for i in range(await jump_btns.count()):
            btn = jump_btns.nth(i)
            has_icon = await btn.locator("i.gd-icon").count() > 0
            if not has_icon:
                await btn.click()
                log.info("next_page_clicked_via_jump", page=nxt)
                await _sleep_between_actions(delay)
                return True
    except Exception as e:
        log.debug("jump_click_failed", error=str(e))

    log.warning("next_page_not_found")
    return False


# ---- main scraper ----


async def run_scraper(
    pages: int = 10,
    limit: int | None = None,
    output_dir: Path = Path("output"),
    headless: bool = False,
    browser_channel: str = "chrome",
    resume: bool = True,
    list_only: bool = False,
    delay: float = 1.2,
) -> list[ScrapedRecord]:
    """运行完整采集.

    Args:
        pages: 最大采集页数
        limit: 最大采集条数（None=不限）
        output_dir: 输出根目录
        headless: 是否无头模式
        browser_channel: 浏览器渠道
        resume: 是否断点续传
        list_only: 是否仅采集列表
        delay: 操作间隔秒数

    Returns:
        ScrapedRecord 列表
    """
    if delay < 1.0:
        log.warning("delay_too_short_adjusted", requested=delay, adjusted=1.0)
        delay = 1.0

    raw_dir = _ensure_dirs(output_dir)
    resume_mgr = ResumeManager(output_dir)

    if not resume:
        resume_mgr.reset()

    all_records: list[ScrapedRecord] = []
    processed_count = 0
    restart_count = 0

    while restart_count <= MAX_RESTARTS:
        log.info(
            "starting_browser",
            attempt=restart_count + 1,
            pages=pages,
            limit=limit,
            headless=headless,
            resume=resume,
            delay=delay,
        )

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=headless,
                channel=browser_channel,
            )
            context = await browser.new_context(
                viewport={"width": 1920, "height": 1080},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
                ),
            )
            list_page = await context.new_page()

            try:
                await _goto_page(list_page, BASE_URL)

                # 第一页：使用响应监听器捕获筛选后的最终列表 API
                list_data = await _capture_list_api_with_listener(list_page, delay)

                if list_data is None:
                    log.error("no_list_data_captured")
                    restart_count += 1
                    await browser.close()
                    continue

                if _is_api_error_90004(list_data):
                    log.error("list_90004_error", msg=list_data.get("errmsg"))
                    restart_count += 1
                    await browser.close()
                    continue

                # 解析总页数
                data = list_data.get("data", {}) if isinstance(list_data, dict) else {}
                total_pages = data.get("pageTotal", pages)
                total_records = data.get("total", "0")
                log.info("list_meta", total_pages=total_pages, total_records=total_records)

                # 分页循环
                target_pages = min(pages, total_pages)
                current_page = 1

                while current_page <= target_pages:
                    log.info("processing_page", page=current_page, of=target_pages)

                    # 如果是后续页，需要获取列表数据
                    if current_page > 1:
                        # 先注册 expect_response，再点击下一页
                        try:
                            async with list_page.expect_response(
                                lambda r: LIST_API_URL in r.url and r.request.method == "POST",
                                timeout=LIST_API_TIMEOUT,
                            ) as response_info:
                                clicked = await _click_next_page(list_page, delay)
                                if not clicked:
                                    log.warning("pagination_stopped", at_page=current_page)
                                    break
                            response = await response_info.value
                            list_data = await response.json()
                        except Exception as e:
                            log.warning("page_api_failed", page=current_page, error=str(e))
                            break

                    # 保存每页列表 JSON
                    list_file = raw_dir / f"list_page_{current_page:02d}.json"
                    with open(list_file, "w", encoding="utf-8") as f:
                        json.dump(list_data, f, ensure_ascii=False, indent=2)
                    log.info("list_page_saved", page=current_page, path=str(list_file))

                    # 提取记录
                    items_data = _extract_items(list_data)
                    log.info("items_on_page", page=current_page, count=len(items_data))

                    if not items_data:
                        log.warning("no_items_on_page", page=current_page)
                        break

                    # 校验列表数据：必须全部为交易结果公告
                    passed, error_msg = _validate_list_items(items_data, current_page)
                    if not passed:
                        log.error("page_validation_failed", page=current_page, error=error_msg)
                        resume_mgr.mark_error(
                            "page_validation",
                            "",
                            "列表页数据校验失败",
                            error_msg,
                            current_page,
                        )
                        break

                    # 处理每条记录
                    for item in items_data:
                        doc_id = str(item.get("docId", ""))
                        notice_id = str(item.get("noticeId", ""))
                        notice_title = str(item.get("noticeTitle", ""))
                        publish_date = str(item.get("publishDate", ""))
                        project_code = str(item.get("projectCode", ""))
                        project_owner = str(item.get("projectOwner", ""))
                        project_type_name = str(item.get("projectTypeName", ""))
                        site_name = str(item.get("siteName", ""))
                        trading_process = str(item.get("tradingProcess", ""))
                        dataset_name = str(item.get("datasetName", ""))

                        processed_count += 1

                        # 去重检查
                        if resume_mgr.is_seen(doc_id):
                            log.info("skipping_seen_record", doc_id=doc_id, page=current_page)
                            resume_mgr.mark_skip(doc_id)
                            continue

                        # 限制检查
                        if limit is not None and len(all_records) >= limit:
                            log.info("limit_reached", limit=limit)
                            return all_records

                        log.info(
                            "processing_record",
                            page=current_page,
                            doc_id=doc_id,
                            title=notice_title[:50],
                            progress=f"{len(all_records) + 1}/{limit or 'unlimited'}",
                        )

                        if list_only:
                            # 仅列表模式：不采集详情
                            record = ScrapedRecord(
                                doc_id=doc_id,
                                notice_id=notice_id,
                                notice_title=notice_title,
                                publish_date=publish_date,
                                project_code=project_code,
                                project_owner=project_owner,
                                project_type_name=project_type_name,
                                site_name=site_name,
                                trading_process=trading_process,
                                dataset_name=dataset_name,
                                detail_url="",
                                detail_fields=DetailFields(),
                                html_path="",
                                txt_path="",
                                captured_at=datetime.now(),
                            )
                            all_records.append(record)
                            resume_mgr.mark_success(doc_id)
                        else:
                            # 采集详情
                            result = await _capture_detail_page(list_page, item, raw_dir, len(all_records) + 1, delay)

                            if result is None:
                                log.error("detail_capture_failed", doc_id=doc_id, page=current_page)
                                resume_mgr.mark_error(
                                    doc_id,
                                    notice_id,
                                    notice_title,
                                    "detail_page_param_error_or_not_found",
                                    current_page,
                                )
                                continue

                            detail_fields, html_path, txt_path, detail_url, _ = result

                            record = ScrapedRecord(
                                doc_id=doc_id,
                                notice_id=notice_id,
                                notice_title=notice_title,
                                publish_date=publish_date,
                                project_code=project_code,
                                project_owner=project_owner,
                                project_type_name=project_type_name,
                                site_name=site_name,
                                trading_process=trading_process,
                                dataset_name=dataset_name,
                                detail_url=detail_url,
                                detail_fields=detail_fields,
                                html_path=html_path,
                                txt_path=txt_path,
                                captured_at=datetime.now(),
                            )
                            all_records.append(record)
                            resume_mgr.mark_success(doc_id)

                            # 立即保存中间记录
                            record_file = raw_dir / f"record_{len(all_records):03d}_{notice_id[:8]}.json"
                            with open(record_file, "w", encoding="utf-8") as f:
                                json.dump(record.model_dump(mode="json"), f, ensure_ascii=False, indent=2)

                        await _sleep_between_actions(delay)

                    current_page += 1

                # 分页完成或中断
                log.info(
                    "scraping_loop_completed",
                    processed=processed_count,
                    success=len(all_records),
                    failed=resume_mgr.fail_count,
                    skipped=resume_mgr.skip_count,
                )
                return all_records

            finally:
                await browser.close()

        restart_count += 1

    return all_records


# ---- CLI entry ----


async def run_phase1(headless: bool = False) -> None:
    """运行第一阶段（兼容旧版）."""
    log.info("phase1_starting")

    records = await run_scraper(
        pages=1,
        limit=2,
        output_dir=Path("output"),
        headless=headless,
        browser_channel="chrome",
        resume=False,
        list_only=False,
        delay=1.2,
    )

    if records:
        for i, r in enumerate(records, 1):
            df = r.detail_fields
            log.info(
                "record_summary",
                index=i,
                title=r.notice_title[:40],
                project_name=df.project_name[:30],
                project_code=df.project_code,
                transaction_date=df.transaction_date,
                winner=df.winner[:25] if df.winner else "",
                land_area=df.land_area,
                transaction_price=df.transaction_price,
                land_location=df.land_location[:25] if df.land_location else "",
                land_usage=df.land_usage,
                missing=df.missing_fields,
                html=r.html_path,
                txt=r.txt_path,
                url=r.detail_url[:60],
            )
        log.info("phase1_completed", record_count=len(records))
    else:
        log.error("phase1_no_records")
        sys.exit(1)
