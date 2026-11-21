"""湖仓读取领域服务：数据湖路径注册、Schema自动推导、分区发现与差异追踪。"""
from __future__ import annotations

import hashlib
import re
import time
import uuid
from typing import Any

from app.services.base import BaseModuleService
from app.services.cost_govern import CostGovernService
from app.store import store

# 分区目录约定：.../dt=2026-11-01/part-00001.parquet
_PARTITION_DATE_RE = re.compile(r"dt=(\d{4}-\d{2}-\d{2})")
# 旧扫描结果末尾的 "... (12 files)" 摘要不是文件路径
_LISTING_SUFFIX_RE = re.compile(r"\s*\.\.\.\s*\(\d+\s*files?\)\s*$", re.IGNORECASE)

_cost_svc = CostGovernService()


class DataLakeReadService(BaseModuleService):
    MODULE = "data_lake_read"
    ENTITY = "湖仓路径"
    OPERATION_TERM = "读取"
    LIST_FIELDS = ["路径编号", "存储位置", "文件格式", "分区模式", "Schema推导", "文件列表",
                   "分区明细", "命中文件", "最近检索", "最后扫描", "数据量"]
    REQUIRED_FIELDS = ["路径编号", "存储位置", "文件格式"]
    STATUS_ORDER = ["待扫描", "已注册", "Schema变更", "已停用"]
    ACTION_RULES = {"启动扫描": "已注册", "注册路径": "Schema变更", "停用路径": "已停用"}
    NEGATIVE_ACTIONS = ["停用路径"]

    def scan_and_register(self, path_id: int) -> dict[str, Any]:
        """扫描湖仓路径：写入 source_registry、semantic_model、query_audit、data_contract。"""
        path_entry = store.find(self.MODULE, path_id)
        if path_entry is None:
            return {"ok": False, "message": "湖仓路径不存在"}

        storage = path_entry.get("存储位置", "")
        fmt = path_entry.get("文件格式", "parquet")
        partition = path_entry.get("分区模式", "dt")

        # 模拟扫描：按分区目录生成确定性文件清单，日期 → 文件 → 体积/行数可复现
        # 同时保留一个无分区日期的历史文件，兼容既有目录登记
        scanned = [
            (dt, f"{storage}/dt={dt}/part-{i:05d}.{fmt}")
            for dt in _SCAN_PARTITIONS
            for i in range(1, _partition_file_count(storage, dt) + 1)
        ]
        legacy_path = f"{storage}/legacy/part-00001.{fmt}"
        files = [(dt, path, _file_size_mb(path)) for dt, path in scanned]
        files.append((None, legacy_path, _file_size_mb(legacy_path)))

        schema = _derive_schema(fmt)
        data_size = sum(size for _, _, size in files) * 1024 * 1024
        detail = _build_partition_detail(
            [(path, size) for dt, path, size in files if dt]
        )

        # 更新路径信息：保留完整文件路径，分区明细随路径一起登记
        path_entry["文件列表"] = ", ".join(path for _, path, _ in files)
        path_entry["分区明细"] = _dump_partition_detail(detail)
        path_entry["Schema推导"] = ", ".join(schema)
        path_entry["最后扫描"] = time.strftime("%Y-%m-%d %H:%M:%S")
        path_entry["数据量"] = f"{data_size / (1024*1024):.0f}MB"
        path_entry["status"] = "已注册"
        path_entry["pending"] = False

        # 注册为数据源
        source_id = _register_as_source(storage, fmt, schema)

        # 创建语义模型
        _create_semantic_model_for_lake(source_id, storage, schema)

        # 写入审计
        _audit_lake_scan(path_id, storage, len(files))

        return {
            "ok": True,
            "path_id": path_id,
            "storage": storage,
            "files_found": len(files),
            "schema_fields": len(schema),
            "data_size_mb": round(data_size / (1024 * 1024)),
            "source_id": source_id,
        }

    def detect_schema_drift(self, path_id: int) -> dict[str, Any]:
        """检测湖仓 Schema 漂移：对比 source_registry，通知 freshness_mon，更新 data_contract。"""
        path_entry = store.find(self.MODULE, path_id)
        if path_entry is None:
            return {"ok": False, "message": "湖仓路径不存在"}

        storage = path_entry.get("存储位置", "")
        recorded_schema = path_entry.get("Schema推导", "").split(",")
        recorded_schema = [s.strip() for s in recorded_schema if s.strip()]

        # 重新扫描
        new_schema = _derive_schema(path_entry.get("文件格式", "parquet"))
        new_fields = [f for f in new_schema if f not in recorded_schema]
        removed_fields = [f for f in recorded_schema if f not in new_schema]

        has_drift = len(new_fields) > 0 or len(removed_fields) > 0

        if has_drift:
            path_entry["Schema推导"] = ", ".join(new_schema)
            path_entry["status"] = "Schema变更"
            path_entry["pending"] = True
            path_entry["最后扫描"] = time.strftime("%Y-%m-%d %H:%M:%S")

            # 通知 freshness_mon
            _notify_schema_drift(path_id, new_fields, removed_fields)

            # 更新数据契约
            _update_contract_on_drift(storage, has_drift)

        return {
            "ok": True,
            "path_id": path_id,
            "has_drift": has_drift,
            "new_fields": new_fields,
            "removed_fields": removed_fields,
            "recorded_count": len(recorded_schema),
            "new_count": len(new_schema),
        }

    def retrieve_by_date_range(
        self, path_id: int, start_date: str, end_date: str
    ) -> dict[str, Any]:
        """按日期筛选条件检索湖仓文件分区明细，定位候选文件并计算扫描预算。

        - 检索只使用文件自身的分区日期（dt=YYYY-MM-DD），不依赖路径摘要文本；
        - 条件命中后，候选文件限定在该路径登记的文件清单内；
        - 缺少分区日期的历史文件仍可被复用（归入未知分区），但不会被当成当日确证数据。
        """
        path_entry = store.find(self.MODULE, path_id)
        if path_entry is None:
            return {"ok": False, "message": "湖仓路径不存在"}

        start, end = _normalize_range(start_date, end_date)
        if start is None:
            return {"ok": False, "message": "日期区间不合法，应为 YYYY-MM-DD"}

        known_files, unknown_files = _load_catalog_files(path_entry)
        matched = [
            f for f in known_files
            if start <= f["partition_date"] <= end
        ]
        # 未知日期文件：兼容既有目录登记，但不纳入日期命中集合
        query_key = _budget_query_key(path_id, start, end)
        partition_detail = _build_partition_detail(
            [(f["partition_date"], f["size_mb"]) for f in matched]
        )

        budget = _cost_svc.record_partition_scan_budget(
            scope=f"data_lake_read:{path_id}",
            query_key=query_key,
            start_date=start,
            end_date=end,
            matched_files=[_public_file(f) for f in matched],
            unknown_files=[_public_file(f) for f in unknown_files],
            partition_summary=[
                {"partition_date": p, "file_count": len(v["files"]),
                 "size_mb": round(v["size_mb"], 3)}
                for p, v in partition_detail.items()
            ],
            requested_range=(start_date, end_date),
        )

        # 目录中的命中文件摘要回写到路径登记，便于在湖仓读取列表核对
        known_detail = _build_partition_detail(
            [(f["partition_date"], f["size_mb"]) for f in known_files]
        )
        if known_detail and not path_entry.get("分区明细"):
            path_entry["分区明细"] = _dump_partition_detail(known_detail)
        path_entry["最近检索"] = f"{start}~{end}"
        path_entry["命中文件"] = _summarize_paths(f["path"] for f in matched) or "无命中文件"

        return {
            "ok": True,
            "path_id": path_id,
            "storage": path_entry.get("存储位置", ""),
            "query_key": query_key,
            "date_range": f"{start}~{end}",
            "matched_files": [_public_file(f) for f in matched],
            "matched_count": len(matched),
            "unknown_files": [_public_file(f) for f in unknown_files],
            "unknown_count": len(unknown_files),
            "partition_summary": budget["partition_summary"],
            "scan_budget": budget["scan_budget"],
            "cost_entry_id": budget["cost_entry_id"],
            "deduplicated": budget["deduplicated"],
            "coverage": _coverage_note(matched, unknown_files),
        }

    def estimate_partition_scan_cost(
        self, path_id: int, date_range: tuple[str, str]
    ) -> dict[str, Any]:
        """按分区日期估算扫描预算并写入成本治理（兼容旧调用入口）。"""
        path_entry = store.find(self.MODULE, path_id)
        if path_entry is None:
            return {"ok": False, "message": "湖仓路径不存在"}

        result = self.retrieve_by_date_range(path_id, date_range[0], date_range[1])
        if not result["ok"]:
            return result

        budget = result["scan_budget"]
        return {
            "ok": True,
            "estimated_rows": budget["estimated_rows"],
            "estimated_io_mb": budget["estimated_io_mb"],
            "file_count": budget["file_count"],
            "cost_entry_id": result["cost_entry_id"],
            "partition_count": budget["partition_count"],
            "matched_files": result["matched_files"],
            "unknown_files": result["unknown_files"],
        }


