<template>
  <section class="page" data-module="auth">
    <header class="page-head">
      <div>
        <h2>凭证管理管理</h2>
        <p class="page-desc">维护认证凭证的输入、执行约束与结果记录，为凭证管理提供可验证的数据工程流程。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记认证凭证</button>
        <button class="btn" type="button" @click="exportRows">导出凭证管理清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <label class="filter-item">
        <span>状态</span>
        <select v-model="statusFilter">
          <option value="">全部</option>
          <option v-for="status in statuses" :key="status" :value="status">{{ status }}</option>
        </select>
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>状态</th>
          <th>认证窗口</th>
          <th>在途批次</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td>
            <span :class="['status-tag', statusClass(row.status)]">{{ row.status ?? '—' }}</span>
          </td>
          <td>{{ row['认证窗口'] ?? '—' }}</td>
          <td>{{ row['在途批次'] ?? '—' }}</td>
          <td class="row-actions">
            <button
              v-for="action in availableActions(row.status)"
              :key="action"
              class="link"
              type="button"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
            <span v-if="!availableActions(row.status).length" class="empty-state">已到状态终点</span>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 4" class="empty-state">暂无凭证管理数据，可先登记认证凭证</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条凭证管理记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>

const ENDPOINT = '/api/auth'
const columns = ["凭证编号", "关联端点", "认证类型", "凭证密钥", "轮换策略", "有效期天数", "过期告警", "自动刷新"]
// 状态只能按待生效、生效、失效单向推进，失效凭据不提供任何可执行动作。
const statuses = ["草稿", "待生效", "生效", "失效"]
const NEXT_ACTION: Record<string, string> = {
  '草稿': '提交生效',
  '待生效': '启用凭证',
  '生效': '标记失效',
}

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const statusFilter = ref('')
const filterFields = columns.slice(0, 3)

const stats = computed(() => [
  { label: "认证凭证总数", value: rows.value.length },
  { label: "生效凭证", value: rows.value.filter((row) => row.status === '生效').length },
  { label: "待生效凭证", value: rows.value.filter((row) => row.status === '待生效').length },
  { label: "失效凭证", value: rows.value.filter((row) => row.status === '失效').length },
])

function availableActions(status: string | number | null): string[] {
  const action = NEXT_ACTION[String(status ?? '')]
  return action ? [action] : []
}

function statusClass(status: string | number | null): string {
  const value = String(status ?? '')
  if (value === '生效') return 'is-active'
  if (value === '失效') return 'is-expired'
  if (value === '待生效') return 'is-pending'
  return ''
}

function resetFilters() {
  filters.value = {}
  statusFilter.value = ''
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '认证凭证登记入口尚未接入审批流'
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ values: { action } }),
    })
    const payload = await response.json()
    if (!response.ok || payload.ok === false) {
      throw new Error(payload.message ?? '凭证管理动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '凭证管理操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>)
  if (statusFilter.value) {
    query.set('status', statusFilter.value)
  }
  try {
    const response = await request(`${ENDPOINT}?${query.toString()}`)
    if (!response.ok) {
      throw new Error('认证凭证列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '凭证管理列表读取失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.status-tag {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 10px;
  font-size: 12px;
  border: 1px solid var(--border);
}
.status-tag.is-active {
  color: #067647;
  background: #ecfdf3;
  border-color: #abefc6;
}
.status-tag.is-expired {
  color: #b42318;
  background: #fef3f2;
  border-color: #fda29b;
}
.status-tag.is-pending {
  color: #b54708;
  background: #fffaeb;
  border-color: #fedf89;
}
</style>
