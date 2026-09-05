import { useState, useEffect, useRef } from 'react'
import { searchJobs, getResumeForJob, startApply } from '../api'
import ErrorBanner from './ErrorBanner'
import ApplyReviewModal from './ApplyReviewModal'

// /api/search-jobs 是一次性阻塞请求，不是流式的——前端并不知道后端现在具体
// 跑到哪一路、哪一轮，所以这里只按"已经等了多久"分阶段给一个大概率的原因，
// 不编造"现在正在查 XX 来源"这种看起来精确实则是猜的进度。阶段划分依据是
// 实测：AnySearch 那一路要做多轮 LLM 查询规划，整个请求经常要 70-80 秒。
function getWaitingMessage(seconds) {
  if (seconds < 8) {
    return '正在同时查询 HN / RemoteOK / Remotive / AnySearch 四路来源'
  }
  if (seconds < 40) {
    return 'AnySearch 这一路会做多轮智能查询规划，通常是耗时最长的部分，请耐心等待'
  }
  return '等待时间较长，如果长时间无响应，可以检查后端终端是否有报错'
}

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

// 产品决定：fit_label 从"只标注不过滤"改成"弱匹配默认折叠，但留一个开关能
// 展开看到全部"——打分模型判断错了，用户还能找回来，不是被后端直接丢弃。
//
// 分组规则：
// - "弱匹配"：进折叠组
// - "强匹配"/"一般匹配"：默认展示组
// - fit_label 是 null 但 profile_scored 为 true（content 为空、没法打分）：
//   这些没有被判定过"不相关"，只是没法打分，不能连带隐藏，归进默认展示组
// - profile_scored 为 false：不分组，全部当默认展示（维持原样全部平铺）
function splitByFitLabel(jobs, profileScored) {
  if (!profileScored) {
    return { visible: jobs, weak: [] }
  }
  const visible = []
  const weak = []
  for (const job of jobs) {
    if (job.fit_label === '弱匹配') {
      weak.push(job)
    } else {
      visible.push(job)
    }
  }
  return { visible, weak }
}