# ---------- 分区检索辅助 ----------

# 扫描时生成的分区日期（四个已知日期）
_SCAN_PARTITIONS = ["2026-11-01", "2026-11-02", "2026-11-03", "2026-11-04"]
# 每个分区的文件数按路径/日期确定性推导，避免重复检索得到不同结果
_PARTITION_FILE_RANGE = (2, 5)


def _partition_file_count(storage: str, dt: str) -> int:
    digest = int(hashlib.md5(f"{storage}::{dt}".encode()).hexdigest()[:8], 16)
    low, high = _PARTITION_FILE_RANGE
    return low + digest % (high - low + 1)


def _file_size_mb(path: str) -> float:
    digest = int(hashlib.md5(path.encode()).hexdigest()[:8], 16)
    return round(8 + (digest % 2400) / 100, 2)  # 8MB ~ 32MB


def _normalize_range(start_date: str, end_date: str) -> tuple[str | None, str | None]:
    start, end = (start_date or "").strip(), (end_date or "").strip()
    pattern = re.compile(r"^\d{4}-\d{2}-\d{2}$")
    if not pattern.match(start) or not pattern.match(end):
        return None, None
    if start > end:
        start, end = end, start
    return start, end


def _budget_query_key(path_id: int, start: str, end: str) -> str:
    return f"lake:{path_id}:{start}~{end}"


