import { request } from './client'
import { moduleMeta } from '@/data/modules'
import type {
  ActionResult,
  CostBudgetDetail,
  EntryRow,
  OverviewResult,
  PageResult,
  PartitionScanResult,
} from '@/data/types'

export { moduleMeta }

export async function listEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<PageResult> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}?${query}`)
  if (!response.ok) throw new Error('业务数据读取失败')
  return await response.json()
}

export async function createEntry(
  key: string,
  values: Record<string, string>,
): Promise<ActionResult> {
  const response = await request(`/api/${key}`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  return await response.json()
}

export async function runAction(
  key: string,
  id: number,
  action: string,
): Promise<ActionResult> {
  const response = await request(`/api/${key}/${id}/actions`, {
    method: 'POST',
    body: JSON.stringify({ action }),
  })
  return await response.json()
}

export async function downloadEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<void> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}/export?${query}`)
  if (!response.ok) throw new Error('业务数据导出失败')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${key}.csv`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export async function loadOverview(): Promise<OverviewResult> {
  const response = await request('/api/overview')
  if (!response.ok) throw new Error('运营概览读取失败')
  return await response.json()
}

export async function scanLakePartitions(
  pathId: number,
  startDate: string,
  endDate: string,
): Promise<PartitionScanResult> {
  const query = new URLSearchParams({
    path_id: String(pathId),
    start_date: startDate,
    end_date: endDate,
  }).toString()
  const response = await request(`/api/data_lake_read/partition-scan?${query}`, { method: 'POST' })
  const payload = (await response.json()) as PartitionScanResult
  if (!response.ok || !payload.ok) throw new Error(payload.message || '分区检索失败')
  return payload
}

export async function loadPartitionBudget(
  pathId: number,
  startDate: string,
  endDate: string,
): Promise<CostBudgetDetail> {
  const query = new URLSearchParams({ start_date: startDate, end_date: endDate }).toString()
  const response = await request(`/api/data_lake_read/${pathId}/partition-budget?${query}`)
  if (!response.ok) throw new Error('预算记录读取失败')
  return (await response.json()) as CostBudgetDetail
}
