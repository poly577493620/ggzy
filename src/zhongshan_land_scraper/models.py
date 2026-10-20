"""Pydantic models for land transaction data."""

from datetime import datetime

from pydantic import BaseModel


class DetailFields(BaseModel):
    """详情页解析出的字段."""

    project_name: str = ""  # 项目名称
    project_code: str = ""  # 项目编号
    transaction_date: str = ""  # 成交时间
    winner: str = ""  # 竞得人
    land_area: str = ""  # 用地面积
    transaction_price: str = ""  # 成交总价
    land_location: str = ""  # 宗地位置
    land_usage: str = ""  # 土地用途
    transaction_agency: str = ""  # 交易机构
    notice_nature: str = ""  # 公告性质
    attachments: list[str] = []  # 附件链接
    missing_fields: list[str] = []  # 缺失字段
    field_sources: dict[str, str] = {}  # 字段来源记录


class ScrapedRecord(BaseModel):
    """完整采集记录（列表 + 详情）."""

    doc_id: str
    notice_id: str
    notice_title: str
    publish_date: str
    project_code: str
    project_owner: str
    project_type_name: str
    site_name: str
    trading_process: str
    dataset_name: str
    detail_url: str
    detail_fields: DetailFields
    html_path: str = ""  # 保存的HTML路径
    txt_path: str = ""  # 保存的TXT路径
    captured_at: datetime


class OutputRecord(BaseModel):
    """最终输出记录（中文字段，用于 JSON/CSV/Excel）."""

    成交时间: str = ""
    竞得人: str = ""
    用地面积: str = ""
    成交总价: str = ""
    宗地位置: str = ""
    土地用途: str = ""
    项目名称: str = ""
    项目编号: str = ""
    公告标题: str = ""
    公告发布时间: str = ""
    公告详情链接: str = ""
    采集时间: str = ""
    核验状态: str = ""
    备注: str = ""

    @classmethod
    def from_scraped_record(cls, record: ScrapedRecord) -> OutputRecord:
        """从 ScrapedRecord 转换为 OutputRecord.

        Args:
            record: 采集记录

        Returns:
            输出记录
        """
        df = record.detail_fields
        captured_str = record.captured_at.strftime("%Y-%m-%d %H:%M:%S")

        # 确定核验状态
        verification_status = "已核验"
        notes = ""
        if df.missing_fields:
            verification_status = "待核验"
            notes = f"缺失字段: {', '.join(df.missing_fields)}"

        # 检查字段来源
        if "title_extract" in df.field_sources.get("project_code", ""):
            if notes:
                notes += "; 项目编号来自标题提取"
            else:
                notes = "项目编号来自标题提取"

        # 格式化公告发布时间
        publish_formatted = record.publish_date
        if len(publish_formatted) == 14:
            publish_formatted = (
                f"{publish_formatted[:4]}-{publish_formatted[4:6]}-{publish_formatted[6:8]} "
                f"{publish_formatted[8:10]}:{publish_formatted[10:12]}:{publish_formatted[12:14]}"
            )

        return cls(
            成交时间=df.transaction_date,
            竞得人=df.winner,
            用地面积=df.land_area,
            成交总价=df.transaction_price,
            宗地位置=df.land_location,
            土地用途=df.land_usage,
            项目名称=df.project_name,
            项目编号=df.project_code,
            公告标题=record.notice_title,
            公告发布时间=publish_formatted,
            公告详情链接=record.detail_url,
            采集时间=captured_str,
            核验状态=verification_status,
            备注=notes,
        )