export default function JobSearch({ onUseForTailor }) {
  const [targetRole, setTargetRole] = useState('')
  const [targetRegion, setTargetRegion] = useState('Canada Remote')
  const [maxResults, setMaxResults] = useState(15)
  const [loading, setLoading] = useState(false)
  const [jobs, setJobs] = useState(null)
  const [profileScored, setProfileScored] = useState(true)
  const [showWeak, setShowWeak] = useState(false)
  const [error, setError] = useState(null)
  // 哪些卡片的"职位描述"折叠区展开了——用 id 集合记，默认全部收起
  const [expandedIds, setExpandedIds] = useState(() => new Set())

  // 自动投递相关状态：
  // - applyLoadingIds：正在"检查简历/打开浏览器填表"的职位 id 集合（可能不止一个卡片同时点）
  // - applyNotices：按职位 id 存的"非报错类"提示（比如"还没有定制简历"），跟 ErrorBanner 的
  //   真报错分开，不占用同一个通道
  // - applyError：发起投递本身失败时的报错（网络/后端 400），走 ErrorBanner
  // - activeApply：当前打开的"投递前确认"弹窗要用的数据 { job, draft }，null 表示没开着
  // - appliedIds：这次会话里刚确认投递成功的职位 id——跟 job.status（后端持久化的状态）
  //   分开存，确认成功后立刻在卡片上反映"已投递"，不用重新搜索一次才能看到
  const [applyLoadingIds, setApplyLoadingIds] = useState(() => new Set())
  const [applyNotices, setApplyNotices] = useState({})
  const [applyError, setApplyError] = useState(null)
  const [activeApply, setActiveApply] = useState(null)
  const [appliedIds, setAppliedIds] = useState(() => new Set())

  // 搜索期间的等待计时——每秒 +1，请求结束（成功或报错）就清零/停掉。
  const [elapsedSeconds, setElapsedSeconds] = useState(0)
  const timerRef = useRef(null)

  useEffect(() => {
    if (loading) {
      setElapsedSeconds(0)
      timerRef.current = setInterval(() => {
        setElapsedSeconds((s) => s + 1)
      }, 1000)
    } else {
      clearInterval(timerRef.current)
      timerRef.current = null
      setElapsedSeconds(0)
    }
    return () => clearInterval(timerRef.current)
  }, [loading])

  function toggleExpanded(id) {
    setExpandedIds((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  async function handleAutoApply(job) {
    setApplyNotices((prev) => {
      if (!(job.id in prev)) return prev
      const next = { ...prev }
      delete next[job.id]
      return next
    })
    setApplyError(null)
    setApplyLoadingIds((prev) => new Set(prev).add(job.id))
    try {
      const resumeInfo = await getResumeForJob(job.id)
      if (!resumeInfo.resume_path) {
        setApplyNotices((prev) => ({
          ...prev,
          [job.id]: '还没有为这个职位生成定制简历，请先在下面「定制简历生成与导出」里针对这条职位生成并导出一份简历（导出时会自动关联到这条职位）。',
        }))
        return
      }
      // 这一步会真的打开浏览器窗口并尝试填表，可能要几秒到十几秒。
      const draft = await startApply({
        jobId: job.id,
        jobUrl: job.url,
        jobDescription: job.content,
      })
      setActiveApply({ job, draft })
    } catch (err) {
      setApplyError(err)
    } finally {
      setApplyLoadingIds((prev) => {
        const next = new Set(prev)
        next.delete(job.id)
        return next
      })
    }
  }

  function handleApplyConfirmed(jobId) {
    setAppliedIds((prev) => new Set(prev).add(jobId))
    setActiveApply(null)
  }

  function handleApplyCancelled() {
    setActiveApply(null)
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
      setShowWeak(false) // 每次新搜索都回到默认折叠状态
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
      <ErrorBanner error={applyError} onDismiss={() => setApplyError(null)} />

      {loading && (
        <p className="search-waiting-hint">
          已等待 {elapsedSeconds} 秒... {getWaitingMessage(elapsedSeconds)}
        </p>
      )}

      {jobs && (() => {
        const { visible, weak } = splitByFitLabel(jobs, profileScored)
        // 全是弱匹配（默认展示组是空的）：折叠反而是负体验，用户会以为搜索
        // 失败了——这种情况直接展开显示，不受 showWeak 开关状态影响。
        const forceShowWeak = profileScored && visible.length === 0 && weak.length > 0
        const displayWeak = showWeak || forceShowWeak

        const renderCard = (job) => {
          const hasContent = !!(job.content && job.content.trim())
          const isExpanded = expandedIds.has(job.id)
          const isApplying = applyLoadingIds.has(job.id)
          const isApplied = job.status === 'applied' || appliedIds.has(job.id)
          const notice = applyNotices[job.id]
          return (
            <div key={job.id} className="job-card">
              <div className="job-card__title-row">
                <a href={job.url} target="_blank" rel="noreferrer" className="job-card__title">
                  {job.title}
                </a>
                <button
                  type="button"
                  className="job-card__apply-btn btn-primary"
                  disabled={isApplying || isApplied}
                  onClick={() => handleAutoApply(job)}
                >
                  {isApplied ? '✅ 已投递' : isApplying ? '投递中...' : '🤖 自动投递'}
                </button>
              </div>
              <div className="job-card__meta">
                <span className="badge badge--source">{job.source}</span>
                <span className="job-card__date">{job.posted_at || '未知时间'}</span>
                <FitLabelBadge fitLabel={job.fit_label} />
                <MatchedViaBadge matchedVia={job.matched_via || []} source={job.source} />
              </div>

              {notice && <p className="job-card__apply-notice">{notice}</p>}

              <div className="job-card__actions">
                <button
                  type="button"
                  className="job-card__use-btn btn-ghost"
                  disabled={!hasContent}
                  title={hasContent ? undefined : '这条没有抓到职位描述正文，请点标题链接手动复制'}
                  onClick={() => onUseForTailor(job)}
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
        }

        return (
          <div className="job-list">
            {!profileScored && (
              <p className="profile-scored-hint">
                还没有简历数据，先上传简历后可以看到每条职位与你的匹配度
              </p>
            )}
            {forceShowWeak && (
              <p className="profile-scored-hint">
                没有强/一般匹配的结果，以下是全部 {weak.length} 条弱匹配结果
              </p>
            )}
            <p className="result-box__label">共 {jobs.length} 条结果</p>

            {visible.map(renderCard)}

            {profileScored && weak.length > 0 && (
              <>
                {visible.length > 0 && (
                  <button
                    type="button"
                    className="btn-ghost job-list__toggle-weak-btn"
                    onClick={() => setShowWeak((v) => !v)}
                  >
                    {showWeak ? '只看匹配结果' : `显示全部结果（含弱匹配，共 ${weak.length} 条）`}
                  </button>
                )}
                {displayWeak && weak.map(renderCard)}
              </>
            )}
          </div>
        )
      })()}

      {activeApply && (
        <ApplyReviewModal
          job={activeApply.job}
          draft={activeApply.draft}
          onConfirmed={() => handleApplyConfirmed(activeApply.job.id)}
          onCancelled={handleApplyCancelled}
        />
      )}
    </>
  )
}
