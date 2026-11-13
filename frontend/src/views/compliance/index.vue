<template>
  <section class="page" data-module="compliance">
    <header class="page-head">
      <div>
        <h2>采集合规配置管理</h2>
        <p class="page-desc">
          维护采集合规的协议配置、访问限制与请求规范。发布前先跑联合门禁：
          核对站点域名、访问间隔、并发上限与请求头模板，再核对代理可用节点与冷却状态，
          并列出使用该站点的采集计划。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记合规规则</button>
        <button class="btn" type="button" @click="exportRows">导出采集合规配置清单</button>
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
          <th>门禁与动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td class="row-actions">
            <button class="link" type="button" @click="openDetail(row)">详情</button>
            <button class="link" type="button" @click="openGate(row)">门禁预检</button>
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
          <td :colspan="columns.length + 1" class="empty-state">暂无采集合规配置数据，可先登记合规规则</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条采集合规配置记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>

    <!-- 登记规则 -->
    <dialog v-if="createOpen" class="modal" open @close="createOpen = false">
      <h3>登记合规规则</h3>
      <label v-for="field in createFields" :key="field.key" class="form-item">
        <span>{{ field.label }}</span>
        <textarea v-if="field.multiline" v-model="createValues[field.key]" rows="3" :placeholder="field.hint" />
        <input v-else v-model="createValues[field.key]" :placeholder="field.hint" />
      </label>
      <p v-if="createError" class="error-text">{{ createError }}</p>
      <div class="modal-actions">
        <button class="btn" type="button" @click="createOpen = false">取消</button>
        <button class="btn primary" type="button" @click="submitCreate">保存为草稿</button>
      </div>
    </dialog>

    <!-- 发布门禁详情 -->
    <dialog v-if="gateView" class="modal wide" open @close="gateView = null">
      <h3>发布前联合门禁 · {{ gateView.规则编号 }}</h3>
      <p class="page-desc">
        访问阈值 {{ gateView.访问阈值 }} · 并发上限 {{ gateView.并发上限 ?? '—' }} ·
        判定 {{ gateView.passed ? '通过' : '未通过' }}
      </p>

      <div class="detail-grid">
        <article v-for="item in gateSummary" :key="item.label" class="stat-card">
          <span class="stat-label">{{ item.label }}</span>
          <strong class="stat-value">{{ item.value }}</strong>
        </article>
      </div>

      <section v-if="gateView.待调整清单?.length" class="detail-block">
        <h4>待调整清单</h4>
        <table class="data-table">
          <thead>
            <tr><th>范围</th><th>条件</th><th>要求</th><th>实测</th><th>调整建议</th></tr>
          </thead>
          <tbody>
            <tr v-for="(item, index) in gateView.待调整清单" :key="index">
              <td>{{ item.范围 }}</td>
              <td>{{ item.条件 }}</td>
              <td>{{ item.要求 }}</td>
              <td>{{ item.实测 }}</td>
              <td>{{ item.调整建议 }}</td>
            </tr>
          </tbody>
        </table>
      </section>

      <section class="detail-block">
        <h4>判定范围与允许保存标准</h4>
        <ul class="detail-list">
          <li v-for="(standard, key) in gateView.允许保存标准" :key="key">{{ key }}：{{ standard }}</li>
        </ul>
        <p class="page-desc">冲突口径：{{ gateView.冲突口径 }}</p>
      </section>

      <section class="detail-block">
        <h4>代理可用量（站点 {{ gateView.代理可用量?.站点域名 }}）</h4>
        <table class="data-table">
          <thead>
            <tr><th>节点编号</th><th>站点匹配</th><th>并发额度</th><th>锁定额度</th><th>可用额度</th><th>冷却状态</th><th>判定</th></tr>
          </thead>
          <tbody>
            <tr v-for="node in gateProxyRows" :key="node.节点编号">
              <td>{{ node.节点编号 }}</td>
              <td>{{ node.绑定站点 || '通用' }}</td>
              <td>{{ node.并发额度 }}</td>
              <td>{{ node.锁定额度 }}</td>
              <td>{{ node.可用额度 }}</td>
              <td>{{ node.冷却状态 }}</td>
              <td>{{ node.可用标识 }}</td>
            </tr>
          </tbody>
        </table>
      </section>

      <section class="detail-block">
        <h4>计划影响清单（{{ gateView.计划影响?.length ?? 0 }} 个）</h4>
        <table class="data-table">
          <thead>
            <tr><th>计划编号</th><th>目标站点</th><th>采集频率</th><th>启动资格</th><th>排班状态</th><th>冲突</th></tr>
          </thead>
          <tbody>
            <tr v-for="plan in gateView.计划影响" :key="plan.计划编号">
              <td>{{ plan.计划编号 }}</td>
              <td>{{ plan.目标站点 }}</td>
              <td>{{ plan.采集频率 }}</td>
              <td>{{ plan.启动资格 }}</td>
              <td>{{ plan.排班状态 }}</td>
              <td>{{ plan.采集间隔秒 !== null && plan.采集间隔秒 < gateView.访问阈值秒 ? '低于访问阈值' : '—' }}</td>
            </tr>
          </tbody>
        </table>
      </section>

      <div class="modal-actions">
        <button class="btn" type="button" @click="gateView = null">关闭</button>
        <button
          v-if="gateView.可发布"
          class="btn primary"
          type="button"
          @click="publishFromGate"
        >
          确认发布
        </button>
      </div>
      <p v-if="gateError" class="error-text">{{ gateError }}</p>
    </dialog>

    <!-- 规则详情 -->
    <dialog v-if="detailView" class="modal wide" open @close="detailView = null">
      <h3>规则详情 · {{ detailView.规则编号 }}</h3>
      <p class="page-desc">
        状态 {{ detailView.status }} · 版本 {{ detailView.规则版本 }} · 访问间隔 {{ detailView.访问间隔 }} ·
        并发上限 {{ detailView.并发上限 }} · 站点 {{ detailView.站点域名 }}
      </p>

      <section class="detail-block">
        <h4>发布记录</h4>
        <table class="data-table">
          <thead>
            <tr><th>结果</th><th>版本</th><th>来源</th><th>发布时间</th><th>锁定额度</th><th>计划影响</th></tr>
          </thead>
          <tbody>
            <tr v-for="record in detailView.发布记录" :key="String(record.id)">
              <td>{{ record.结果 }}</td>
              <td>{{ record.版本号 }}</td>
              <td>{{ record.来源 }}</td>
              <td>{{ record.发布时间 }}</td>
              <td>{{ record.锁定额度 ?? '—' }}</td>
              <td>{{ (record.计划影响 ?? []).join('、') || '—' }}</td>
            </tr>
            <tr v-if="!detailView.发布记录?.length">
              <td colspan="6" class="empty-state">尚未发布</td>
            </tr>
          </tbody>
        </table>
      </section>

      <section class="detail-block">
        <h4>当前锁定与代理可用量</h4>
        <table class="data-table">
          <thead>
            <tr><th>节点编号</th><th>并发额度</th><th>锁定额度</th><th>可用额度</th><th>冷却状态</th></tr>
          </thead>
          <tbody>
            <tr v-for="node in detailProxyRows" :key="node.节点编号">
              <td>{{ node.节点编号 }}</td>
              <td>{{ node.并发额度 }}</td>
              <td>{{ node.锁定额度 }}</td>
              <td>{{ node.可用额度 }}</td>
              <td>{{ node.冷却状态 }}</td>
            </tr>
          </tbody>
        </table>
      </section>

      <section class="detail-block">
        <h4>计划影响清单</h4>
        <table class="data-table">
          <thead>
            <tr><th>计划编号</th><th>采集频率</th><th>计划状态</th><th>启动资格</th><th>排班状态</th></tr>
          </thead>
          <tbody>
            <tr v-for="plan in detailView.计划影响" :key="plan.计划编号">
              <td>{{ plan.计划编号 }}</td>
              <td>{{ plan.采集频率 }}</td>
              <td>{{ plan.计划状态 }}</td>
              <td>{{ plan.启动资格 }}</td>
              <td>{{ plan.排班状态 }}</td>
            </tr>
            <tr v-if="!detailView.计划影响?.length">
              <td colspan="5" class="empty-state">该站点暂无采集计划</td>
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
type Gate = Record<string, any>

