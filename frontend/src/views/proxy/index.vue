<template>
  <section class="page" data-module="proxy">
    <header class="page-head">
      <div>
        <h2>代理池管理</h2>
        <p class="page-desc">
          维护代理池的节点配置、响应指标与切换策略。合规规则发布时按站点锁定这里的额度，
          因此清单会同步显示并发额度、锁定额度、可用额度与冷却状态。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记代理节点</button>
        <button class="btn" type="button" @click="exportRows">导出代理池管理清单</button>
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
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可用判定</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td :class="row.可用标识 === '不可用' ? 'error-text' : ''">
            {{ row.可用标识 }}<span v-if="row.不可用原因">（{{ row.不可用原因 }}）</span>
          </td>
          <td class="row-actions">
            <button class="link" type="button" @click="openDetail(row)">详情</button>
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 2" class="empty-state">暂无代理池管理数据，可先登记代理节点</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条代理池管理记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>

    <dialog v-if="createOpen" class="modal" open @close="createOpen = false">
      <h3>登记代理节点</h3>
      <label v-for="field in createFields" :key="field.key" class="form-item">
        <span>{{ field.label }}</span>
        <input v-model="createValues[field.key]" :placeholder="field.hint" />
      </label>
      <p v-if="createError" class="error-text">{{ createError }}</p>
      <div class="modal-actions">
        <button class="btn" type="button" @click="createOpen = false">取消</button>
        <button class="btn primary" type="button" @click="submitCreate">登记为草稿</button>
      </div>
    </dialog>

    <dialog v-if="detailView" class="modal" open @close="detailView = null">
      <h3>代理节点详情 · {{ detailView.节点编号 }}</h3>
      <p class="page-desc">
        状态 {{ detailView.节点状态 ?? detailView.status }} · 冷却 {{ detailView.冷却状态 }} ·
        并发额度 {{ detailView.并发额度 }} · 锁定 {{ detailView.锁定额度 }} · 可用 {{ detailView.可用额度 }}
      </p>
      <p v-if="detailView.不可用原因" class="error-text">不可用原因：{{ detailView.不可用原因 }}</p>
      <section class="detail-block">
        <h4>被合规规则锁定的额度</h4>
        <table class="data-table">
          <thead>
            <tr><th>规则编号</th><th>版本号</th><th>锁定额度</th></tr>
          </thead>
          <tbody>
            <tr v-for="lock in detailView.锁定规则" :key="`${lock.规则编号}-${lock.版本号}`">
              <td>{{ lock.规则编号 }}</td>
              <td>{{ lock.版本号 }}</td>
              <td>{{ lock.锁定额度 }}</td>
            </tr>
            <tr v-if="!detailView.锁定规则?.length">
              <td colspan="3" class="empty-state">未被任何合规规则锁定</td>
            </tr>
          </tbody>
        </table>
      </section>
      <div class="modal-actions">
        <button class="btn" type="button" @click="detailView = null">关闭</button>
      </div>
    </dialog>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null | undefined>

type Lock = { 规则编号?: string; 版本号?: string; 锁定额度?: number }
type ProxyDetail = {
  节点编号?: string
  status?: string
  节点状态?: string
  冷却状态?: string
  并发额度?: number
  锁定额度?: number
  可用额度?: number
  不可用原因?: string
  锁定规则?: Lock[]
}

const ENDPOINT = '/api/proxy'
const columns = ["节点编号", "代理地址", "代理类型", "地理位置", "响应延迟", "可用率", "并发额度", "锁定额度", "可用额度", "冷却状态", "节点状态"]
const actions = ["提交校验", "启用节点", "停用节点"]
const statuses = ["草稿", "待校验", "已启用", "已停用"]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const stats = computed(() => [
  { label: '代理节点总数', value: total.value },
  { label: '可用节点', value: rows.value.filter((row) => row.可用标识 === '可用').length },
  { label: '可用额度合计', value: rows.value.reduce((sum, row) => sum + Number(row.可用额度 ?? 0), 0) },
])

const createOpen = ref(false)
const createError = ref('')
const createValues = ref<Record<string, string>>({
  节点编号: '',
  代理地址: '',
  代理类型: 'HTTP',
  地理位置: '',
  响应延迟: '120ms',
  可用率: '98%',
  切换策略: '按延迟',
  并发额度: '4',
  冷却状态: '就绪',
  绑定站点: '',
})
const createFields = [
  { key: '节点编号', label: '节点编号', hint: '如 PROX-0006' },
  { key: '代理地址', label: '代理地址', hint: 'IP:端口' },
  { key: '代理类型', label: '代理类型', hint: 'HTTP / HTTPS / SOCKS5' },
  { key: '地理位置', label: '地理位置', hint: '如 北京' },
  { key: '响应延迟', label: '响应延迟', hint: '如 120ms' },
  { key: '可用率', label: '可用率', hint: '如 98%' },
  { key: '切换策略', label: '切换策略', hint: '如 按延迟' },
  { key: '并发额度', label: '并发额度', hint: '正整数' },
  { key: '冷却状态', label: '冷却状态', hint: '就绪 / 冷却中' },
  { key: '绑定站点', label: '绑定站点', hint: '可留空表示通用节点' },
]

const detailView = ref<ProxyDetail | null>(null)

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  createError.value = ''
  createOpen.value = true
}

async function submitCreate() {
  createError.value = ''
  try {
    const response = await request(ENDPOINT, {
      method: 'POST',
      body: JSON.stringify({ values: createValues.value }),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) {
      throw new Error(payload.message ?? '代理节点登记失败')
    }
    createOpen.value = false
    await reload()
  } catch (error) {
    createError.value = error instanceof Error ? error.message : '代理节点登记失败'
  }
}

async function openDetail(row: Row) {
  const response = await request(`${ENDPOINT}/${row.id}`)
  if (response.ok) {
    detailView.value = await response.json()
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ values: { action } }),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) {
      throw new Error(payload.message ?? '代理池管理动作未生效')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '代理池管理操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('代理节点列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '代理池管理列表读取失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.modal { position: fixed; inset: 10% 50% auto; transform: translateX(50%); width: min(520px, 92vw); max-height: 80vh; overflow: auto; background: #fff; border: 1px solid var(--border); border-radius: 10px; padding: 18px; box-shadow: 0 20px 40px rgba(15, 23, 42, 0.18); z-index: 20; }
.form-item span { display: block; font-size: 12px; color: var(--muted); margin-top: 8px; }
.form-item input { width: 100%; padding: 6px 8px; border: 1px solid var(--border); border-radius: 6px; font: inherit; }
.modal-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 14px; }
.detail-block { margin-top: 12px; }
.detail-block h4 { margin: 0 0 8px; font-size: 14px; }
</style>
