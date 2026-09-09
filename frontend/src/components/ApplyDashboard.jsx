import { useState, useEffect, useMemo, useCallback } from 'react'
import { getApplyDashboard, markApplyConfirmed } from '../api'
import ErrorBanner from './ErrorBanner'

const STATUS_META = {
  queued_for_review: { label: '排队待处理', cls: 'badge--medium' },
  submitted_unverified: { label: '已提交·待确认', cls: 'badge--weak' },
  applied_confirmed: { label: '已确认投递', cls: 'badge--strong' },
  needs_user: { label: '需人工处理', cls: 'badge--weak' },
  applied: { label: '已投递（旧）', cls: 'badge--strong' },
  new: { label: '新', cls: 'badge--neutral' },
  viewed: { label: '看过', cls: 'badge--neutral' },
}

function StatusBadge({ status, label }) {
  const meta = STATUS_META[status] || { label: label || status, cls: 'badge--neutral' }
  return <span className={`badge ${meta.cls}`}>{meta.label}</span>
}

function fmtTime(iso) {
  if (!iso) return '—'
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString()
}

function useDashboard() {
  const [data, setData] = useState({ queue: [], applications: [] })
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  const reload = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      setData(await getApplyDashboard())
    } catch (err) {
      setError(err)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    reload()
  }, [reload])

  return { data, loading, error, reload }
}

function Toolbar({ query, setQuery, statusFilter, setStatusFilter, statuses, onRefresh }) {
  return (
    <div className="control-row dashboard__toolbar">
      <input
        placeholder="按公司 / 职位名搜索"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        style={{ flex: 1, minWidth: '12em' }}
      />
      <select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
        <option value="">全部状态</option>
        {statuses.map((s) => (
          <option key={s} value={s}>
            {(STATUS_META[s] || {}).label || s}
          </option>
        ))}
      </select>
      <button type="button" className="btn-ghost" onClick={onRefresh}>
        刷新
      </button>
    </div>
  )
}

function applyFilters(rows, query, statusFilter) {
  const q = query.trim().toLowerCase()
  return rows.filter((r) => {
    if (statusFilter && r.status !== statusFilter) return false
    if (!q) return true
    return (
      (r.title || '').toLowerCase().includes(q) ||
      (r.company || '').toLowerCase().includes(q)
    )
  })
}