const ENDPOINT = '/api/compliance'
const columns = ["规则编号", "站点域名", "协议标识", "访问间隔", "并发上限", "请求头模板", "合规状态", "最近审查"]
const actions = ["提交审批", "发布规则", "废弃规则"]
const statuses = ["草稿", "待审批", "已生效", "已废弃"]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const stats = computed(() => [
  { label: '合规规则总数', value: total.value },
  { label: '已生效规则', value: rows.value.filter((row) => row.status === '已生效').length },
  { label: '待审批规则', value: rows.value.filter((row) => row.status === '待审批').length },
])

const createOpen = ref(false)
const createError = ref('')
const createValues = ref<Record<string, string>>({
  规则编号: '',
  站点域名: '',
  协议标识: 'https',
  访问间隔: '1s',
  并发上限: '4',
  请求头模板: 'User-Agent: CompQcBot/1.0',
  最近审查: '',
})
const createFields = [
  { key: '规则编号', label: '规则编号', hint: '如 COMP-0004' },
  { key: '站点域名', label: '站点域名', hint: '如 news.demo.local' },
  { key: '协议标识', label: '协议标识', hint: 'http 或 https' },
  { key: '访问间隔', label: '访问间隔', hint: '如 1s / 500ms' },
  { key: '并发上限', label: '并发上限', hint: '1~32 的整数' },
  { key: '请求头模板', label: '请求头模板', hint: '每行 `名称: 值`，需包含 User-Agent', multiline: true },
  { key: '最近审查', label: '最近审查', hint: 'YYYY-MM-DD，可留空' },
]

