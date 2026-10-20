"""Unit tests for core data processing functions.

覆盖:
- 日期转换格式化
- 项目编号正则提取
- 字段别名匹配
- 缺失值"待核验"标记
- OutputRecord 转换
- 去重逻辑
- Excel 导出基础
"""

import re
from datetime import datetime
from pathlib import Path

import pytest
from openpyxl import load_workbook

from zhongshan_land_scraper.models import DetailFields, OutputRecord, ScrapedRecord
from zhongshan_land_scraper.output_writer import write_excel
from zhongshan_land_scraper.parser import FIELD_ALIASES, _match_field_name
from zhongshan_land_scraper.resume import ResumeManager


class TestDateFormat:
    """测试日期格式转换."""

    def test_publish_date_14_digit_formatting(self) -> None:
        """14位 yyyymmddHHMMSS -> yyyy-mm-dd HH:MM:SS."""
        record = ScrapedRecord(
            doc_id="test",
            notice_id="n1",
            notice_title="test",
            publish_date="20260804094835",
            project_code="c1",
            project_owner="o1",
            project_type_name="t1",
            site_name="s1",
            trading_process="p1",
            dataset_name="d1",
            detail_url="u1",
            detail_fields=DetailFields(),
            captured_at=datetime(2026, 8, 5, 10, 0, 0),
        )
        out = OutputRecord.from_scraped_record(record)
        assert out.公告发布时间 == "2026-08-04 09:48:35"

    def test_publish_date_short_formatting(self) -> None:
        """非14位日期保持原样."""
        record = ScrapedRecord(
            doc_id="test",
            notice_id="n1",
            notice_title="test",
            publish_date="2026-08-04",
            project_code="c1",
            project_owner="o1",
            project_type_name="t1",
            site_name="s1",
            trading_process="p1",
            dataset_name="d1",
            detail_url="u1",
            detail_fields=DetailFields(),
            captured_at=datetime.now(),
        )
        out = OutputRecord.from_scraped_record(record)
        assert out.公告发布时间 == "2026-08-04"


class TestProjectCodeExtraction:
    """测试项目编号从标题提取."""

    def test_extract_from_title_standard(self) -> None:
        """标准格式 G06-2026-0016."""
        title = "[中山公用][商业用地、标准地]G06-2026-0016中山市..."
        match = re.search(r"[A-Z]\d{2}-\d{4}-\d{4}", title)
        assert match is not None
        assert match.group(0) == "G06-2026-0016"

    def test_extract_from_title_no_code(self) -> None:
        """无编号时返回 None."""
        title = "中山市某地块使用权出让"
        match = re.search(r"[A-Z]\d{2}-\d{4}-\d{4}", title)
        assert match is None


class TestFieldAliases:
    """测试字段别名匹配."""

    @pytest.mark.parametrize(
        ("label", "expected"),
        [
            ("项目名称", "project_name"),
            ("宗地编号", "project_code"),
            ("成交时间", "transaction_date"),
            ("竞得人", "winner"),
            ("用地面积", "land_area"),
            ("成交总价", "transaction_price"),
            ("宗地位置", "land_location"),
            ("土地用途", "land_usage"),
            ("交易机构名称", "transaction_agency"),
            ("公告性质", "notice_nature"),
        ],
    )
    def test_exact_match(self, label: str, expected: str) -> None:
        """精确匹配常见标签."""
        assert _match_field_name(label) == expected

    @pytest.mark.parametrize(
        ("label", "expected"),
        [
            ("  项目名称  ", "project_name"),
            ("成交时间：", "transaction_date"),
            ("竞得人：", "winner"),
        ],
    )
    def test_with_whitespace_and_colon(self, label: str, expected: str) -> None:
        """带空白和冒号的标签."""
        assert _match_field_name(label) == expected

    def test_no_match(self) -> None:
        """无匹配时返回 None."""
        assert _match_field_name("不存在的字段") is None

    def test_alias_coverage(self) -> None:
        """所有核心字段都有别名."""
        required = [
            "project_name", "project_code", "transaction_date", "winner",
            "land_area", "transaction_price", "land_location", "land_usage",
        ]
        for field in required:
            assert field in FIELD_ALIASES
            assert len(FIELD_ALIASES[field]) > 0