def _parse_registered_files(path_entry: dict[str, Any]) -> list[str]:
    """解析路径登记的文件清单，兼容旧版的 "... (N files)" 摘要。"""
    raw = str(path_entry.get("文件列表", "") or "")
    raw = _LISTING_SUFFIX_RE.sub("", raw)
    return [part.strip() for part in raw.split(",") if part.strip()]


def _load_catalog_files(
    path_entry: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """读取路径下所有文件的分区日期，缺失日期的历史文件单独归集。"""
    known: list[dict[str, Any]] = []
    unknown: list[dict[str, Any]] = []
    for path in _parse_registered_files(path_entry):
        match = _PARTITION_DATE_RE.search(path)
        record = {"path": path, "size_mb": _file_size_mb(path)}
        if match:
            known.append({**record, "partition_date": match.group(1)})
        else:
            unknown.append({**record, "partition_date": None})
    known.sort(key=lambda f: (f["partition_date"], f["path"]))
    unknown.sort(key=lambda f: f["path"])
    return known, unknown


def _build_partition_detail(files: list[tuple[str, float]]) -> dict[str, dict[str, Any]]:
    """按分区日期归档 (文件路径, 体积)，输出各分区的文件数与总体积。"""
    detail: dict[str, dict[str, Any]] = {}
    for dt, size in files:
        bucket = detail.setdefault(dt, {"files": [], "size_mb": 0.0})
        bucket["files"].append(size)
        bucket["size_mb"] += size
    for bucket in detail.values():
        bucket["size_mb"] = round(bucket["size_mb"], 3)
    return dict(sorted(detail.items()))


def _dump_partition_detail(detail: dict[str, dict[str, Any]]) -> str:
    return "; ".join(
        f"{dt}({len(bucket['files'])}文件/{bucket['size_mb']:.1f}MB)"
        for dt, bucket in detail.items()
    )


def _public_file(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "path": record["path"],
        "partition_date": record.get("partition_date"),
        "size_mb": record["size_mb"],
    }


def _summarize_paths(paths) -> str:
    paths = list(paths)
    if len(paths) <= 3:
        return ", ".join(paths)
    return ", ".join(paths[:3]) + f" 等{len(paths)}个文件"


def _coverage_note(
    matched: list[dict[str, Any]], unknown: list[dict[str, Any]]
) -> dict[str, Any]:
    """给出命中文件与未知分区文件的覆盖说明，避免把未知文件当成当日确证数据。"""
    return {
        "confirmed_files": len(matched),
        "confirmed_rows_basis": "仅按命中分区文件的体积推导",
        "unknown_files": len(unknown),
        "unknown_note": (
            f"另有 {len(unknown)} 个缺少分区日期的历史文件，仅计入待确认部分，未作为当日确证数据"
            if unknown else "目录内文件均带有分区日期，无未知部分"
        ),
    }


def _derive_schema(fmt: str) -> list[str]:
    schemas = {
        "parquet": ["uid", "action", "action_time", "platform", "version", "event_type", "properties"],
        "orc": ["id", "user_id", "order_amount", "order_time", "region", "category"],
        "json": ["event_id", "event_type", "event_time", "payload", "source"],
        "csv": ["col_1", "col_2", "col_3", "col_4", "col_5"],
        "avro": ["user_id", "session_id", "page", "duration", "actions", "device"],
    }
    return schemas.get(fmt, ["field_1", "field_2", "field_3"])


def _register_as_source(storage: str, fmt: str, schema: list[str]) -> int:
    rows = store.rows("source_registry")
    source_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
    source = {
        "id": source_id,
        "源编号": f"SOUR-L{source_id:04d}",
        "源类型": fmt,
        "连接地址": storage,
        "驱动配置": f"lakehouse/{fmt}",
        "数据库名": storage.rsplit("/", 1)[-1],
        "认证方式": "lakehouse_iam",
        "接入状态": "已接入",
        "最近同步": time.strftime("%Y-%m-%d %H:%M:%S"),
        "status": "已注册",
        "pending": True,
        "abnormal": False,
    }
    rows.append(source)
    return source_id


def _create_semantic_model_for_lake(source_id: int, storage: str, schema: list[str]) -> None:
    rows = store.rows("semantic_model")
    model_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
    model = {
        "id": model_id,
        "模型编号": f"SEMA-L{source_id:04d}",
        "模型名称": f"湖仓自动发现_{storage.rsplit('/',1)[-1]}",
        "源表映射": f"data_lake_read:{source_id}",
        "字段别名": ", ".join(schema),
        "度量定义": "COUNT(*), SUM(metrics)",
        "维度定义": ", ".join(schema[:3]),
        "计算口径": "湖仓自动推导",
        "版本序号": "v1.0-lake",
        "status": "已投产",
        "pending": True,
        "abnormal": False,
    }
    rows.append(model)


def _audit_lake_scan(path_id: int, storage: str, file_count: int) -> None:
    rows = store.rows("query_audit")
    audit_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
    audit = {
        "id": audit_id,
        "记录编号": f"AUDIT-{uuid.uuid4().hex[:8]}",
        "查询语句": f"LAKE_SCAN path_id={path_id} storage={storage}",
        "执行耗时": "500ms",
        "扫描行数": str(file_count),
        "返回行数": str(file_count),
        "数据来源": "data_lake_read",
        "操作用户": "lake_scanner",
        "查询时间": time.strftime("%Y-%m-%d %H:%M:%S"),
        "status": "正常",
        "pending": False,
        "abnormal": False,
    }
    rows.append(audit)


def _notify_schema_drift(path_id: int, new_fields: list[str], removed_fields: list[str]) -> None:
    rows = store.rows("freshness_mon")
    mon_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
    mon = {
        "id": mon_id,
        "规则编号": f"FRES-D{path_id:04d}",
        "目标数据集": f"data_lake_read:{path_id}",
        "SLA秒数": "3600",
        "检测频率": "on_change",
        "延迟阈值": "Schema漂移检测",
        "告警级别": "WARNING" if len(new_fields) > 2 else "INFO",
        "触发次数": "1",
        "最后检查": time.strftime("%Y-%m-%d %H:%M:%S"),
        "status": "已告警",
        "pending": True,
        "abnormal": len(removed_fields) > 0,
    }
    rows.append(mon)


def _update_contract_on_drift(storage: str, has_drift: bool) -> None:
    contracts = store.rows("data_contract")
    for c in contracts:
        if storage in str(c.get("数据集", "")):
            if has_drift and c.get("status") == "已承诺":
                c["status"] = "已违约"