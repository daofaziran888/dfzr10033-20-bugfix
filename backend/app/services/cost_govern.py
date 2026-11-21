"""成本治理领域服务：查询配额管理、资源消耗追踪与跨模块成本归因。"""
from __future__ import annotations

import time
import uuid
from typing import Any

from app.services.base import BaseModuleService
from app.store import store

# 分区扫描预算口径：按文件体积折算扫描行数，保证同一批文件重复检索得到同一预算
ROWS_PER_MB = 12_000


class CostGovernService(BaseModuleService):
    MODULE = "cost_govern"
    ENTITY = "配额策略"
    OPERATION_TERM = "查询"
    LIST_FIELDS = ["策略编号", "适用角色", "查询上限", "并发上限", "月度配额", "告警阈值", "限流动作",
                   "生效时段", "检索范围", "命中分区", "扫描预算", "未知部分"]
    REQUIRED_FIELDS = ["策略编号", "适用角色"]
    STATUS_ORDER = ["草稿", "已生效", "已告警", "已停用"]
    ACTION_RULES = {"启效策略": "已生效", "触发限流": "已告警", "停用策略": "已停用"}
    NEGATIVE_ACTIONS = ["触发限流", "停用策略"]

    def record_partition_scan_budget(
        self,
        *,
        scope: str,
        query_key: str,
        start_date: str,
        end_date: str,
        matched_files: list[dict[str, Any]],
        unknown_files: list[dict[str, Any]],
        partition_summary: list[dict[str, Any]],
        requested_range: tuple[str, str] | None = None,
    ) -> dict[str, Any]:
        """把按分区计算的扫描预算写入当前检索范围对应的成本治理记录。

        同一 query_key（同一路径 + 同一互斥区间）重复提交时复用原记录，
        不新增预算记录，也不累加扫描量。
        """
        budget = _compute_scan_budget(matched_files, unknown_files, partition_summary)
        rows = store.rows(self.MODULE)

        existing = next((r for r in rows if r.get("query_key") == query_key), None)
        if existing is not None:
            existing["预算清单"] = budget
            return {
                "ok": True,
                "cost_entry_id": existing["id"],
                "deduplicated": True,
                "scan_budget": budget,
                "partition_summary": partition_summary,
            }

        cost_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        estimated_rows = budget["estimated_rows"]
        estimated_io = budget["estimated_io_mb"]
        matched_count = budget["file_count"]
        unknown_note = (
            f"{budget['unknown']['file_count']}个无分区日期文件/待确认{budget['unknown']['estimated_rows']}行"
            if budget["unknown"]["file_count"] else "无"
        )
        cost = {
            "id": cost_id,
            "策略编号": f"COST-LK{cost_id:04d}",
            "适用角色": "lake_reader",
            "查询上限": str(estimated_rows),
            "并发上限": str(matched_count),
            "月度配额": str(estimated_rows * 30),
            "告警阈值": str(int(estimated_io * 1.5)),
            "限流动作": "throttle",
            "生效时段": f"{start_date}~{end_date}",
            "检索范围": f"{start_date}~{end_date}",
            "命中分区": ", ".join(p["partition_date"] for p in budget["partitions"]) or "无",
            "扫描预算": f"{estimated_rows}行/{estimated_io}MB",
            "未知部分": unknown_note,
            "status": "已生效" if estimated_io < 5000 else "已告警",
            "pending": estimated_io >= 5000,
            "abnormal": estimated_io >= 10000,
            "query_key": query_key,
            "budget_scope": scope,
            "requested_range": list(requested_range or (start_date, end_date)),
            "matched_files": matched_files,
            "unknown_files": unknown_files,
            "预算清单": budget,
        }
        rows.append(cost)

        return {
            "ok": True,
            "cost_entry_id": cost_id,
            "deduplicated": False,
            "scan_budget": budget,
            "partition_summary": partition_summary,
        }

    def get_partition_scan_budget(self, query_key: str) -> dict[str, Any]:
        """按检索范围读取预算记录，返回成本治理详情所需的预算清单。"""
        entry = next(
            (r for r in store.rows(self.MODULE) if r.get("query_key") == query_key), None
        )
        if entry is None:
            return {"ok": False, "message": "该检索范围还没有预算记录", "query_key": query_key}
        return {
            "ok": True,
            "query_key": query_key,
            "cost_entry_id": entry.get("id"),
            "策略编号": entry.get("策略编号"),
            "检索范围": entry.get("检索范围"),
            "命中分区": entry.get("命中分区"),
            "扫描预算": entry.get("扫描预算"),
            "未知部分": entry.get("未知部分"),
            "status": entry.get("status"),
            "matched_files": entry.get("matched_files", []),
            "unknown_files": entry.get("unknown_files", []),
            "budget_sheet": entry.get("预算清单"),
        }

    def aggregate_module_costs(self) -> dict[str, Any]:
        """汇总各模块成本：读取 query_audit、result_cache、query_federation，写入 cost_govern。"""
        # 统计各模块查询审计
        audits = store.rows("query_audit")
        module_costs: dict[str, dict[str, float]] = {}
        for a in audits:
            source = a.get("数据来源", "unknown")
            elapsed_str = a.get("执行耗时", "0ms")
            try:
                elapsed = float(elapsed_str.replace("ms", ""))
            except (ValueError, AttributeError):
                elapsed = 0
            scanned_str = a.get("扫描行数", "0")
            try:
                scanned = int(scanned_str)
            except (ValueError, TypeError):
                scanned = 0

            if source not in module_costs:
                module_costs[source] = {"total_time_ms": 0, "total_scanned": 0, "query_count": 0}
            module_costs[source]["total_time_ms"] += elapsed
            module_costs[source]["total_scanned"] += scanned
            module_costs[source]["query_count"] += 1

        # 生成汇总策略
        generated_policies = []
        for module, costs in module_costs.items():
            if costs["total_scanned"] > 1000:
                rows = store.rows(self.MODULE)
                cost_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
                alert_level = "已告警" if costs["total_scanned"] > 100000 else "已生效"
                cost = {
                    "id": cost_id,
                    "策略编号": f"COST-MC{uuid.uuid4().hex[:8]}",
                    "适用角色": f"module_{module}",
                    "查询上限": str(costs["total_scanned"]),
                    "并发上限": str(costs["query_count"]),
                    "月度配额": str(costs["total_scanned"] * 30),
                    "告警阈值": "100000",
                    "限流动作": "monitor",
                    "生效时段": time.strftime("%Y-%m-%d"),
                    "status": alert_level,
                    "pending": alert_level == "已告警",
                    "abnormal": costs["total_scanned"] > 500000,
                }
                rows.append(cost)
                generated_policies.append({
                    "policy_id": cost_id,
                    "module": module,
                    "status": alert_level,
                })

        return {
            "ok": True,
            "modules_analyzed": len(module_costs),
            "total_scanned_rows": sum(c["total_scanned"] for c in module_costs.values()),
            "total_time_ms": sum(c["total_time_ms"] for c in module_costs.values()),
            "policies_generated": len(generated_policies),
            "module_breakdown": {k: v for k, v in sorted(module_costs.items())},
        }

    def check_monthly_quota(self) -> dict[str, Any]:
        """检查月度配额：读取 cost_govern 策略和 query_audit 当月使用量，超限时触发 freshness_mon 告警。"""
        policies = store.rows(self.MODULE)
        overused: list[dict[str, Any]] = []

        for policy in policies:
            try:
                quota = int(policy.get("月度配额", "0"))
                alert = int(policy.get("告警阈值", "0"))
            except (ValueError, TypeError):
                continue

            if quota <= 0:
                continue

            # 统计当月使用量
            usage = _estimate_monthly_usage(policy.get("适用角色", ""))
            ratio = usage / quota

            if ratio > 0.8:
                policy["status"] = "已告警"
                policy["pending"] = True
                overused.append({
                    "policy_id": policy["id"],
                    "角色": policy.get("适用角色"),
                    "quota": quota,
                    "usage": usage,
                    "ratio": round(ratio, 2),
                })

                # 触发 freshness_mon 通知
                _notify_quota_alert(policy["id"], policy.get("适用角色", ""), ratio)

        return {
            "ok": True,
            "total_policies": len(policies),
            "overused_count": len(overused),
            "overused": overused[:10],
        }


