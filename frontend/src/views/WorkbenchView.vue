<template>
  <section class="page" :data-module="meta.key">
    <header class="page-head">
      <div>
        <h2>{{ meta.name }}</h2>
        <p class="page-desc">{{ meta.desc }}</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">
          登记{{ meta.entity }}
        </button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">
          导出{{ meta.name }}清单
        </button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <p class="status-legend">
      <span v-for="item in statusSummary" :key="item.status" class="legend-item">
        {{ item.status }}：{{ item.count }}
      </span>
    </p>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <section v-if="meta.key === 'data_lake_read'" class="partition-panel" data-testid="partition-panel">
      <header class="partition-head">
        <h3>分区日期检索</h3>
        <p>选择湖仓路径与日期区间，按文件分区明细定位候选文件并计算扫描预算。</p>
      </header>
      <form class="analysis-form" @submit.prevent="runPartitionScan">
        <label>
          <span>湖仓路径</span>
          <select v-model.number="scanPathId" data-testid="scan-path">
            <option v-for="row in rows" :key="String(row.id)" :value="Number(row.id)">
              {{ row["路径编号"] }}（{{ row["存储位置"] }}）
            </option>
          </select>
        </label>
        <label>
          <span>起始日期</span>
          <input v-model="scanStart" type="date" data-testid="scan-start" required />
        </label>
        <label>
          <span>结束日期</span>
          <input v-model="scanEnd" type="date" data-testid="scan-end" required />
        </label>
        <button class="btn primary" type="submit" data-testid="scan-submit">检索分区</button>
      </form>

      <div v-if="scanError" class="error-text">{{ scanError }}</div>

      <template v-if="scanResult">
        <div class="analysis-summary">
          <article class="analysis-card">
            <span class="stat-label">检索范围</span>
            <strong>{{ scanResult.date_range }}</strong>
          </article>
          <article class="analysis-card">
            <span class="stat-label">纳入文件</span>
            <strong data-testid="scan-matched">{{ scanResult.matched_count }}</strong>
          </article>
          <article class="analysis-card">
            <span class="stat-label">未知日期文件</span>
            <strong data-testid="scan-unknown">{{ scanResult.unknown_count }}</strong>
          </article>
          <article class="analysis-card">
            <span class="stat-label">扫描预算</span>
            <strong data-testid="scan-budget">
              {{ scanResult.scan_budget.estimated_rows }}行 / {{ scanResult.scan_budget.estimated_io_mb }}MB
            </strong>
          </article>
          <article class="analysis-card">
            <span class="stat-label">成本治理记录</span>
            <strong data-testid="scan-cost-id">#{{ scanResult.cost_entry_id }}</strong>
            <small>{{ scanResult.deduplicated ? '复用已有预算' : '新建预算' }}</small>
          </article>
        </div>

        <table class="data-table" data-testid="scan-file-table">
          <thead>
            <tr><th>文件路径</th><th>分区日期</th><th>体积</th><th>纳入状态</th></tr>
          </thead>
          <tbody>
            <tr v-for="file in scanResult.matched_files" :key="file.path" data-included="true">
              <td>{{ file.path }}</td>
              <td>{{ file.partition_date }}</td>
              <td>{{ file.size_mb }}MB</td>
              <td>已纳入（确证）</td>
            </tr>
            <tr v-for="file in scanResult.unknown_files" :key="file.path" data-included="false" class="unknown-row">
              <td>{{ file.path }}</td>
              <td>—</td>
              <td>{{ file.size_mb }}MB</td>
              <td>未知日期（待确认，不计入预算）</td>
            </tr>
          </tbody>
        </table>

        <p class="coverage-note" data-testid="scan-coverage">{{ scanResult.coverage.unknown_note }}</p>

        <table class="data-table" data-testid="scan-budget-table">
          <thead>
            <tr><th>分区日期</th><th>命中文件数</th><th>分区体积</th><th>分区扫描行数</th></tr>
          </thead>
          <tbody>
            <tr v-for="line in scanResult.scan_budget.partitions" :key="line.partition_date">
              <td>{{ line.partition_date }}</td>
              <td>{{ line.file_count }}</td>
              <td>{{ line.size_mb }}MB</td>
              <td>{{ line.estimated_rows }}</td>
            </tr>
          </tbody>
        </table>
      </template>
    </section>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in meta.fields" :key="column">{{ column }}</th>
          <th>当前状态</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr
          v-for="row in rows"
          :key="String(row.id)"
          :data-testid="`row-${row.id}`"
          :data-status="String(row.status)"
          :class="{ 'row-clickable': meta.key === 'cost_govern' }"
          @click="openRowDetail(row)"
        >
          <td v-for="column in meta.fields" :key="column">{{ row[column] ?? '—' }}</td>
          <td>{{ row.status }}</td>
          <td class="row-actions" @click.stop>
            <button
              v-for="action in meta.actions"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action}`"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="meta.fields.length + 2" class="empty-state">
            暂无{{ meta.name }}数据，可先登记{{ meta.entity }}
          </td>
        </tr>
      </tbody>
    </table>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记{{ meta.entity }}</h3>
        <label v-for="field in meta.fields" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <div v-if="detailRow" class="modal-backdrop" data-testid="row-detail">
      <div class="modal">
        <h3>成本治理详情 · {{ detailRow["策略编号"] }}</h3>
        <dl class="dag-detail">
          <div><dt>检索范围</dt><dd data-testid="detail-range">{{ detailRow["检索范围"] ?? '—' }}</dd></div>
          <div><dt>策略状态</dt><dd>{{ detailRow.status }}</dd></div>
          <div><dt>命中分区</dt><dd data-testid="detail-partitions">{{ detailRow["命中分区"] ?? '—' }}</dd></div>
          <div><dt>扫描预算</dt><dd data-testid="detail-budget">{{ detailRow["扫描预算"] ?? '—' }}</dd></div>
          <div><dt>未知部分</dt><dd data-testid="detail-unknown">{{ detailRow["未知部分"] ?? '—' }}</dd></div>
          <div><dt>查询上限（行）</dt><dd>{{ detailRow["查询上限"] ?? '—' }}</dd></div>
        </dl>

        <template v-if="detailBudget && detailBudget.ok">
          <h4>本次查询预算清单</h4>
          <table class="data-table" data-testid="detail-budget-table">
            <thead>
              <tr><th>分区日期</th><th>文件数</th><th>体积</th><th>估算行数</th></tr>
            </thead>
            <tbody>
              <tr v-for="line in detailBudget.budget_sheet?.partitions ?? []" :key="line.partition_date">
                <td>{{ line.partition_date }}</td>
                <td>{{ line.file_count }}</td>
                <td>{{ line.size_mb }}MB</td>
                <td>{{ line.estimated_rows }}</td>
              </tr>
              <tr v-if="detailBudget.budget_sheet?.unknown.file_count" class="unknown-row">
                <td>未知日期</td>
                <td>{{ detailBudget.budget_sheet?.unknown.file_count }}</td>
                <td>{{ detailBudget.budget_sheet?.unknown.estimated_io_mb }}MB</td>
                <td>待确认（不计入确证预算）</td>
              </tr>
            </tbody>
          </table>
          <p class="coverage-note">
            确证 {{ detailBudget.budget_sheet?.confirmed.file_count ?? 0 }} 个文件 /
            估算 {{ detailBudget.budget_sheet?.confirmed.estimated_rows ?? 0 }} 行；
            未知 {{ detailBudget.budget_sheet?.unknown.file_count ?? 0 }} 个文件仅作待确认。
          </p>
        </template>
        <p v-else-if="detailBudget" class="error-text">{{ detailBudget.message }}</p>

        <div class="page-actions">
          <button class="btn" type="button" data-testid="close-detail" @click="closeRowDetail">关闭</button>
        </div>
      </div>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条{{ meta.name }}记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  createEntry,
  downloadEntries,
  listEntries,
  loadPartitionBudget,
  moduleMeta,
  runAction as applyAction,
  scanLakePartitions,
} from '@/api/backend-service'
import type { CostBudgetDetail, EntryRow, PartitionScanResult } from '@/data/types'

const props = defineProps<{ moduleKey: string }>()
const meta = moduleMeta(props.moduleKey)
const rows = ref<EntryRow[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = meta.fields.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})
const stats = computed(() => [
  { label: meta.metrics[0], value: total.value },
  { label: meta.metrics[1], value: rows.value.filter((row) => row.status === meta.statuses[1]).length },
  {
    label: meta.metrics[2],
    value: rows.value.filter((row) => row.status === meta.statuses[meta.statuses.length - 1]).length,
  },
])
const statusSummary = computed(() =>
  meta.statuses.map((status) => ({
    status,
    count: rows.value.filter((row) => String(row.status) === status).length,
  })),
)

// 湖仓分区检索
const scanPathId = ref<number>(0)
const scanStart = ref('2026-11-01')
const scanEnd = ref('2026-11-02')
const scanResult = ref<PartitionScanResult | null>(null)
const scanError = ref('')

// 成本治理详情
const detailRow = ref<EntryRow | null>(null)
const detailBudget = ref<CostBudgetDetail | null>(null)

function openRowDetail(row: EntryRow) {
  if (meta.key !== 'cost_govern') return
  detailRow.value = row
  detailBudget.value = null
  void loadRowBudget(row)
}

async function loadRowBudget(row: EntryRow) {
  const range = String(row['检索范围'] ?? row['生效时段'] ?? '')
  const [start, end] = range.split('~')
  if (!start || !end) {
    detailBudget.value = { ok: false, message: '该策略没有可核对的检索区间' } as CostBudgetDetail
    return
  }
  const match = String(row['query_key'] ?? '').match(/^lake:(\d+):/)
  if (!match) {
    detailBudget.value = { ok: false, message: '该策略不是湖仓分区扫描预算' } as CostBudgetDetail
    return
  }
  try {
    detailBudget.value = await loadPartitionBudget(Number(match[1]), start, end)
  } catch (error) {
    detailBudget.value = {
      ok: false,
      message: error instanceof Error ? error.message : '预算清单读取失败',
    } as CostBudgetDetail
  }
}

function closeRowDetail() {
  detailRow.value = null
  detailBudget.value = null
}

async function runPartitionScan() {
  scanError.value = ''
  if (!scanPathId.value) {
    scanError.value = '请先选择湖仓路径'
    return
  }
  try {
    scanResult.value = await scanLakePartitions(scanPathId.value, scanStart.value, scanEnd.value)
    await reload()
  } catch (error) {
    scanError.value = error instanceof Error ? error.message : '分区检索失败'
  }
}

function resetFilters() {
  filters.value = {}
  void reload()
}

async function exportRows() {
  errorMessage.value = ''
  try {
    await downloadEntries(meta.key, filters.value)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(meta.fields.map((field) => [field, '']))
  errorMessage.value = ''
  showCreate.value = true
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const result = await createEntry(meta.key, createValues.value)
    if (!result.ok) throw new Error(result.message)
    showCreate.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

async function runAction(action: string, row: EntryRow) {
  errorMessage.value = ''
  try {
    const result = await applyAction(meta.key, Number(row.id), action)
    if (!result.ok) throw new Error(result.message)
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  try {
    const payload = await listEntries(meta.key, filters.value)
    rows.value = payload.items
    total.value = payload.total
    if (meta.key === 'data_lake_read' && !scanPathId.value && rows.value.length) {
      scanPathId.value = Number(rows.value[0].id)
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '业务列表读取失败'
  }
}

onMounted(reload)
</script>
