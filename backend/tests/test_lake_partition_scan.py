"""湖仓按分区检索与扫描预算的集成测试。

场景固定为：四个已知分区日期 + 一个无日期历史文件，两个互斥区间分别检索。
"""
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.cost_govern import CostGovernService
from app.services.data_lake_read import DataLakeReadService
from app.store import store

client = TestClient(app)

PATH_ID = 1
LEFT_RANGE = ("2026-11-01", "2026-11-02")
RIGHT_RANGE = ("2026-11-03", "2026-11-04")


@pytest.fixture()
def lake_catalog():
    """准备一个含四个已知日期分区和一个未知日期文件的湖仓路径。"""
    path = store.find("data_lake_read", PATH_ID)
    if path is None:
        path, _ = DataLakeReadService().create_entry({
            "路径编号": "LAKE-TEST-001",
            "存储位置": "s3://data-lake/test",
            "文件格式": "parquet",
            "分区模式": "dt",
        })
        path = store.find("data_lake_read", path["id"])
    storage = "s3://data-lake/test"
    files = [
        f"{storage}/dt=2026-11-01/part-00001.parquet",
        f"{storage}/dt=2026-11-01/part-00002.parquet",
        f"{storage}/dt=2026-11-02/part-00001.parquet",
        f"{storage}/dt=2026-11-03/part-00001.parquet",
        f"{storage}/dt=2026-11-03/part-00002.parquet",
        f"{storage}/dt=2026-11-04/part-00001.parquet",
        f"{storage}/legacy/part-00001.parquet",
    ]
    path["文件列表"] = ", ".join(files)
    for row in list(store.rows("cost_govern")):
        if str(row.get("query_key", "")).startswith(f"lake:{path['id']}:"):
            store.rows("cost_govern").remove(row)
    return path


def test_two_disjoint_ranges_get_distinct_budgets(lake_catalog):
    lake = DataLakeReadService()
    left = lake.retrieve_by_date_range(lake_catalog["id"], *LEFT_RANGE)
    right = lake.retrieve_by_date_range(lake_catalog["id"], *RIGHT_RANGE)

    assert left["ok"] and right["ok"]
    assert left["query_key"] != right["query_key"]

    # 两个区间命中的文件不相交
    left_paths = {f["path"] for f in left["matched_files"]}
    right_paths = {f["path"] for f in right["matched_files"]}
    assert left_paths and right_paths and not (left_paths & right_paths)

    # 不同文件 → 不同扫描预算（修复前此处相同）
    assert left["scan_budget"]["estimated_rows"] != right["scan_budget"]["estimated_rows"]
    assert left["cost_entry_id"] != right["cost_entry_id"]

    # 预算只统计本次命中分区
    assert [p["partition_date"] for p in left["scan_budget"]["partitions"]] == ["2026-11-01", "2026-11-02"]
    assert [p["partition_date"] for p in right["scan_budget"]["partitions"]] == ["2026-11-03", "2026-11-04"]


def test_unknown_date_file_is_not_confirmed(lake_catalog):
    result = DataLakeReadService().retrieve_by_date_range(lake_catalog["id"], *LEFT_RANGE)
    assert result["unknown_count"] == 1
    unknown = result["unknown_files"][0]
    assert unknown["partition_date"] is None
    assert unknown["path"] not in {f["path"] for f in result["matched_files"]}

    # 未知文件只进待确认部分，不计入确证扫描预算
    budget = result["scan_budget"]
    assert budget["unknown"]["file_count"] == 1
    assert budget["unknown"]["confirmed"] is False
    assert budget["estimated_rows"] == budget["confirmed"]["estimated_rows"]
    assert budget["estimated_total_rows"] > budget["estimated_rows"]
    assert "未作为当日确证数据" in result["coverage"]["unknown_note"]


def test_repeat_submission_reuses_same_budget(lake_catalog):
    lake = DataLakeReadService()
    first = lake.retrieve_by_date_range(lake_catalog["id"], *LEFT_RANGE)
    before = len(store.rows("cost_govern"))
    second = lake.retrieve_by_date_range(lake_catalog["id"], *LEFT_RANGE)

    assert second["deduplicated"] is True
    assert second["cost_entry_id"] == first["cost_entry_id"]
    assert second["scan_budget"]["estimated_rows"] == first["scan_budget"]["estimated_rows"]
    assert len(store.rows("cost_govern")) == before


def test_legacy_summary_listing_still_parsed(lake_catalog):
    lake_catalog["文件列表"] = (
        "s3://data-lake/test/dt=2026-11-01/part-00001.parquet, "
        "s3://data-lake/test/dt=2026-11-01/part-00002.parquet... (2 files)"
    )
    result = DataLakeReadService().retrieve_by_date_range(lake_catalog["id"], *LEFT_RANGE)
    assert result["ok"]
    assert result["matched_count"] == 2
    assert all(f["path"].endswith(".parquet") for f in result["matched_files"])


def test_api_scan_and_budget_detail(lake_catalog):
    payload = client.post(
        "/api/data_lake_read/partition-scan",
        params={"path_id": lake_catalog["id"], "start_date": LEFT_RANGE[0], "end_date": LEFT_RANGE[1]},
    ).json()
    assert payload["ok"] and payload["matched_count"] == 3

    detail = client.get(
        f"/api/data_lake_read/{lake_catalog['id']}/partition-budget",
        params={"start_date": LEFT_RANGE[0], "end_date": LEFT_RANGE[1]},
    ).json()
    assert detail["ok"]
    assert detail["cost_entry_id"] == payload["cost_entry_id"]
    assert detail["budget_sheet"]["estimated_rows"] == payload["scan_budget"]["estimated_rows"]
    assert detail["检索范围"] == f"{LEFT_RANGE[0]}~{LEFT_RANGE[1]}"


def test_cost_govern_record_kept_in_current_scope(lake_catalog):
    result = DataLakeReadService().retrieve_by_date_range(lake_catalog["id"], *RIGHT_RANGE)
    entry = store.find("cost_govern", result["cost_entry_id"])
    assert entry is not None
    assert entry["检索范围"] == f"{RIGHT_RANGE[0]}~{RIGHT_RANGE[1]}"
    assert entry["生效时段"] == entry["检索范围"]
    assert entry["query_key"] == result["query_key"]
    assert entry["预算清单"]["estimated_rows"] == result["scan_budget"]["estimated_rows"]

    fetched = CostGovernService().get_partition_scan_budget(result["query_key"])
    assert fetched["ok"] and fetched["cost_entry_id"] == result["cost_entry_id"]
