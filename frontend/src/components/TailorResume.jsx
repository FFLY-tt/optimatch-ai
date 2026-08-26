import { useState } from 'react'
import { tailorResume, exportResume } from '../api'
import ErrorBanner from './ErrorBanner'

const EXPORT_FORMATS = ['docx', 'md', 'pdf']

export default function TailorResume({ jobDescription, onJobDescriptionChange }) {
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
              {result.passed_review ? '✅ 审核通过' : '⚠️ 未通过审核'}
            </span>
            <span>尝试次数：{result.attempts}</span>
            {result.issue && result.issue !== 'none' && <span>问题：{result.issue}</span>}
          </div>

          <p className="result-box__label">匹配到的简历片段：</p>
          <ul className="matched-chunks">
            {result.matched_chunks.map((c, i) => (
              <li key={i}>{c}</li>
            ))}
          </ul>

          <p className="result-box__label">定制简历内容（可编辑）：</p>
          <textarea
            className="tailored-content"
            rows={14}
            value={editedContent}
            onChange={(e) => setEditedContent(e.target.value)}
          />

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