def _compute_scan_budget(
    matched_files: list[dict[str, Any]],
    unknown_files: list[dict[str, Any]],
    partition_summary: list[dict[str, Any]],
) -> dict[str, Any]:
    """按分区汇总扫描预算：已知分区的命中文件计入确证预算，未知文件单列待确认。"""

    def _rows(size_mb: float) -> int:
        return int(round(float(size_mb) * ROWS_PER_MB))

    partitions: list[dict[str, Any]] = []
    for item in partition_summary:
        size_mb = float(item.get("size_mb", 0))
        partitions.append({
            "partition_date": item["partition_date"],
            "file_count": int(item.get("file_count", 0)),
            "size_mb": round(size_mb, 3),
            "estimated_rows": _rows(size_mb),
        })

    matched_rows = sum(_rows(f.get("size_mb", 0)) for f in matched_files)
    matched_io = round(sum(float(f.get("size_mb", 0)) for f in matched_files), 3)
    unknown_rows = sum(_rows(f.get("size_mb", 0)) for f in unknown_files)
    unknown_io = round(sum(float(f.get("size_mb", 0)) for f in unknown_files), 3)

    return {
        "basis": "按分区日期命中文件体积折算",
        "partitions": partitions,
        "partition_count": len(partitions),
        "confirmed": {
            "file_count": len(matched_files),
            "estimated_rows": matched_rows,
            "estimated_io_mb": matched_io,
        },
        "unknown": {
            "file_count": len(unknown_files),
            "estimated_rows": unknown_rows,
            "estimated_io_mb": unknown_io,
            "confirmed": False,
        },
        "file_count": len(matched_files),
        "estimated_rows": matched_rows,
        "estimated_io_mb": matched_io,
        "estimated_total_rows": matched_rows + unknown_rows,
    }


def _estimate_monthly_usage(role: str) -> int:
    audits = store.rows("query_audit")
    count = 0
    for a in audits:
        if role in str(a.get("数据来源", "")) or role in str(a.get("适用角色", "")):
            scanned_str = a.get("扫描行数", "0")
            try:
                count += int(scanned_str)
            except (ValueError, TypeError):
                pass
    return max(count, 100)


def _notify_quota_alert(policy_id: int, role: str, ratio: float) -> None:
    rows = store.rows("freshness_mon")
    mon_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
    mon = {
        "id": mon_id,
        "规则编号": f"FRES-Q{policy_id:04d}",
        "目标数据集": f"cost_govern:{policy_id}",
        "SLA秒数": "86400",
        "检测频率": "monthly",
        "延迟阈值": f"quota_overuse_{ratio:.0%}",
        "告警级别": "CRITICAL" if ratio > 1.0 else "WARNING",
        "触发次数": "1",
        "最后检查": time.strftime("%Y-%m-%d %H:%M:%S"),
        "status": "已告警",
        "pending": True,
        "abnormal": ratio > 1.0,
    }
    rows.append(mon)