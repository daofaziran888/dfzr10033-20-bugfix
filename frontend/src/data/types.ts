/** 纯前端数据层的公共类型：与全栈版后端返回的结构保持一致，换回后端时页面不用改。 */

export type EntryRow = {
  id: number
  status: string
  pending: boolean
  abnormal: boolean
  [field: string]: string | number | boolean
}

export type ModuleMeta = {
  key: string
  name: string
  entity: string
  desc: string
  fields: string[]
  statuses: string[]
  actions: string[]
  actionTargets: Record<string, string>
  metrics: string[]
}

export type PageResult = {
  items: EntryRow[]
  total: number
  page: number
  size: number
}

export type ActionResult = {
  ok: boolean
  message: string
}

export type LakeFileRef = {
  path: string
  partition_date: string | null
  size_mb: number
}

export type PartitionBudgetLine = {
  partition_date: string
  file_count: number
  size_mb: number
  estimated_rows: number
}

export type PartitionScanBudget = {
  basis: string
  partitions: PartitionBudgetLine[]
  partition_count: number
  confirmed: { file_count: number; estimated_rows: number; estimated_io_mb: number }
  unknown: {
    file_count: number
    estimated_rows: number
    estimated_io_mb: number
    confirmed: boolean
  }
  file_count: number
  estimated_rows: number
  estimated_io_mb: number
  estimated_total_rows: number
}

export type PartitionScanResult = {
  ok: boolean
  message?: string
  query_key: string
  date_range: string
  matched_files: LakeFileRef[]
  matched_count: number
  unknown_files: LakeFileRef[]
  unknown_count: number
  scan_budget: PartitionScanBudget
  cost_entry_id: number
  deduplicated: boolean
  coverage: {
    confirmed_files: number
    confirmed_rows_basis: string
    unknown_files: number
    unknown_note: string
  }
}

export type CostBudgetDetail = {
  ok: boolean
  message?: string
  query_key: string
  cost_entry_id: number
  策略编号: string
  检索范围: string
  命中分区: string
  扫描预算: string
  未知部分: string
  status: string
  matched_files: LakeFileRef[]
  unknown_files: LakeFileRef[]
  budget_sheet: PartitionScanBudget | null
}

export type OverviewResult = {
  cards: { label: string; value: number }[]
  modules: { name: string; created: number; pending: number; abnormal: number }[]
}