class TestMissingFieldsPending:
    """测试缺失值标记为"待核验"."""

    def test_all_fields_present_verified(self) -> None:
        """所有必填字段存在 -> 已核验."""
        df = DetailFields(
            transaction_date="2026-08-04",
            winner="某公司",
            land_area="1000平方米",
            transaction_price="100万元",
            land_location="某镇",
            land_usage="工业用地",
        )
        record = ScrapedRecord(
            doc_id="test",
            notice_id="n1",
            notice_title="test",
            publish_date="20260804094835",
            project_code="c1",
            project_owner="o1",
            project_type_name="t1",
            site_name="s1",
            trading_process="p1",
            dataset_name="d1",
            detail_url="u1",
            detail_fields=df,
            captured_at=datetime.now(),
        )
        out = OutputRecord.from_scraped_record(record)
        assert out.核验状态 == "已核验"
        assert out.备注 == ""

    def test_missing_fields_pending(self) -> None:
        """缺失必填字段 -> 待核验 + 备注说明."""
        df = DetailFields(
            missing_fields=["transaction_date", "winner", "land_area"],
        )
        record = ScrapedRecord(
            doc_id="test",
            notice_id="n1",
            notice_title="test",
            publish_date="20260804094835",
            project_code="c1",
            project_owner="o1",
            project_type_name="t1",
            site_name="s1",
            trading_process="p1",
            dataset_name="d1",
            detail_url="u1",
            detail_fields=df,
            captured_at=datetime.now(),
        )
        out = OutputRecord.from_scraped_record(record)
        assert out.核验状态 == "待核验"
        assert "transaction_date" in out.备注
        assert "winner" in out.备注
        assert "land_area" in out.备注

    def test_title_extract_note(self) -> None:
        """项目编号来自标题提取 -> 备注说明."""
        df = DetailFields(
            field_sources={"project_code": "title_extract"},
        )
        record = ScrapedRecord(
            doc_id="test",
            notice_id="n1",
            notice_title="test",
            publish_date="20260804094835",
            project_code="c1",
            project_owner="o1",
            project_type_name="t1",
            site_name="s1",
            trading_process="p1",
            dataset_name="d1",
            detail_url="u1",
            detail_fields=df,
            captured_at=datetime.now(),
        )
        out = OutputRecord.from_scraped_record(record)
        assert "项目编号来自标题提取" in out.备注


class TestDedupAndResume:
    """测试去重和断点续传."""

    def test_resume_manager_seen(self, tmp_path: Path) -> None:
        """已采集的 docId 被标记为 seen."""
        mgr = ResumeManager(tmp_path)
        mgr.mark_success("doc-001")
        assert mgr.is_seen("doc-001") is True
        assert mgr.is_seen("doc-002") is False

    def test_resume_manager_skip_count(self, tmp_path: Path) -> None:
        """跳过计数正确."""
        mgr = ResumeManager(tmp_path)
        mgr.mark_success("doc-001")
        mgr.mark_skip("doc-001")
        mgr.mark_skip("doc-001")
        summary = mgr.summary()
        assert summary["success"] == 1
        assert summary["skipped"] == 2
        assert summary["seen_ids"] == 1

    def test_resume_persistence(self, tmp_path: Path) -> None:
        """断点数据持久化到文件."""
        mgr1 = ResumeManager(tmp_path)
        mgr1.mark_success("doc-001")
        mgr1.mark_success("doc-002")

        mgr2 = ResumeManager(tmp_path)
        assert mgr2.is_seen("doc-001") is True
        assert mgr2.is_seen("doc-002") is True

    def test_reset_clears_all(self, tmp_path: Path) -> None:
        """重置清除所有记录."""
        mgr = ResumeManager(tmp_path)
        mgr.mark_success("doc-001")
        mgr.reset()
        assert mgr.is_seen("doc-001") is False
        assert mgr.success_count == 0


class TestExcelExport:
    """测试 Excel 导出功能."""

    def test_excel_creates_file(self, tmp_path: Path) -> None:
        """Excel 文件被正确创建."""
        records = [
            OutputRecord(
                成交时间="2026-08-04",
                竞得人="甲公司",
                用地面积="1000平方米",
                成交总价="500万元",
                宗地位置="某镇",
                土地用途="工业用地",
                项目名称="地块A",
                项目编号="G01-2026-0001",
                公告标题="出让公告",
                公告发布时间="2026-08-01",
                公告详情链接="https://example.com/1",
                采集时间="2026-08-05 10:00:00",
                核验状态="已核验",
                备注="",
            ),
            OutputRecord(
                成交时间="",
                竞得人="",
                用地面积="",
                成交总价="",
                宗地位置="",
                土地用途="",
                项目名称="地块B",
                项目编号="G02-2026-0002",
                公告标题="公告B",
                公告发布时间="2026-08-02",
                公告详情链接="https://example.com/2",
                采集时间="2026-08-05 10:00:01",
                核验状态="待核验",
                备注="缺失字段: transaction_date, winner",
            ),
        ]
        out_path = tmp_path / "test.xlsx"
        write_excel(records, out_path)
        assert out_path.exists()

        wb = load_workbook(out_path)
        ws = wb.active
        assert ws is not None
        # 表头 + 2 行数据 = 3 行
        assert ws.max_row == 3
        # 14 列
        assert ws.max_column == 14
        # 验证表头
        headers = [ws.cell(row=1, column=c).value for c in range(1, 15)]
        assert "成交时间" in headers
        assert "竞得人" in headers
        assert "核验状态" in headers
        # 验证数据行
        assert ws.cell(row=2, column=headers.index("核验状态") + 1).value == "已核验"
        assert ws.cell(row=3, column=headers.index("核验状态") + 1).value == "待核验"
        # 超链接列
        link_col = headers.index("公告详情链接") + 1
        cell = ws.cell(row=2, column=link_col)
        assert cell.hyperlink is not None

    def test_excel_empty_records(self, tmp_path: Path) -> None:
        """空记录列表不创建文件（writer 内部处理）."""
        # write_excel 直接调用会创建空工作簿
        out_path = tmp_path / "empty.xlsx"
        write_excel([], out_path)
        assert out_path.exists()
        wb = load_workbook(out_path)
        ws = wb.active
        assert ws is not None
        assert ws.max_row == 1  # 只有表头
