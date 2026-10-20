"""Detail page parser for extracting land transaction fields from DOM."""

import re
from typing import Any

from bs4 import BeautifulSoup

# 字段别名映射
FIELD_ALIASES: dict[str, list[str]] = {
    "project_name": ["项目名称", "宗地名称"],
    "project_code": ["项目编号", "宗地编号", "地块编号", "公告编号", "宗地号"],
    "transaction_date": ["成交时间", "成交日期", "交易日期"],
    "winner": ["竞得人", "受让人", "成交单位", "竞买人", "竞得单位"],
    "land_area": ["用地面积", "宗地面积", "土地面积", "出让面积", "面积"],
    "transaction_price": ["成交总价", "成交价", "成交金额", "成交地价", "总价"],
    "land_location": ["宗地位置", "地块位置", "土地位置", "坐落", "位置"],
    "land_usage": ["土地用途", "规划用途", "用地性质", "用途"],
    "transaction_agency": ["交易机构名称", "交易机构", "交易中心"],
    "notice_nature": ["公告性质", "公告类型", "性质"],
}


def _normalize_text(text: str) -> str:
    """清理文本中的空白字符."""
    return " ".join(text.split())


def _match_field_name(label: str) -> str | None:
    """根据标签文本匹配字段名.

    Args:
        label: 标签文本

    Returns:
        匹配的字段名或 None
    """
    label = _normalize_text(label)
    for field_name, aliases in FIELD_ALIASES.items():
        for alias in aliases:
            if alias in label:
                return field_name
    return None


def parse_detail_html(html: str) -> dict[str, Any]:
    """从详情页 HTML 中解析字段.

    解析策略：
    1. 表格中的 th-td 结构
    2. dl/dt/dd 结构
    3. 带中文标签的 div/p 结构
    4. 公告正文中的键值文本

    Args:
        html: 页面 HTML 字符串

    Returns:
        解析出的字段字典
    """
    soup = BeautifulSoup(html, "html.parser")
    result: dict[str, str] = {}
    field_sources: dict[str, str] = {}

    # 策略1: 解析表格中的 th-td 结构
    for table in soup.find_all("table"):
        for row in table.find_all("tr"):
            # 查找 th + td 结构
            ths = row.find_all("th")
            tds = row.find_all("td")
            if ths and tds:
                for th, td in zip(ths, tds, strict=False):
                    label = _normalize_text(th.get_text())
                    value = _normalize_text(td.get_text())
                    field = _match_field_name(label)
                    if field and value and value not in ("/", "—", "-"):
                        result[field] = value
                        field_sources[field] = f"table_th_td:{label}"

            # 查找 td + td 结构（交替标签和值）
            if not ths and len(tds) >= 2:
                for i in range(0, len(tds) - 1, 2):
                    label = _normalize_text(tds[i].get_text())
                    value = _normalize_text(tds[i + 1].get_text()) if i + 1 < len(tds) else ""
                    field = _match_field_name(label)
                    if field and value and value not in ("/", "—", "-"):
                        result[field] = value
                        field_sources[field] = f"table_td_td:{label}"

    # 策略2: 解析 dl/dt/dd 结构
    for dl in soup.find_all("dl"):
        dts = dl.find_all("dt")
        dds = dl.find_all("dd")
        for dt, dd in zip(dts, dds, strict=False):
            label = _normalize_text(dt.get_text())
            value = _normalize_text(dd.get_text())
            field = _match_field_name(label)
            if field and value and value not in ("/", "—", "-"):
                result[field] = value
                field_sources[field] = f"dl_dt_dd:{label}"

    # 策略3: 查找带 class 的 div/span 标签对
    # 查找公告信息区域
    info_sections = soup.find_all(
        ["div", "section"],
        class_=lambda c: c and any(k in c.lower() for k in ["info", "detail", "content", "notice"]),
    )
    for section in info_sections:
        # 查找标签-值对
        for elem in section.find_all(["div", "p", "span", "li"]):
            text = _normalize_text(elem.get_text())
            # 尝试匹配 "标签：值" 或 "标签:值" 模式
            for field_name, aliases in FIELD_ALIASES.items():
                if field_name in result:
                    continue
                for alias in aliases:
                    # 匹配 "标签：值" 或 "标签:值"
                    patterns = [
                        rf"{re.escape(alias)}[\s]*[:：][\s]*(.+)",
                    ]
                    for pattern in patterns:
                        match = re.search(pattern, text)
                        if match:
                            value = _normalize_text(match.group(1))
                            if value and value not in ("/", "—", "-"):
                                result[field_name] = value
                                field_sources[field_name] = f"text_pattern:{alias}"
                                break

    # 策略4: 从页面标题中提取项目编号
    title_elem = soup.find("title")
    if title_elem:
        title_text = _normalize_text(title_elem.get_text())
        # 尝试从标题中提取形如 G06-2026-0016 的编号
        code_match = re.search(r"[A-Z]\d{2}-\d{4}-\d{4}", title_text)
        if code_match and "project_code" not in result:
            result["project_code"] = code_match.group(0)
            field_sources["project_code"] = "title_extract"

    # 策略5: 查找附件链接
    attachments: list[str] = []
    for link in soup.find_all("a", href=True):
        href = str(link.get("href", ""))
        text = link.get_text()
        if any(k in text.lower() for k in ["pdf", "附件", "下载", "文件"]) or href.endswith(".pdf"):
            attachments.append(href)

    return {
        "fields": result,
        "field_sources": field_sources,
        "attachments": attachments,
    }


def extract_fields_from_api(api_data: dict[str, Any]) -> dict[str, Any]:
    """从详情 API 响应中提取字段.

    Args:
        api_data: 详情 API JSON 数据

    Returns:
        解析出的字段字典
    """
    result: dict[str, str] = {}
    field_sources: dict[str, str] = {}

    if not isinstance(api_data, dict):
        return {"fields": result, "field_sources": field_sources, "attachments": []}

    data = api_data.get("data", {})
    if not isinstance(data, dict):
        return {"fields": result, "field_sources": field_sources, "attachments": []}

    # 解析 tradingNoticeColumnModelList
    columns = data.get("tradingNoticeColumnModelList", [])
    for column in columns:
        if not isinstance(column, dict):
            continue

        # 解析 keyTable 类型的 multiKeyValueTableList
        if column.get("viewStyle") == "keyTable":
            kv_list = column.get("multiKeyValueTableList", [])
            for table in kv_list:
                if not isinstance(table, list):
                    continue
                for item in table:
                    if not isinstance(item, dict):
                        continue
                    key = item.get("key", "")
                    value = item.get("value", "")
                    field = _match_field_name(key)
                    if field and value and value not in ("/", "—", "-"):
                        result[field] = value
                        field_sources[field] = f"api_keytable:{key}"

        # 解析 richText 类型的内容（HTML 表格）
        if column.get("viewStyle") == "richText":
            richtext = column.get("richtext", "")
            if richtext:
                # 从 HTML 中解析
                parsed = parse_detail_html(richtext)
                for field, value in parsed.get("fields", {}).items():
                    if field not in result:
                        result[field] = value
                        field_sources[field] = f"api_richtext:{parsed['field_sources'].get(field, '')}"

    # 解析附件
    attachments: list[str] = []
    for column in columns:
        files = column.get("noticeFileBOList", []) if isinstance(column, dict) else []
        if isinstance(files, list):
            for file_item in files:
                if isinstance(file_item, dict):
                    file_url = file_item.get("fileUrl", "")
                    if file_url:
                        attachments.append(file_url)

    return {
        "fields": result,
        "field_sources": field_sources,
        "attachments": attachments,
    }
