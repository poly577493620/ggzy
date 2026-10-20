"""Resume / checkpoint management for scraped records."""

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger()

SEEN_IDS_FILE = "seen_ids.json"
ERROR_RECORDS_FILE = "error_records.json"


class ResumeManager:
    """断点续传管理器.

    维护已采集的 docId 集合和错误记录列表，支持跳过已采集项。
    """

    def __init__(self, output_dir: Path) -> None:
        """初始化.

        Args:
            output_dir: 输出根目录
        """
        self.output_dir = output_dir
        self.seen_ids_path = output_dir / SEEN_IDS_FILE
        self.error_records_path = output_dir / ERROR_RECORDS_FILE

        self.seen_ids: set[str] = set()
        self.error_records: list[dict[str, Any]] = []
        self.success_count = 0
        self.fail_count = 0
        self.skip_count = 0

        self._load()

    def _load(self) -> None:
        """加载已有的 seen_ids 和 error_records."""
        if self.seen_ids_path.exists():
            try:
                with open(self.seen_ids_path, encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict) and "doc_ids" in data:
                    self.seen_ids = set(data["doc_ids"])
                    self.success_count = data.get("success_count", len(self.seen_ids))
                    log.info("resume_loaded", seen=len(self.seen_ids))
                elif isinstance(data, list):
                    # 兼容旧格式
                    self.seen_ids = set(data)
                    self.success_count = len(self.seen_ids)
                    log.info("resume_loaded_legacy", seen=len(self.seen_ids))
            except Exception as e:
                log.warning("resume_load_failed", error=str(e))

        if self.error_records_path.exists():
            try:
                with open(self.error_records_path, encoding="utf-8") as f:
                    self.error_records = json.load(f)
                self.fail_count = len(self.error_records)
            except Exception as e:
                log.warning("error_records_load_failed", error=str(e))

    def is_seen(self, doc_id: str) -> bool:
        """检查 docId 是否已经采集过.

        Args:
            doc_id: 文档 ID

        Returns:
            True 如果已采集
        """
        return doc_id in self.seen_ids

    def mark_success(self, doc_id: str) -> None:
        """标记成功采集.

        Args:
            doc_id: 文档 ID
        """
        if doc_id not in self.seen_ids:
            self.seen_ids.add(doc_id)
            self.success_count += 1
            self._save()

    def mark_skip(self, doc_id: str) -> None:
        """标记跳过（重复）.

        Args:
            doc_id: 文档 ID
        """
        self.skip_count += 1

    def mark_error(self, doc_id: str, notice_id: str, notice_title: str, error: str, page: int) -> None:
        """标记错误.

        Args:
            doc_id: 文档 ID
            notice_id: 公告 ID
            notice_title: 公告标题
            error: 错误描述
            page: 页码
        """
        record = {
            "doc_id": doc_id,
            "notice_id": notice_id,
            "notice_title": notice_title,
            "error": error,
            "page": page,
            "timestamp": datetime.now().isoformat(),
        }
        self.error_records.append(record)
        self.fail_count += 1
        self._save_errors()

    def _save(self) -> None:
        """保存 seen_ids."""
        data = {
            "doc_ids": sorted(self.seen_ids),
            "success_count": self.success_count,
            "last_updated": datetime.now().isoformat(),
        }
        self.output_dir.mkdir(parents=True, exist_ok=True)
        with open(self.seen_ids_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _save_errors(self) -> None:
        """保存 error_records."""
        self.output_dir.mkdir(parents=True, exist_ok=True)
        with open(self.error_records_path, "w", encoding="utf-8") as f:
            json.dump(self.error_records, f, ensure_ascii=False, indent=2)

    def reset(self) -> None:
        """重置所有记录（用于 --no-resume）."""
        self.seen_ids.clear()
        self.error_records.clear()
        self.success_count = 0
        self.fail_count = 0
        self.skip_count = 0
        if self.seen_ids_path.exists():
            self.seen_ids_path.unlink()
        if self.error_records_path.exists():
            self.error_records_path.unlink()
        log.info("resume_reset")

    def summary(self) -> dict[str, int]:
        """返回采集统计.

        Returns:
            统计字典
        """
        return {
            "success": self.success_count,
            "failed": self.fail_count,
            "skipped": self.skip_count,
            "total_processed": self.success_count + self.fail_count + self.skip_count,
            "seen_ids": len(self.seen_ids),
        }
