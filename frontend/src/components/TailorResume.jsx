import { useState } from 'react'
import { tailorResume, exportResume } from '../api'
import ErrorBanner from './ErrorBanner'

const EXPORT_FORMATS = ['docx', 'md', 'pdf']

export default function TailorResume({ jobDescription, onJobDescriptionChange, jobId }) {
  const [userNotes, setUserNotes] = useState('')
  const [topK, setTopK] = useState(5)
  const [loading, setLoading] = useState(false)
  const [result, setResult] = useState(null)
  const [editedContent, setEditedContent] = useState('')
  const [error, setError] = useState(null)

  const [candidateName, setCandidateName] = useState('')
  const [exportingFormat, setExportingFormat] = useState(null)
  const [exportLinks, setExportLinks] = useState({})
  const [exportError, setExportError] = useState(null)

  async function handleGenerate(e) {
    e.preventDefault()
    if (!jobDescription.trim()) return
    setLoading(true)
    setError(null)
    setResult(null)
    setExportLinks({})
    try {
      const data = await tailorResume({
        jobDescription,
        userNotes,
        topK: Number(topK) || 5,
      })
      setResult(data)
      setEditedContent(data.tailored_resume)
    } catch (err) {
      setError(err)
    } finally {
      setLoading(false)
    }
  }

  async function handleExport(format) {
    if (!candidateName.trim() || !editedContent.trim()) return
    setExportingFormat(format)
    setExportError(null)
    try {
      const data = await exportResume(format, {
        finalContent: editedContent,
        candidateName,
        jobId,
      })
      setExportLinks((prev) => ({ ...prev, [format]: data.download_url }))
    } catch (err) {
      setExportError(err)
    } finally {
      setExportingFormat(null)
    }
  }

  return (
    <>
      <form onSubmit={handleGenerate} className="form-col">
        <textarea
          rows={6}
          placeholder="职位描述 (JD) *"
          value={jobDescription}
          onChange={(e) => onJobDescriptionChange(e.target.value)}
          required
        />
        <textarea
          rows={3}
          placeholder="补充说明（可选）"
          value={userNotes}
          onChange={(e) => setUserNotes(e.target.value)}
        />
        <div className="control-row">
          <label>
            top_k：
            <input
              type="number"
              min={1}
              value={topK}
              onChange={(e) => setTopK(e.target.value)}
              style={{ width: '4em' }}
            />
          </label>
          <button className="btn-primary" type="submit" disabled={loading || !jobDescription.trim()}>
            {loading ? '生成中...' : '生成定制简历'}
          </button>
        </div>
      </form>
      <ErrorBanner error={error} onDismiss={() => setError(null)} />

      {result && (
        <div className="result-box">
          <div className="review-status">
            <span className={`badge ${result.passed_review ? 'badge--strong' : 'badge--weak'}`}>
              {result.passed_review ? '✅ 结构校验通过（原文关键内容一字未丢）' : '⚠️ 结构校验未通过'}
            </span>
            <span>尝试次数：{result.attempts}</span>
            {result.issue && result.issue.toLowerCase() !== 'none' && (
              <span>问题：{result.issue}</span>
            )}
          </div>

          {result.relevance_label && (
            <p
              className={`job-link-hint ${
                result.relevance_label === 'WEAK'
                  ? 'job-link-hint--unlinked'
                  : 'job-link-hint--linked'
              }`}
            >
              {result.relevance_label === 'STRONG' && '🟢 强匹配'}
              {result.relevance_label === 'MODERATE' && '🟡 一般匹配'}
              {result.relevance_label === 'WEAK' && '🔴 弱匹配 —— 你的真实经历跟这个岗位方向不太搭，投递前自己再权衡一下'}
              {result.relevance_note ? `：${result.relevance_note}` : ''}
            </p>
          )}

          {result.changes && result.changes.length > 0 && (
            <>
              <p className="result-box__label">本次针对该职位做的调整（原文其余部分原样保留）：</p>
              <ul className="matched-chunks">
                {result.changes.map((c, i) => (
                  <li key={i}>{c}</li>
                ))}
              </ul>
            </>
          )}

          {result.matched_chunks && result.matched_chunks.length > 0 && (
            <>
              <p className="result-box__label">这条职位命中了简历里的这些经历（仅供参考）：</p>
              <ul className="matched-chunks">
                {result.matched_chunks.map((c, i) => (
                  <li key={i}>{c}</li>
                ))}
              </ul>
            </>
          )}

          <p className="result-box__label">定制简历内容（可编辑）：</p>
          <textarea
            className="tailored-content"
            rows={14}
            value={editedContent}
            onChange={(e) => setEditedContent(e.target.value)}
          />

          {jobId ? (
            <p className="job-link-hint job-link-hint--linked">
              📌 导出会关联到职位 <code>{jobId}</code>——自动投递会自动用这份简历，不用手动选文件
            </p>
          ) : (
            <p className="job-link-hint job-link-hint--unlinked">
              这份简历没有关联到具体职位（不是从职位卡片的"用这条生成简历"跳转过来的）——
              导出后自动投递不会自动用到它，需要手动指定简历路径
            </p>
          )}
          <div className="export-row control-row">
            <input
              placeholder="候选人姓名 *"
              value={candidateName}
              onChange={(e) => setCandidateName(e.target.value)}
            />
            {EXPORT_FORMATS.map((fmt) => (
              <button
                key={fmt}
                type="button"
                className="btn-primary"
                onClick={() => handleExport(fmt)}
                disabled={!candidateName.trim() || !editedContent.trim() || exportingFormat === fmt}
              >
                {exportingFormat === fmt ? '导出中...' : `导出 ${fmt.toUpperCase()}`}
              </button>
            ))}
          </div>
          <ErrorBanner error={exportError} onDismiss={() => setExportError(null)} />
          {Object.keys(exportLinks).length > 0 && (
            <div className="export-links">
              {Object.entries(exportLinks).map(([fmt, url]) => (
                <a key={fmt} href={url} target="_blank" rel="noreferrer">
                  下载 {fmt.toUpperCase()}
                </a>
              ))}
            </div>
          )}
        </div>
      )}
    </>
  )
}