function QueueView({ rows, query, setQuery, statusFilter, setStatusFilter, onRefresh }) {
  const statuses = useMemo(() => [...new Set(rows.map((r) => r.status))].sort(), [rows])
  const shown = useMemo(() => applyFilters(rows, query, statusFilter), [rows, query, statusFilter])

  return (
    <>
      <Toolbar
        query={query} setQuery={setQuery}
        statusFilter={statusFilter} setStatusFilter={setStatusFilter}
        statuses={statuses} onRefresh={onRefresh}
      />
      <p className="result-box__label">
        排队中 {shown.length} / {rows.length} 条（按 fit_score 降序）
      </p>
      <div className="apply-modal__table-wrap">
        <table className="apply-modal__table dashboard__table">
          <thead>
            <tr>
              <th>fit</th>
              <th>关联度</th>
              <th>职位</th>
              <th>公司</th>
              <th>来源</th>
              <th>状态</th>
              <th>入队时间</th>
            </tr>
          </thead>
          <tbody>
            {shown.map((r) => (
              <tr key={r.record_id}>
                <td>{r.fit_score == null ? '—' : r.fit_score.toFixed(3)}</td>
                <td>{r.relevance_label || (r.fit_label || '—')}</td>
                <td>
                  {r.url ? (
                    <a href={r.url} target="_blank" rel="noreferrer">{r.title || r.url}</a>
                  ) : (r.title || '—')}
                </td>
                <td>{r.company || '—'}</td>
                <td><span className="badge badge--source">{r.source || '—'}</span></td>
                <td><StatusBadge status={r.status} label={r.status_label} /></td>
                <td className="dashboard__time">{fmtTime(r.first_seen)}</td>
              </tr>
            ))}
            {shown.length === 0 && (
              <tr><td colSpan={7} className="dashboard__empty">没有匹配的排队职位</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </>
  )
}

function ApplicationsView({ rows, query, setQuery, statusFilter, setStatusFilter, onRefresh }) {
  const [busyId, setBusyId] = useState(null)
  const [markError, setMarkError] = useState(null)
  const statuses = useMemo(() => [...new Set(rows.map((r) => r.status))].sort(), [rows])
  const shown = useMemo(() => applyFilters(rows, query, statusFilter), [rows, query, statusFilter])

  async function handleMark(recordId) {
    setBusyId(recordId)
    setMarkError(null)
    try {
      await markApplyConfirmed(recordId)
      await onRefresh()
    } catch (err) {
      setMarkError(err)
    } finally {
      setBusyId(null)
    }
  }

  return (
    <>
      <Toolbar
        query={query} setQuery={setQuery}
        statusFilter={statusFilter} setStatusFilter={setStatusFilter}
        statuses={statuses} onRefresh={onRefresh}
      />
      <ErrorBanner error={markError} onDismiss={() => setMarkError(null)} />
      <p className="result-box__label">投递历史 {shown.length} / {rows.length} 条（按更新时间降序）</p>
      <div className="apply-modal__table-wrap">
        <table className="apply-modal__table dashboard__table">
          <thead>
            <tr>
              <th>职位</th>
              <th>公司</th>
              <th>状态</th>
              <th>判定依据</th>
              <th>更新时间</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {shown.map((r) => (
              <tr key={r.record_id}>
                <td>
                  {r.url ? (
                    <a href={r.url} target="_blank" rel="noreferrer">{r.title || r.url}</a>
                  ) : (r.title || '—')}
                </td>
                <td>{r.company || '—'}</td>
                <td><StatusBadge status={r.status} label={r.status_label} /></td>
                <td className="dashboard__reason">{r.status_reason || '—'}</td>
                <td className="dashboard__time">{fmtTime(r.updated_at || r.applied_at)}</td>
                <td>
                  {(r.status === 'submitted_unverified' || r.status === 'needs_user' || r.status === 'applied') && (
                    <button
                      type="button"
                      className="btn-ghost"
                      disabled={busyId === r.record_id}
                      onClick={() => handleMark(r.record_id)}
                    >
                      {busyId === r.record_id ? '...' : '标为已确认'}
                    </button>
                  )}
                </td>
              </tr>
            ))}
            {shown.length === 0 && (
              <tr><td colSpan={6} className="dashboard__empty">没有匹配的投递记录</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </>
  )
}

export default function ApplyDashboard() {
  const { data, loading, error, reload } = useDashboard()
  const [tab, setTab] = useState('queue')
  const [query, setQuery] = useState('')
  const [statusFilter, setStatusFilter] = useState('')

  // 切 tab 时清掉搜索/筛选，两个视图的状态集不一样
  function switchTab(next) {
    setTab(next)
    setQuery('')
    setStatusFilter('')
  }

  return (
    <>
      <div className="dashboard__tabs">
        <button
          type="button"
          className={`dashboard__tab ${tab === 'queue' ? 'dashboard__tab--active' : ''}`}
          onClick={() => switchTab('queue')}
        >
          待处理队列 {data.queue.length > 0 && `(${data.queue.length})`}
        </button>
        <button
          type="button"
          className={`dashboard__tab ${tab === 'applications' ? 'dashboard__tab--active' : ''}`}
          onClick={() => switchTab('applications')}
        >
          投递历史 {data.applications.length > 0 && `(${data.applications.length})`}
        </button>
      </div>

      <ErrorBanner error={error} onDismiss={() => {}} />
      {loading && <p className="search-waiting-hint">加载中...</p>}

      {!loading && tab === 'queue' && (
        <QueueView
          rows={data.queue}
          query={query} setQuery={setQuery}
          statusFilter={statusFilter} setStatusFilter={setStatusFilter}
          onRefresh={reload}
        />
      )}
      {!loading && tab === 'applications' && (
        <ApplicationsView
          rows={data.applications}
          query={query} setQuery={setQuery}
          statusFilter={statusFilter} setStatusFilter={setStatusFilter}
          onRefresh={reload}
        />
      )}
    </>
  )
}