const gateView = ref<Gate | null>(null)
const gateError = ref('')
const detailView = ref<Gate | null>(null)

const gateSummary = computed(() => {
  const gate = gateView.value
  if (!gate) return []
  return [
    { label: '代理可分配额度', value: gate.代理可用量?.可分配额度 ?? 0 },
    { label: '需要额度', value: gate.并发上限 ?? 0 },
    { label: '受影响计划', value: (gate.计划影响 ?? []).length },
    { label: '待调整项', value: (gate.待调整清单 ?? []).length },
  ]
})

const gateProxyRows = computed(() => {
  const gate = gateView.value
  if (!gate?.代理可用量) return []
  return [...(gate.代理可用量.可用节点 ?? []), ...(gate.代理可用量.不可用节点 ?? [])]
})

const detailProxyRows = computed(() => {
  const detail = detailView.value
  if (!detail?.代理可用量) return []
  return [...(detail.代理可用量.可用节点 ?? []), ...(detail.代理可用量.不可用节点 ?? [])]
})

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
      throw new Error(payload.message ?? '合规规则登记失败')
    }
    createOpen.value = false
    await reload()
  } catch (error) {
    createError.value = error instanceof Error ? error.message : '合规规则登记失败'
  }
}

async function openGate(row: Row) {
  gateError.value = ''
  gateId.value = Number(row.id)
  try {
    const response = await request(`${ENDPOINT}/${row.id}/publish-gate`)
    if (!response.ok) {
      throw new Error('发布前门禁预检失败')
    }
    gateView.value = await response.json()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '发布前门禁预检失败'
  }
}

async function publishFromGate() {
  if (!gateView.value || gateId.value === null) return
  gateError.value = ''
  try {
    const response = await request(`${ENDPOINT}/${gateId.value}/actions`, {
      method: 'POST',
      body: JSON.stringify({ values: { action: '发布规则' } }),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) {
      throw new Error(payload.detail?.message ?? payload.message ?? '发布未生效')
    }
    gateView.value = null
    await reload()
  } catch (error) {
    gateError.value = error instanceof Error ? error.message : '发布未生效'
  }
}

const gateId = ref<number | null>(null)

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  if (action === '发布规则') {
    await openGate(row)
    return
  }
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ values: { action } }),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) {
      throw new Error(payload.detail?.message ?? payload.message ?? '采集合规配置动作未生效')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '采集合规配置操作失败'
  }
}

async function openDetail(row: Row) {
  const response = await request(`${ENDPOINT}/${row.id}`)
  if (response.ok) {
    detailView.value = await response.json()
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('合规规则列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '采集合规配置列表读取失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.modal { position: fixed; inset: 8% 50% auto; transform: translateX(50%); width: min(560px, 92vw); max-height: 84vh; overflow: auto; background: #fff; border: 1px solid var(--border); border-radius: 10px; padding: 18px; box-shadow: 0 20px 40px rgba(15, 23, 42, 0.18); z-index: 20; }
.modal.wide { width: min(980px, 96vw); }
.form-item span { display: block; font-size: 12px; color: var(--muted); margin-top: 10px; }
.form-item input, .form-item textarea { width: 100%; padding: 6px 8px; border: 1px solid var(--border); border-radius: 6px; font: inherit; }
.modal-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 14px; }
.detail-block { margin-top: 14px; }
.detail-block h4 { margin: 0 0 8px; font-size: 14px; }
.detail-list { margin: 0; padding-left: 18px; font-size: 13px; color: #334155; }
.detail-grid { display: flex; gap: 10px; margin-top: 10px; }
</style>
