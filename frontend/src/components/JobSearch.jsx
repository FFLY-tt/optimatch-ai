import { useState } from 'react'
import { searchJobs } from '../api'
import ErrorBanner from './ErrorBanner'

// matched_via 强弱信号展示规则：
// - 含 "title"：强信号，实色标签
// - 非空但不含 "title"（只有 tags 或只有 description）：弱信号，实测这类假阳性比例高
// - 空数组 + source 是 remoteok/remotive：这两个连接器本来就会计算 matched_via，
//   空数组说明是"跨字段拼凑命中"（比如关键词 A 只在 title 里、关键词 B 只在
//   description 里，没有单个字段完整包含所有关键词）——比单字段命中更弱
// - 空数组 + source 不是 remoteok/remotive（hn/anysearch）：这两路根本没有
//   matched_via 概念，不是弱信号，不能和上面那种空数组混为一谈
function MatchedViaBadge({ matchedVia, source }) {
  const isFieldConnector = source === 'remoteok' || source === 'remotive'

  if (matchedVia.length === 0) {
    if (!isFieldConnector) {
      return <span className="badge badge--neutral">整条内容语境相关</span>
    }
    return (
      <span
        className="badge badge--weakest"
        title="关键词分散命中在不同字段（比如一个词只在标题里、另一个词只在正文里），没有单个字段完整包含所有关键词——比标签/正文单独命中更弱的信号"
      >
        跨字段拼凑命中
      </span>
    )
  }

  if (matchedVia.includes('title')) {
    return <span className="badge badge--strong">标题命中：{matchedVia.join(', ')}</span>
  }

  return (
    <span
      className="badge badge--weak"
      title="关键词只在标签或正文里出现，标题没有直接体现——实测这类结果假阳性比例较高，请自行判断"
    >
      仅 {matchedVia.join('/')} 命中
    </span>
  )
}

// fit_label 徽章——跟 matched_via 是两个不同维度的信号（matched_via 是"关键词
// 搜索命中的位置"，fit_label 是"这条职位内容跟当前简历画像的匹配度"），分开展示。
// fit_score 为 None（没有画像 / 这条 content 是空的，没法打分）不展示徽章，
// 不能当成"弱匹配"处理——两者语义不一样。
function FitLabelBadge({ fitLabel }) {
  if (!fitLabel) return null
  const variant = fitLabel === '强匹配' ? 'strong' : fitLabel === '弱匹配' ? 'weak' : 'medium'
  return <span className={`badge badge--fit-${variant}`}>{fitLabel}</span>
}

export default function JobSearch({ onUseForTailor }) {
  const [targetRole, setTargetRole] = useState('')
  const [targetRegion, setTargetRegion] = useState('Canada Remote')
  const [maxResults, setMaxResults] = useState(15)
  const [loading, setLoading] = useState(false)
  const [jobs, setJobs] = useState(null)
  const [profileScored, setProfileScored] = useState(true)
  const [error, setError] = useState(null)
  // 哪些卡片的"职位描述"折叠区展开了——用 id 集合记，默认全部收起
  const [expandedIds, setExpandedIds] = useState(() => new Set())

  function toggleExpanded(id) {
    setExpandedIds((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  async function handleSearch(e) {
    e.preventDefault()
    if (!targetRole.trim()) return
    setLoading(true)
    setError(null)
    setJobs(null)
    try {
      const data = await searchJobs({
        targetRole,
        targetRegion,
        maxResults: Number(maxResults) || 15,
      })
      setJobs(data.jobs)
      setProfileScored(data.profile_scored)
    } catch (err) {
      setError(err)
    } finally {
      setLoading(false)
    }
  }

  return (
    <>
      <form onSubmit={handleSearch} className="control-row">
        <input
          placeholder="目标职位 *"
          value={targetRole}
          onChange={(e) => setTargetRole(e.target.value)}
          required
        />
        <input
          placeholder="目标地区"
          value={targetRegion}
          onChange={(e) => setTargetRegion(e.target.value)}
        />
        <input
          type="number"
          min={1}
          value={maxResults}
          onChange={(e) => setMaxResults(e.target.value)}
          style={{ width: '5em' }}
        />
        <button className="btn-primary" type="submit" disabled={loading || !targetRole.trim()}>
          {loading ? '搜索中...' : '搜索'}
        </button>
      </form>
      <ErrorBanner error={error} onDismiss={() => setError(null)} />

      {jobs && (
        <div className="job-list">
          {!profileScored && (
            <p className="profile-scored-hint">
              还没有简历数据，先上传简历后可以看到每条职位与你的匹配度
            </p>
          )}
          <p className="result-box__label">共 {jobs.length} 条结果</p>
          {jobs.map((job) => {
            const hasContent = !!(job.content && job.content.trim())
            const isExpanded = expandedIds.has(job.id)
            return (
              <div key={job.id} className="job-card">
                <a
                  href={job.url}
                  target="_blank"
                  rel="noreferrer"
                  className="job-card__title"
                >
                  {job.title}
                </a>
                <div className="job-card__meta">
                  <span className="badge badge--source">{job.source}</span>
                  <span className="job-card__date">{job.posted_at || '未知时间'}</span>
                  <FitLabelBadge fitLabel={job.fit_label} />
                  <MatchedViaBadge matchedVia={job.matched_via || []} source={job.source} />
                </div>

                <div className="job-card__actions">
                  <button
                    type="button"
                    className="job-card__use-btn btn-ghost"
                    disabled={!hasContent}
                    title={hasContent ? undefined : '这条没有抓到职位描述正文，请点标题链接手动复制'}
                    onClick={() => onUseForTailor(job.content)}
                  >
                    用这条生成简历
                  </button>
                  <button
                    type="button"
                    className="job-card__toggle-btn btn-ghost"
                    onClick={() => toggleExpanded(job.id)}
                  >
                    {isExpanded ? '收起职位描述' : '展开查看职位描述'}
                  </button>
                </div>

                {isExpanded && (
                  <pre className="job-card__content">
                    {hasContent ? job.content : '（这条没有抓到职位描述正文）'}
                  </pre>
                )}
              </div>
            )
          })}
        </div>
      )}
    </>
  )
}
